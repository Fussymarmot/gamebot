"""Диагностика задержек: в лог попадает всё, что дольше порога.

  SlowApiLog: запрос к Telegram (чистое время сети, внутри повторов RetryMiddleware);
  SlowCallbackLog: полное время обработки нажатия кнопки (все middleware + обработчик).
Порог задаётся переменной окружения SLOW_MS (по умолчанию 300 мс).
"""
import os
import time
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware

from utils.logs import logger

SLOW_MS = float(os.getenv("SLOW_MS", "300"))


class SlowApiLog(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        api = getattr(method, "__api_method__", type(method).__name__)
        if api == "getUpdates":
            return await make_request(bot, method)
        t = time.perf_counter()
        try:
            return await make_request(bot, method)
        finally:
            ms = (time.perf_counter() - t) * 1000
            if ms >= SLOW_MS:
                logger.warning("МЕДЛЕННО: запрос %s к Telegram занял %.0f мс", api, ms)


class SlowCallbackLog(BaseMiddleware):
    async def __call__(self, handler: Callable, event, data: Dict[str, Any]) -> Any:
        t = time.perf_counter()
        try:
            return await handler(event, data)
        finally:
            ms = (time.perf_counter() - t) * 1000
            if ms >= SLOW_MS:
                logger.warning("МЕДЛЕННО: кнопка %r обработана за %.0f мс", getattr(event, "data", None), ms)
