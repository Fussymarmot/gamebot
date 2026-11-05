"""Соединения с SQLite и помощники запросов.

q      — чтение (отдельное соединение: не видит чужие незавершённые транзакции)
x      — одиночная запись
batch  — пачка записей одной транзакцией (всё или ничего)
tx     — транзакция с чтением и записью внутри: `async with tx() as t: ...`

Все записи идут через один asyncio.Lock и явные BEGIN IMMEDIATE / COMMIT / ROLLBACK,
поэтому чужой `x()` больше не может закоммитить половину чужой пачки, а при любой ошибке
(включая отмену задачи) транзакция откатывается целиком.

Правила для кода внутри `tx()`:
  * не делать await к Telegram и другим долгим операциям — пока транзакция открыта,
    остальные записи ждут;
  * внутри можно вызывать q / x / batch / вложенный tx — они выполнятся в этой же транзакции.
"""
import asyncio
import contextvars
import os
from contextlib import asynccontextmanager
from typing import Optional

import aiosqlite

from config import BASE_DIR
from utils.logs import logger

DB_PATH = os.path.join(BASE_DIR, "bot.db")

BUSY_TIMEOUT_MS = 10000
READ_POOL_SIZE = 3            # WAL позволяет читать параллельно: один тяжёлый SELECT не стопорит остальные
LOCK_WAIT_WARN = 5.0          # столько секунд ждём писательскую блокировку, потом пишем предупреждение
LOCK_WAIT_MAX = 30.0          # и столько — потом сдаёмся с ошибкой вместо вечного зависания

_conn: Optional[aiosqlite.Connection] = None      # запись
_rpool: list = []                                 # чтение (пул соединений)
_rnext = 0
_wlock = asyncio.Lock()                           # единственный писатель
_open_lock = asyncio.Lock()                       # защита от двойного открытия соединений
_holder: Optional[str] = None                     # кто сейчас держит блокировку (для диагностики)


class DatabaseBusy(RuntimeError):
    """Писательская блокировка не освободилась за LOCK_WAIT_MAX секунд."""


@asynccontextmanager
async def _writer():
    """Захват писательской блокировки с таймаутом и диагностикой.

    Раньше при зависшей транзакции все записи (ставки, выплаты, результаты игр) молча вставали
    в очередь навсегда. Теперь через LOCK_WAIT_WARN секунд в логе видно, кто держит блокировку,
    а через LOCK_WAIT_MAX ожидание обрывается ошибкой, и бот продолжает отвечать.
    """
    global _holder
    if not _wlock.locked():
        await _wlock.acquire()
    else:
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            await asyncio.wait_for(_wlock.acquire(), LOCK_WAIT_WARN)
        except asyncio.TimeoutError:
            logger.warning("Запись в БД ждёт блокировку больше %.1f с; держит: %s", LOCK_WAIT_WARN, _holder)
            try:
                await asyncio.wait_for(_wlock.acquire(), max(0.1, LOCK_WAIT_MAX - (loop.time() - started)))
            except asyncio.TimeoutError:
                logger.error("Запись в БД не дождалась блокировки за %.1f с; держит: %s", LOCK_WAIT_MAX, _holder)
                raise DatabaseBusy("база данных занята")
    task = asyncio.current_task()
    _holder = task.get_name() if task else "?"
    try:
        yield
    finally:
        _holder = None
        _wlock.release()


async def _open(writer: bool) -> aiosqlite.Connection:
    # isolation_level=None: автокоммит, транзакциями управляем явно сами
    c = await aiosqlite.connect(DB_PATH, isolation_level=None)
    await c.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    await c.execute("PRAGMA temp_store=MEMORY")
    await c.execute("PRAGMA cache_size=-16384")          # 16 МБ страниц на соединение
    if writer:
        await c.execute("PRAGMA journal_mode=WAL")
        await c.execute("PRAGMA synchronous=NORMAL")
    else:
        await c.execute("PRAGMA query_only=ON")
        await c.execute("PRAGMA mmap_size=134217728")     # 128 МБ: чтение без лишних копирований
    return c


async def connect() -> aiosqlite.Connection:
    """Соединение для записи."""
    global _conn
    if _conn is None:
        async with _open_lock:
            if _conn is None:
                _conn = await _open(True)
    return _conn


async def connect_read() -> aiosqlite.Connection:
    """Соединение для чтения из пула (WAL: читатель видит только зафиксированные данные)."""
    global _rnext
    if len(_rpool) < READ_POOL_SIZE:
        await connect()  # режим WAL включает писатель
        async with _open_lock:
            while len(_rpool) < READ_POOL_SIZE:
                _rpool.append(await _open(False))
    _rnext = (_rnext + 1) % len(_rpool)
    return _rpool[_rnext]


async def disconnect():
    global _conn
    async with _wlock:  # не закрываем соединение посреди чужой транзакции
        async with _open_lock:
            for c in (*_rpool, _conn):
                if c is not None:
                    try:
                        await c.close()
                    except Exception:
                        logger.debug("Не удалось закрыть соединение с базой", exc_info=True)
            _conn = None
            _rpool.clear()


@asynccontextmanager
async def exclusive():
    """Монопольный доступ к записи без транзакции (для VACUUM, checkpoint и т. п.)."""
    async with _writer():
        yield await connect()


async def _run(c: aiosqlite.Connection, sql: str, params=()):
    cur = await c.execute(sql, params)
    await cur.close()


async def _rollback(c: aiosqlite.Connection):
    """Дожидается ROLLBACK даже при повторной отмене текущей задачи."""
    task = asyncio.create_task(_run(c, "ROLLBACK"))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Отмена исходной задачи уже обрабатывается в tx(); не бросаем
            # соединение в неопределённом состоянии, пока откат ещё выполняется.
            continue
        except Exception:
            break
    try:
        task.result()
    except Exception as exc:
        # Отсутствие активной транзакции допустимо; остальные ошибки важны.
        if "no transaction is active" not in str(exc).lower():
            logger.exception("Не удалось выполнить ROLLBACK SQLite", exc_info=exc)


class Tx:
    """Операции внутри открытой транзакции, принадлежащей одной asyncio-задаче."""

    def __init__(self, conn: aiosqlite.Connection):
        self._c = conn
        self.owner = asyncio.current_task()
        self.active = True
        self.lastrowid = 0

    def _check(self):
        if not self.active:
            raise RuntimeError("Транзакция уже завершена")

    async def execute(self, sql: str, params=()) -> int:
        """Выполняет запись, возвращает число затронутых строк."""
        self._check()
        cur = await self._c.execute(sql, params)
        n, self.lastrowid = cur.rowcount, cur.lastrowid
        await cur.close()
        return n

    async def executemany(self, sql: str, rows) -> None:
        self._check()
        cur = await self._c.executemany(sql, rows)
        await cur.close()

    async def fetchone(self, sql: str, params=()):
        self._check()
        cur = await self._c.execute(sql, params)
        row = await cur.fetchone()
        await cur.close()
        return row

    async def fetchall(self, sql: str, params=()):
        self._check()
        cur = await self._c.execute(sql, params)
        rows = await cur.fetchall()
        await cur.close()
        return rows


_current: contextvars.ContextVar[Optional[Tx]] = contextvars.ContextVar("db_tx", default=None)


def _active_tx() -> Optional[Tx]:
    t = _current.get()
    if t is None or not t.active:
        return None
    # ContextVar копируется в asyncio.create_task(). Не разрешаем дочерней задаче
    # незаметно пользоваться соединением/транзакцией родителя.
    if t.owner is not asyncio.current_task():
        raise RuntimeError(
            "Нельзя использовать транзакцию из другой asyncio-задачи; "
            "не создавайте asyncio.create_task() внутри database.tx()"
        )
    return t


@asynccontextmanager
async def tx():
    """Транзакция: при выходе без ошибки COMMIT, при любой ошибке и отмене задачи ROLLBACK."""
    outer = _active_tx()
    if outer is not None:  # вложенный вызов присоединяется к внешней транзакции
        yield outer
        return
    async with _writer():
        c = await connect()
        t = Tx(c)
        token = _current.set(t)
        try:
            await _run(c, "BEGIN IMMEDIATE")
            yield t
            await _run(c, "COMMIT")
        except BaseException:
            await _rollback(c)
            raise
        finally:
            t.active = False
            _current.reset(token)


async def q(sql: str, params=(), one: bool = False):
    t = _active_tx()
    if t is not None:  # внутри транзакции читаем свои же незафиксированные записи
        return await (t.fetchone if one else t.fetchall)(sql, params)
    c = await connect_read()
    cur = await c.execute(sql, params)
    rows = await cur.fetchone() if one else await cur.fetchall()
    await cur.close()
    return rows


async def batch(statements) -> None:
    """Все запросы пачки выполняются одной транзакцией: либо все, либо ни один."""
    async with tx() as t:
        for sql, params in statements:
            await t.execute(sql, params)


async def x(sql: str, params=()) -> int:
    """Одиночная запись, возвращает lastrowid."""
    t = _active_tx()
    if t is not None:
        await t.execute(sql, params)
        return t.lastrowid
    async with _writer():
        c = await connect()
        cur = await c.execute(sql, params)  # автокоммит: один запрос атомарен сам по себе
        last = cur.lastrowid
        await cur.close()
        return last
