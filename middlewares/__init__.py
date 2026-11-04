"""Middleware бота. setup() подключает их к диспетчеру в нужном порядке."""
from aiogram import Dispatcher


def setup(dp: Dispatcher):
    from middlewares.access import CallbackGuardMiddleware, GroupCheckMiddleware
    from middlewares.automod import AutoModMiddleware
    from middlewares.features import FeatureMiddleware
    from middlewares.maintenance import MaintenanceMiddleware
    from middlewares.quiet_time import QuietTimeMiddleware
    from middlewares.ratelimit import RateLimitMiddleware
    from middlewares.register import RegisterMiddleware
    from middlewares.stats import StatsMiddleware

    # Регистрация идёт первой и на всех типах событий: даже заблокированные антиспамом сообщения,
    # нажатия кнопок и реакции заносят человека в базу.
    dp.update.outer_middleware(RegisterMiddleware())
    dp.message.outer_middleware(GroupCheckMiddleware())
    dp.message.outer_middleware(MaintenanceMiddleware())
    dp.message.outer_middleware(RateLimitMiddleware())
    dp.message.outer_middleware(FeatureMiddleware())
    dp.message.outer_middleware(AutoModMiddleware())
    dp.message.outer_middleware(StatsMiddleware())
    dp.message.outer_middleware(QuietTimeMiddleware())
    dp.callback_query.outer_middleware(CallbackGuardMiddleware())
    dp.callback_query.outer_middleware(MaintenanceMiddleware())
