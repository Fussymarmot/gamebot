"""Фоновые задачи без потери ссылки.

Event loop хранит на задачи только слабые ссылки. Если сделать просто `asyncio.create_task(coro)` и
нигде не сохранить результат, сборщик мусора может уничтожить задачу посреди выполнения: таймер лобби
не сработает, наблюдатель дуэли не закроет партию, игроки навсегда останутся «заняты».
spawn() держит сильную ссылку до завершения и пишет в лог необработанные ошибки.
"""
import asyncio
from typing import Coroutine, Set

from utils.logs import logger

_tasks: Set[asyncio.Task] = set()


def _done(task: asyncio.Task):
    _tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Фоновая задача %s завершилась с ошибкой", task.get_name(), exc_info=exc)


def spawn(coro: Coroutine, name: str = "") -> asyncio.Task:
    task = asyncio.get_running_loop().create_task(coro, name=name or getattr(coro, "__name__", "task"))
    _tasks.add(task)
    task.add_done_callback(_done)
    return task


async def cancel_all():
    """Для корректной остановки: отменяет все фоновые задачи и ждёт их."""
    pending = [t for t in _tasks if not t.done()]
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
