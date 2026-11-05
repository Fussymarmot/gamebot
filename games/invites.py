"""Занятость игроков: один человек не может быть сразу в двух играх на двоих."""
import time
from typing import Dict

from utils.logs import logger

BUSY_TTL = 2 * 60 * 60      # страховка от «вечно занятых» после сбоя: через 2 часа бронь снимается
busy: Dict[int, str] = {}
_expires: Dict[int, float] = {}
RELEASE_HOOKS: list = []     # f(ids): вызывается при освобождении игроков (эскроу дуэлей)


def _hook(ids):
    for h in RELEASE_HOOKS:
        try:
            h(ids)
        except Exception:
            logger.exception("Ошибка в обработчике освобождения игроков %s", ids)


def release(*ids: int):
    for i in ids:
        busy.pop(i, None)
        _expires.pop(i, None)
    _hook(ids)


def _purge(uid: int) -> bool:
    exp = _expires.get(uid)
    if exp is not None and exp <= time.monotonic():
        logger.warning("Снята просроченная бронь игрока %s (tag=%s)", uid, busy.get(uid))
        release(uid)
        return True
    return False


def purge_expired() -> int:
    """Снимает все просроченные брони. Вызывается по таймеру, чтобы не ждать, пока игрок сам придёт."""
    now = time.monotonic()
    stale = [u for u, exp in list(_expires.items()) if exp <= now]
    for uid in stale:
        logger.warning("Снята просроченная бронь игрока %s (tag=%s)", uid, busy.get(uid))
        release(uid)
    return len(stale)


def is_busy(uid: int) -> bool:
    _purge(uid)
    return uid in busy


def hold(uid: int, tag: str) -> bool:
    _purge(uid)
    if uid in busy:
        return False
    busy[uid] = tag
    _expires[uid] = time.monotonic() + BUSY_TTL
    return True


def acquire(a: int, b: int, tag: str) -> bool:
    _purge(a)
    _purge(b)
    if a in busy or b in busy:
        return False
    busy[a] = busy[b] = tag
    deadline = time.monotonic() + BUSY_TTL
    _expires[a] = _expires[b] = deadline
    return True
