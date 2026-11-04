"""Точка входа: python main.py"""
import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import middlewares
from config import ADMIN_ID, BASE_DIR, BOT_TOKEN
from database.lifecycle import close as close_db
from database.schema import init_db
from database.settings import get_setting
from games import core as games_core
from games import gamemute
from handlers import get_routers
from handlers.economy import economy
from services import features, maintenance, tags
from services.commands import set_commands
from services.economy import load_shop
from services.scheduler import scheduler, start_scheduler
from utils import ui
from utils.premium import PremiumEmoji

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(os.path.join(BASE_DIR, "bot.log"), maxBytes=1_000_000,
                                  backupCount=3, encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("bot")


async def on_startup(bot: Bot):
    await init_db()
    await maintenance.load()
    await features.load()
    await load_shop()
    await tags.load_cache()
    gamemute.set_bot(bot)
    await gamemute.restore_after_restart(bot, games_core.registry.keys())
    economy.setup(bot)
    if (await get_setting("button_icons", "1")) != "0":
        await ui.probe_icons(bot)
    await set_commands(bot)
    start_scheduler(bot)
    me = await bot.me()
    log.info("Бот запущен: @%s (иконки на кнопках: %s)", me.username, "вкл" if ui.ICONS_ENABLED else "выкл")
    if not me.can_read_all_group_messages:
        # Режим приватности включён: в группе бот получает только команды и ответы на свои сообщения,
        # поэтому слова и числа в Ассоциациях, WordSeek, «Взломай код» и т.д. он не увидит.
        log.warning("Включён режим приватности: бот не видит обычные сообщения в группе")
        try:
            await bot.send_message(
                ADMIN_ID, "⚠️ <b>У бота включён режим приватности.</b>\n\nИз-за него в группе он видит только "
                          "команды и ответы на свои сообщения, поэтому игры со словами и числами "
                          "(Ассоциации, WordSeek, Взломай код, Мемори) не заметят обычные сообщения.\n\n"
                          "Как исправить: @BotFather → /mybots → бот → Bot Settings → Group Privacy → "
                          "<b>Turn off</b>. Потом удали бота из группы и добавь снова (или просто сделай его "
                          "админом) и перезапусти.")
        except Exception:
            log.info("Не удалось отправить предупреждение о режиме приватности админу")


async def on_shutdown(bot: Bot):
    if scheduler.running:
        scheduler.shutdown(wait=False)
    await close_db()
    log.info("Бот остановлен")


async def main():
    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    bot.session.middleware(PremiumEmoji())
    dp = Dispatcher()

    middlewares.setup(dp)
    for router in get_routers():
        dp.include_router(router)

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    try:
        # chat_member и message_reaction нужны, чтобы видеть вход людей и реакции (боту нужны права админа)
        updates = set(dp.resolve_used_update_types()) | {
            "message", "edited_message", "callback_query", "chat_member", "message_reaction"}
        await dp.start_polling(bot, allowed_updates=sorted(updates))
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
