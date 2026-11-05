"""Пользователи, участники группы и люди, которые писали боту в личку."""
import time
from collections import OrderedDict
from datetime import timedelta
from typing import List, Optional

from database.core import q, x
from utils.timeutils import now, parse_ts, ts

# Ограниченные LRU-кэши: на больших группах память не растёт бесконечно.
_USER_CACHE_MAX = 20_000
_SEEN_CACHE_MAX = 10_000
_PRIVATE_SEEN_MAX = 20_000

_user_cache = OrderedDict()
_private_seen = OrderedDict()
_seen_cache = OrderedDict()


def _remember(cache, key, value, limit: int):
    cache[key] = value
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def clear_cache():
    _user_cache.clear()


async def save_user(user_id: int, username: str, first_name: str, last_name: str):
    key = (username, first_name, last_name)
    hit = _user_cache.get(user_id)
    if hit and hit[0] == key and time.time() - hit[1] < 3600:
        _user_cache.move_to_end(user_id)
        return
    await x("""INSERT INTO users (user_id, username, first_name, last_name, updated_at, first_seen)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET username=excluded.username,
               first_name=excluded.first_name, last_name=excluded.last_name,
               updated_at=excluded.updated_at""",
            (user_id, username, first_name, last_name, ts(), ts()))
    _remember(_user_cache, user_id, (key, time.time()), _USER_CACHE_MAX)


async def get_user_by_username(username: str) -> Optional[int]:
    row = await q("SELECT user_id FROM users WHERE LOWER(username)=LOWER(?)",
                  (username.lstrip("@"),), one=True)
    return row[0] if row else None


async def get_user_name(user_id: int) -> str:
    row = await q("SELECT first_name, username FROM users WHERE user_id=?", (user_id,), one=True)
    if row:
        return row[0] or (f"@{row[1]}" if row[1] else f"ID {user_id}")
    return f"ID {user_id}"


async def set_member(user_id: int, is_member: bool):
    await x("""INSERT INTO members (user_id, is_member, checked_at) VALUES (?,?,?)
               ON CONFLICT(user_id) DO UPDATE SET is_member=excluded.is_member,
               checked_at=excluded.checked_at""", (user_id, 1 if is_member else 0, ts()))


async def get_member_flag(user_id: int) -> Optional[bool]:
    row = await q("SELECT is_member FROM members WHERE user_id=?", (user_id,), one=True)
    return None if row is None else bool(row[0])


async def all_known_user_ids() -> List[int]:
    rows = await q("SELECT user_id FROM users UNION SELECT user_id FROM members")
    return [r[0] for r in rows]


async def count_members() -> int:
    row = await q("SELECT COUNT(*) FROM members WHERE is_member=1", one=True)
    return row[0]


async def register_private_user(user_id: int):
    if user_id in _private_seen:
        return
    _remember(_private_seen, user_id, True, _PRIVATE_SEEN_MAX)
    await x("INSERT OR IGNORE INTO private_users (user_id, first_seen, blocked) VALUES (?,?,0)",
            (user_id, ts()))
    await x("UPDATE private_users SET blocked=0 WHERE user_id=?", (user_id,))


async def mark_private_blocked(user_id: int):
    _private_seen.pop(user_id, None)
    await x("UPDATE private_users SET blocked=1 WHERE user_id=?", (user_id,))


async def private_recipients() -> List[int]:
    rows = await q("""SELECT p.user_id FROM private_users p
                      JOIN members m ON m.user_id = p.user_id
                      WHERE p.blocked = 0 AND m.is_member = 1""")
    return [r[0] for r in rows]


async def is_newbie(user_id: int, hours: int) -> bool:
    hit = _seen_cache.get(user_id)
    if hit is None or time.time() - hit[1] > 600:
        row = await q("SELECT first_seen FROM users WHERE user_id=?", (user_id,), one=True)
        hit = (row[0] if row else None, time.time())
        _remember(_seen_cache, user_id, hit, _SEEN_CACHE_MAX)
    else:
        _seen_cache.move_to_end(user_id)
    if not hit[0]:
        return False
    return now() - parse_ts(hit[0]) < timedelta(hours=hours)
