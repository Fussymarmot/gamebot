"""Повтор запросов к Telegram при лимитах и сетевых сбоях.

Что происходило раньше: при частых сообщениях (большая игра, рассылка, проверка участников) Telegram отвечал
429 «слишком часто». Исключение TelegramRetryAfter поднималось прямо в игровой цикл покера, блэкджека,
рулетки и т. д., цикл падал, игра останавливалась, а «зависшие» игроки оставались занятыми.
Теперь запрос просто ждёт указанное Telegram время и повторяется.

Повторяются:
  * TelegramRetryAfter: всегда (запрос Telegram не выполнял, повтор безопасен);
  * TelegramServerError (5xx) и сетевые сбои только для методов, у которых повтор не создаёт дубль
    (правки, ответы на кнопки, удаления, проверки), но не для sendMessage и подобных.
"""
import asyncio
from typing import Any

from aiogram import Bot
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.methods import TelegramMethod
from aiogram.methods.base import TelegramType

from utils.logs import logger

MAX_RETRY_AFTER = 20        # дольше этого не ждём: пусть вызывающий код решает сам
RETRY_AFTER_ATTEMPTS = 3
NET_ATTEMPTS = 3

# Методы без побочных эффектов при повторе: повтор не создаст дубль сообщения.
IDEMPOTENT = {
    "answerCallbackQuery", "editMessageText", "editMessageReplyMarkup", "editMessageCaption", "deleteMessage",
    "getChatMember", "getChat", "getChatAdministrators", "getChatMemberCount", "getMe", "sendChatAction",
    "restrictChatMember", "setMyCommands", "setChatMemberTag", "unpinChatMessage",
}


class RetryMiddleware(BaseRequestMiddleware):
    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Any:
        api = getattr(method, "__api_method__", "")
        if api == "getUpdates":      # у поллинга своя логика повторов
            return await make_request(bot, method)
        retry_after_left, net_left = RETRY_AFTER_ATTEMPTS, NET_ATTEMPTS
        while True:
            try:
                return await make_request(bot, method)
            except TelegramRetryAfter as ex:
                retry_after_left -= 1
                if retry_after_left < 0 or ex.retry_after > MAX_RETRY_AFTER:
                    raise
                logger.warning("Telegram просит подождать %s с (%s), повторяю", ex.retry_after, api)
                await asyncio.sleep(ex.retry_after + 0.3)
            except (TelegramNetworkError, TelegramServerError) as ex:
                net_left -= 1
                if api not in IDEMPOTENT or net_left <= 0:
                    raise
                logger.warning("Сбой сети при вызове %s (%s), повторяю", api, type(ex).__name__)
                await asyncio.sleep(0.5 * (NET_ATTEMPTS - net_left))
