"""Доступ: бот работает только в основной группе и в личке с её участниками."""
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message

from config import GROUP_ID
from database.users import register_private_user
from services import members


class GroupCheckMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        chat = event.chat
        user = event.from_user
        if chat.type == "private":
            if not user or user.is_bot:
                return
            if await members.is_member(event.bot, user.id):
                await register_private_user(user.id)
                return await handler(event, data)
            return
        if chat.id == GROUP_ID:
            return await handler(event, data)
        try:
            await event.bot.leave_chat(chat.id)
        except Exception:
            pass
        return


class CallbackGuardMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: CallbackQuery, data: Dict[str, Any]) -> Any:
        msg = event.message
        user = event.from_user
        if msg is None:
            return await handler(event, data)
        if msg.chat.id == GROUP_ID:
            return await handler(event, data)
        if msg.chat.type == "private" and user and await members.is_member(event.bot, user.id):
            await register_private_user(user.id)
            return await handler(event, data)
        try:
            await event.answer()
        except Exception:
            pass
        return
