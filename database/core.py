"""Соединение с SQLite и базовые помощники запросов: q (чтение), x (запись), batch (пачка записей)."""
import os
from typing import Optional

import aiosqlite

from config import BASE_DIR

DB_PATH = os.path.join(BASE_DIR, "bot.db")

_conn: Optional[aiosqlite.Connection] = None


async def connect() -> aiosqlite.Connection:
    global _conn
    if _conn is None:
        _conn = await aiosqlite.connect(DB_PATH)
        await _conn.execute("PRAGMA journal_mode=WAL")
        await _conn.execute("PRAGMA synchronous=NORMAL")
        await _conn.commit()
    return _conn


async def disconnect():
    global _conn
    if _conn is not None:
        await _conn.close()
        _conn = None


async def q(sql: str, params=(), one: bool = False):
    c = await connect()
    cur = await c.execute(sql, params)
    rows = await cur.fetchone() if one else await cur.fetchall()
    await cur.close()
    return rows


async def batch(statements) -> None:
    c = await connect()
    for sql, params in statements:
        cur = await c.execute(sql, params)
        await cur.close()
    await c.commit()


async def x(sql: str, params=()) -> int:
    c = await connect()
    cur = await c.execute(sql, params)
    await c.commit()
    last = cur.lastrowid
    await cur.close()
    return last
