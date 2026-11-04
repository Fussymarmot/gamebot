"""Отключённые админом команды отвечают «функция отключена»."""
import time
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import Message

from services import features

_feature_notice: Dict[Any, float] = {}


class FeatureMiddleware(BaseMiddleware):

    async def __call__(self, handler: Callable, event: Message, data: Dict[str, Any]) -> Any:
        text = event.text or ""
        if text.startswith("/"):
            name = text[1:].split(maxsplit=1)[0].split("@")[0].lower() if len(text) > 1 else ""
            if name and not features.is_on(name):
                key = (event.chat.id, name)
                if time.time() - _feature_notice.get(key, 0) > 60:
                    _feature_notice[key] = time.time()
                    await event.answer("🚫 Эта функция отключена.")
                return
        return await handler(event, data)
