import logging
import re
import time
from collections import defaultdict, deque
from typing import Any, Callable, Deque, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import ADMIN_ID, GROUP_ID
from database.settings import get_setting
from database.users import is_newbie
from services import mods, modtools

log = logging.getLogger(__name__)
LINK = re.compile(r"https?://|t\.me/|telegram\.me/|www\.|\b\w+\.(?:ru|com|net|org|me|io|xyz|ly)\b", re.I)
_times: Dict[int, Deque[float]] = defaultdict(deque)
_cooldown: Dict[int, float] = {}


def has_link(message: Message) -> bool:
    for ent in (message.entities or []) + (message.caption_entities or []):
        if ent.type in ("url", "text_link"):
            return True
    return bool(LINK.search(message.text or message.caption or ""))


async def notice(message: Message, text: str, markup=None):
    try:
        msg = await message.answer(text, reply_markup=markup)
    except Exception:
        return
    if markup is None:
        import asyncio

        async def later():
            await asyncio.sleep(10)
            try:
                await msg.delete()
            except Exception:
                pass
        asyncio.create_task(later())


class AutoModMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        user = event.from_user
        if event.chat.id != GROUP_ID or not user or user.is_bot or user.id == ADMIN_ID:
            return await handler(event, data)
        if (await get_setting("automod_on", "1")) != "1" or await mods.is_mod(user.id):
            return await handler(event, data)
        hours = int(await get_setting("automod_hours", "48"))
        newbie = await is_newbie(user.id, hours)
        name = user.first_name or user.username or "Участник"

        dq = _times[user.id]
        t = time.time()
        dq.append(t)
        while dq and t - dq[0] > 8:
            dq.popleft()
        limit = 5 if newbie else 10
        if len(dq) >= limit and t - _cooldown.get(user.id, 0) > 60:
            _cooldown[user.id] = t
            dq.clear()
            err = await modtools.mute(event.bot, 0, user.id, 10, "автомодерация: флуд")
            try:
                await event.delete()
            except Exception:
                pass
            if not err:
                await notice(event, modtools.muted_text(name, 10) + "\n<i>Причина: флуд.</i>",
                             modtools.appeal_kb(user.id))
            return
        if len(dq) >= limit:
            try:
                await event.delete()
            except Exception:
                pass
            return

        if newbie and has_link(event):
            try:
                await event.delete()
            except Exception:
                pass
            text, muted = await modtools.warn(event.bot, 0, user.id, name, "автомодерация: ссылка от новичка")
            await notice(event, text, modtools.appeal_kb(user.id) if muted else None)
            return
        return await handler(event, data)
