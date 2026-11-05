"""Журнал сообщений группы: буфер в памяти, пакетная запись и очистка.

Нужен для розыгрышей (условие по сообщениям) и для команды /clear."""
from datetime import timedelta
from typing import List

from database.core import tx, x
from utils.tasks import spawn
from utils.timeutils import now, ts

_msg_buf: List[tuple] = []
FLUSH_AT = 100          # столько записей в буфере запускают фоновую запись
_flushing = False


async def save_message(user_id: int, chat_id: int, text: str, message_id: int = 0):
    """Кладёт сообщение в буфер и сразу возвращается.

    Раньше при заполнении буфера запись в БД шла прямо здесь, то есть внутри обработчика сообщения или
    внутри запроса к Telegram (каждое сообщение бота тоже сюда попадает): игра ждала очереди на запись.
    Теперь запись идёт отдельной задачей, а по таймеру её подстраховывает планировщик (раз в 15 секунд).
    """
    global _flushing
    _msg_buf.append((user_id, chat_id, text, ts(), message_id))
    if len(_msg_buf) >= FLUSH_AT and not _flushing:
        _flushing = True
        spawn(_flush_bg(), name="flush_messages")


async def _flush_bg():
    global _flushing
    try:
        await flush_messages()
    except Exception:
        from utils.logs import logger
        logger.warning("Не удалось записать журнал сообщений", exc_info=True)
    finally:
        _flushing = False


async def flush_messages():
    global _msg_buf
    if not _msg_buf:
        return
    buf, _msg_buf = _msg_buf, []
    try:
        async with tx() as t:
            await t.executemany(
                "INSERT INTO messages (user_id, chat_id, text, created_at, message_id) VALUES (?,?,?,?,?)", buf)
    except BaseException:
        _msg_buf = buf + _msg_buf  # запись откатилась, сообщения не теряем
        raise


async def cleanup_old_messages():
    await x("DELETE FROM messages WHERE created_at < ?", (ts(now() - timedelta(days=90)),))

