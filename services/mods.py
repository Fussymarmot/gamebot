import time
from datetime import timedelta
from typing import Dict, List, Optional, Set, Tuple

from config import ADMIN_ID
from database.core import q, x
from utils.timeutils import now, parse_ts, ts

ALL_PERMS = ("mute", "warn")
PERM_NAMES = {"mute": "муты", "warn": "предупреждения"}


# Кэш строк таблицы moderators на короткое время: права проверяются на каждое сообщение группы,
# и раньше каждая проверка была отдельным запросом к БД. Срок «until» проверяется при каждом обращении.
_CACHE_TTL = 30.0
_cache: Dict[int, Tuple[Optional[tuple], float]] = {}


def clear_cache(uid: Optional[int] = None):
    if uid is None:
        _cache.clear()
    else:
        _cache.pop(uid, None)


async def _row(uid: int) -> Optional[tuple]:
    hit = _cache.get(uid)
    if hit and hit[1] > time.monotonic():
        return hit[0]
    row = await q("SELECT perms, until FROM moderators WHERE user_id=?", (uid,), one=True)
    row = tuple(row) if row else None
    if len(_cache) > 5000:
        _cache.clear()
    _cache[uid] = (row, time.monotonic() + _CACHE_TTL)
    return row


async def perms_of(uid: int) -> Optional[Set[str]]:
    row = await _row(uid)
    if not row:
        return None
    if row[1] and parse_ts(row[1]) <= now():
        await x("DELETE FROM moderators WHERE user_id=?", (uid,))
        clear_cache(uid)
        return None
    return {p for p in (row[0] or "").split(",") if p}


async def has(uid: int, perm: str) -> bool:
    if uid == ADMIN_ID:
        return True
    perms = await perms_of(uid)
    return bool(perms and perm in perms)


async def is_mod(uid: int) -> bool:
    return bool(await perms_of(uid))


async def add(uid: int, days: Optional[int], by: int, perms=ALL_PERMS):
    until = ts(now() + timedelta(days=days)) if days else None
    await x("INSERT OR REPLACE INTO moderators (user_id, perms, until, added_by, created_at) VALUES (?,?,?,?,?)",
            (uid, ",".join(perms), until, by, ts()))
    clear_cache(uid)


async def remove(uid: int) -> bool:
    row = await q("SELECT 1 FROM moderators WHERE user_id=?", (uid,), one=True)
    await x("DELETE FROM moderators WHERE user_id=?", (uid,))
    clear_cache(uid)
    return bool(row)


async def listing() -> List[tuple]:
    await x("DELETE FROM moderators WHERE until IS NOT NULL AND until <= ?", (ts(),))
    clear_cache()
    return await q("SELECT user_id, perms, until FROM moderators ORDER BY created_at")


def until_text(until: Optional[str]) -> str:
    return "без срока" if not until else "до " + parse_ts(until).strftime("%d.%m.%Y %H:%M")
