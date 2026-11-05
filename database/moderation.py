"""Чёрный список игр и журнал действий модерации."""
import time
from typing import Dict, Tuple

from database.core import q, x
from utils.timeutils import ts

# Проверка бана идёт на каждое действие в играх (вход в лобби, ставка, запуск). Кэш на 60 секунд;
# ban_user / unban_user сразу обновляют его, так что бан действует мгновенно.
_BAN_TTL = 60.0
_ban_cache: Dict[int, Tuple[bool, float]] = {}


def clear_cache():
    _ban_cache.clear()


async def is_user_banned(user_id: int) -> bool:
    hit = _ban_cache.get(user_id)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    banned = bool(await q("SELECT 1 FROM banned WHERE user_id=?", (user_id,), one=True))
    if len(_ban_cache) > 5000:
        _ban_cache.clear()
    _ban_cache[user_id] = (banned, time.monotonic() + _BAN_TTL)
    return banned


async def ban_user(user_id: int):
    await x("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (user_id,))
    _ban_cache[user_id] = (True, time.monotonic() + _BAN_TTL)


async def unban_user(user_id: int):
    await x("DELETE FROM banned WHERE user_id=?", (user_id,))
    _ban_cache[user_id] = (False, time.monotonic() + _BAN_TTL)


async def log_mod(admin_id: int, target_id: int, action: str, details: str = ""):
    await x("INSERT INTO mod_log (admin_id, target_id, action, details, created_at) VALUES (?,?,?,?,?)",
            (admin_id, target_id, action, details, ts()))
