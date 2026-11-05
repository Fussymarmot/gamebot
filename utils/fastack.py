"""Быстрый ответ на нажатия кнопок.

Проблема: обработчики вызывали cb.answer() в самом конце, после работы с БД и правки сообщения
(несколько запросов к Telegram подряд). Всё это время на кнопке крутились «часики», а тост вроде
«Поле готово!» появлялся раньше, чем клиент успевал показать новое сообщение.

Решение (работает для всех кнопок без правки обработчиков):
  * AutoAckMiddleware: если обработчик не ответил на кнопку за ACK_DELAY секунд (или закончил,
    так и не ответив), бот сам отвечает пустым answerCallbackQuery, и «часики» исчезают;
  * AnswerOnceMiddleware (запросный): повторный ответ на тот же callback_query_id молча пропускается,
    поэтому поздний cb.answer("текст") из обработчика не падает с «query is too old / already answered».
Быстрые ответы (алерты «Это поле другого игрока» и т. п.) приходят раньше таймера и работают как раньше.
"""
import asyncio
from collections import OrderedDict
from typing import Any, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramAPIError
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery

from utils.logs import logger

ACK_DELAY = 0.4        # столько ждём «настоящего» ответа обработчика, потом отвечаем сами
_MAX_TRACKED = 4000

_answered: "OrderedDict[str, None]" = OrderedDict()


def _mark(cb_id: str) -> bool:
    """True, если мы первые отвечаем на этот callback."""
    if cb_id in _answered:
        return False
    _answered[cb_id] = None
    while len(_answered) > _MAX_TRACKED:
        _answered.popitem(last=False)
    return True


class AnswerOnceMiddleware(BaseRequestMiddleware):
    """Внешний запросный middleware: на один callback_query отправляется только один ответ."""

    async def __call__(self, make_request, bot, method):
        if isinstance(method, AnswerCallbackQuery):
            cb_id = method.callback_query_id
            if not _mark(cb_id):
                return True                      # уже отвечали: повтор не нужен
            try:
                return await make_request(bot, method)
            except BaseException:
                _answered.pop(cb_id, None)       # запрос не дошёл: можно попробовать ещё раз
                raise
        return await make_request(bot, method)


async def _ack(event: CallbackQuery):
    try:
        await event.answer()
    except TelegramAPIError:
        pass                                     # «query is too old» и т. п.: не страшно
    except Exception:
        logger.debug("Не удалось быстро ответить на кнопку", exc_info=True)


async def _ack_later(event: CallbackQuery):
    await asyncio.sleep(ACK_DELAY)
    if event.id not in _answered:
        await _ack(event)


class AutoAckMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable, event: CallbackQuery, data: Dict[str, Any]) -> Any:
        timer = asyncio.get_running_loop().create_task(_ack_later(event))
        try:
            return await handler(event, data)
        finally:
            if not timer.done():
                timer.cancel()
            if event.id not in _answered:        # обработчик закончил, не ответив
                await _ack(event)
