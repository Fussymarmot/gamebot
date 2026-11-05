from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from games import core as games_core, pause
from utils.logs import logger
from utils.ui import kb

router = Router()


def resume_markup():
    return kb([[("▶️ Продолжить", "pause:resume")]])


async def do_pause(bot):
    if pause.is_paused():
        return
    pause.pause()
    text = "⏸ <b>Игры на паузе</b>\n\n<i>Таймеры остановлены. Админ продолжит игру.</i>"
    try:
        pause.state["msg"] = await bot.send_message(GROUP_ID, text, reply_markup=resume_markup())
    except Exception:
        logger.warning("Не удалось объявить паузу игр", exc_info=True)
        pause.state["msg"] = None


async def do_resume(bot):
    if not pause.is_paused():
        return
    pause.resume()
    msg = pause.state.get("msg")
    pause.state["msg"] = None
    text = "▶️ <b>Игры продолжаются</b>"
    if msg:
        try:
            await msg.edit_text(text)
            return
        except Exception:
            logger.debug("Не удалось отредактировать сообщение паузы", exc_info=True)
    try:
        await bot.send_message(GROUP_ID, text)
    except Exception:
        logger.debug("Не удалось отправить сообщение о снятии паузы", exc_info=True)


@router.message(Command("pause"))
async def cmd_pause(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    if pause.is_paused():
        await message.answer("Игры уже на паузе. Продолжить: /resume")
        return
    await do_pause(message.bot)


@router.message(Command("resume"))
async def cmd_resume(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    if not pause.is_paused():
        await message.answer("Игры не на паузе.")
        return
    await do_resume(message.bot)


@router.callback_query(F.data == "pause:resume")
async def resume_cb(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Продолжить игру может только админ", show_alert=True)
        return
    await do_resume(cb.bot)
    await cb.answer()
