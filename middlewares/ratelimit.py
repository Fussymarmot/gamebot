import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import ADMIN_ID

CMD_LIMIT, CMD_WINDOW = 20, 60
LOBBY_LIMIT, LOBBY_WINDOW = 5, 60
REPEAT_LIMIT, REPEAT_WINDOW = 3, 5

GAME_CMDS = {"ttt", "rps", "duel", "mafia", "werewolf", "memory", "code", "whomost", "assoc", "games",
             "top_games", "stopgame", "stopmafia", "roulette", "checkers", "wordseek", "seabattle", "hangman"}
LOBBY_CMDS = {"mafia", "werewolf", "memory", "code", "whomost", "assoc", "roulette", "wordseek"}

_cmds: Dict[int, Deque[float]] = defaultdict(deque)
_lobbies: Dict[int, Deque[float]] = defaultdict(deque)
_repeat: Dict[int, Deque[tuple]] = defaultdict(deque)
_warned: Dict[int, float] = {}


def _hit(store: Dict[int, Deque], uid: int, limit: int, window: int) -> bool:
    now = time.time()
    dq = store[uid]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return False
    dq.append(now)
    return True


def purge():
    now = time.time()
    for store, window in ((_cmds, CMD_WINDOW), (_lobbies, LOBBY_WINDOW)):
        for uid in list(store):
            dq = store[uid]
            while dq and now - dq[0] > window:
                dq.popleft()
            if not dq:
                del store[uid]
    for uid in list(_repeat):
        if not _repeat[uid] or now - _repeat[uid][-1][1] > REPEAT_WINDOW:
            del _repeat[uid]
    for uid in [u for u, t in _warned.items() if now - t > 120]:
        del _warned[uid]


def allow_lobby(uid: int) -> bool:
    return uid == ADMIN_ID or _hit(_lobbies, uid, LOBBY_LIMIT, LOBBY_WINDOW)


def command_name(text: str) -> str:
    return text[1:].split()[0].split("@")[0].lower() if text and text.startswith("/") else ""


class RateLimitMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        user = event.from_user
        cmd = command_name(event.text or "")
        if not user or user.id == ADMIN_ID or cmd not in GAME_CMDS:
            return await handler(event, data)
        now = time.time()
        rep = _repeat[user.id]
        while rep and now - rep[0][1] > REPEAT_WINDOW:
            rep.popleft()
        rep.append((cmd, now))
        if sum(1 for c, _ in rep if c == cmd) > REPEAT_LIMIT:
            return
        ok = _hit(_cmds, user.id, CMD_LIMIT, CMD_WINDOW)
        if ok and cmd in LOBBY_CMDS:
            ok = _hit(_lobbies, user.id, LOBBY_LIMIT, LOBBY_WINDOW)
        if not ok:
            if now - _warned.get(user.id, 0) > 30:
                _warned[user.id] = now
                await event.answer("⏳ Слишком много действий. Попробуй немного позже.")
            return
        return await handler(event, data)
