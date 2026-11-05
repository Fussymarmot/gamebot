"""Регистрация людей: любое действие в группе заносит человека в базу.

Подключается на уровень Update, то есть раньше всех остальных middleware и для всех типов событий:
сообщения (в том числе правки и сервисные), нажатия кнопок, реакции, вход и выход участников.
Благодаря этому розыгрыши и проверки членства видят всех, а не только тех, кто писал текст.
"""
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Update

from config import GROUP_ID
from services import members
from utils.logs import logger



async def register_update(update: Update, data: Dict[str, Any]):
    chat = data.get("event_chat")
    user = data.get("event_from_user")

    cm = update.chat_member
    if cm is not None:
        if cm.chat.id == GROUP_ID:
            await members.register(cm.new_chat_member.user, members._status_ok(cm.new_chat_member))
        return

    msg = update.message or update.edited_message
    left = msg.left_chat_member if msg else None
    if msg is not None and msg.chat.id == GROUP_ID:
        for joined in msg.new_chat_members or []:
            await members.register(joined, True)
        if left is not None:
            await members.register(left, False)

    if chat is not None and chat.id == GROUP_ID and user is not None:
        if left is not None and left.id == user.id:
            return
        await members.register(user, True)


class RegisterMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Update, data: Dict[str, Any]) -> Any:
        try:
            await register_update(event, data)
        except Exception:
            logger.exception("Не удалось зарегистрировать обновление в базе")
        return await handler(event, data)
