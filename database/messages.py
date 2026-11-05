"""Журнал сообщений группы: буфер в памяти, пакетная запись и очистка.

Нужен для розыгрышей (условие по сообщениям) и для команды /clear."""
from datetime import timedelta
from typing import List

from database.core import tx, x
from utils.timeutils import now, ts

_msg_buf: List[tuple] = []


async def save_message(user_id: int, chat_id: int, text: str, message_id: int = 0):
    _msg_buf.append((user_id, chat_id, text, ts(), message_id))
    if len(_msg_buf) >= 50:
        await flush_messages()


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

