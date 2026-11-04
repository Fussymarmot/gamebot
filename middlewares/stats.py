"""Журнал сообщений группы (для розыгрышей и /clear) и проверка тегов.

Сами люди регистрируются в middlewares/register.py."""
import asyncio
import logging
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from config import GROUP_ID
from database.messages import save_message
from database.users import save_user, set_member
from services import members, tags

log = logging.getLogger(__name__)


class StatsMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        user = event.from_user
        if event.chat.id == GROUP_ID and user and not user.is_bot:
            try:
                await save_user(user.id, user.username or "", user.first_name or "",
                                user.last_name or "")
                await save_message(user.id, event.chat.id, event.text or event.caption or "", event.message_id)
                hit = members._cache.get(user.id)
                if not hit or not hit[0]:
                    members.remember(user.id, True)
                    await set_member(user.id, True)
                if user.id in tags.perm_cache:
                    asyncio.create_task(
                        tags.enforce(event.bot, user.id, getattr(event, "sender_tag", None)))
            except Exception:
                log.exception("Ошибка записи статистики")
        return await handler(event, data)
