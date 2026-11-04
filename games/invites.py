"""Занятость игроков: один человек не может быть сразу в двух играх на двоих."""
from typing import Dict

busy: Dict[int, str] = {}


def is_busy(uid: int) -> bool:
    return uid in busy


def hold(uid: int, tag: str) -> bool:
    if uid in busy:
        return False
    busy[uid] = tag
    return True


def acquire(a: int, b: int, tag: str) -> bool:
    if a in busy or b in busy:
        return False
    busy[a] = busy[b] = tag
    return True


def release(*ids: int):
    for i in ids:
        busy.pop(i, None)
