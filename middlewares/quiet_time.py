"""Тихое время: в это окно команды в группе не обрабатываются (кроме админа)."""
import time
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import ADMIN_ID, GROUP_ID
from services.quiet import quiet_state

_last_notice: Dict[int, float] = {}


class QuietTimeMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        if event.chat.id == GROUP_ID and event.text and event.text.startswith("/"):
            quiet, end = await quiet_state()
            if quiet and not (event.from_user and event.from_user.id == ADMIN_ID):
                if time.time() - _last_notice.get(event.chat.id, 0) > 600:
                    _last_notice[event.chat.id] = time.time()
                    await event.answer(f"🌙 Тихое время до {end:02d}:00 по Москве")
                return
        return await handler(event, data)
