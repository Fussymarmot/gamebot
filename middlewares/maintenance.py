"""Режим технических работ: отвечает только админу, остальным показывает уведомление."""
import time
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery

from config import ADMIN_ID, GROUP_ID
from services import maintenance
from utils.logs import logger

_notice_at: Dict[Any, float] = {}


class MaintenanceMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event, data: Dict[str, Any]) -> Any:
        if not maintenance.is_on():
            return await handler(event, data)
        user = event.from_user
        if user and user.id == ADMIN_ID:
            return await handler(event, data)
        if isinstance(event, CallbackQuery):
            try:
                await event.answer(maintenance.notice(short=True)[:190], show_alert=True)
            except Exception:
                logger.debug("Не удалось показать уведомление о техработах", exc_info=True)
            return
        in_group = event.chat.id == GROUP_ID
        if in_group and not (event.text and event.text.startswith("/")):
            return
        key = event.chat.id if in_group else user.id
        gap = 60 if in_group else 30
        if time.time() - _notice_at.get(key, 0) > gap:
            _notice_at[key] = time.time()
            await event.answer(maintenance.notice())
        return
