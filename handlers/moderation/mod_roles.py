from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_ID
from database.moderation import log_mod
from services import mods
from services.modtools import name_of
from utils.fmt import code, esc
from utils.targets import parse_target

router = Router()


def usage(cmd: str, extra: str = "") -> str:
    return f"❌ Формат: {code(f'/{cmd} @username' + extra)} или {code(f'/{cmd} 123456789' + extra)}"


@router.message(Command("addmod"))
async def cmd_addmod(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    if not t or not t.id:
        await message.answer(usage("addmod", " [дни]"))
        return
    if t.id == ADMIN_ID or t.id == message.bot.id:
        await message.answer("❌ Это нельзя")
        return
    days = None
    if t.rest:
        word = t.rest.split()[0]
        if not word.isdigit() or not 1 <= int(word) <= 365:
            await message.answer("❌ Срок: число дней от 1 до 365, либо ничего (без срока).")
            return
        days = int(word)
    await mods.add(t.id, days, ADMIN_ID)
    await log_mod(ADMIN_ID, t.id, "addmod", f"{days} дн." if days else "без срока")
    await message.answer(f"🛡 <b>{esc(t.name)}</b> теперь модератор ({'на ' + str(days) + ' дн.' if days else 'без срока'}). "
                         "Доступны /mute, /unmute, /warn и просмотр /mutes, /warns.")


@router.message(Command("delmod"))
async def cmd_delmod(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    if not t or not t.id:
        await message.answer(usage("delmod"))
        return
    if not await mods.remove(t.id):
        await message.answer(f"{esc(t.name)} не модератор.")
        return
    await log_mod(ADMIN_ID, t.id, "delmod", "")
    await message.answer(f"✅ <b>{esc(t.name)}</b> больше не модератор.")


@router.message(Command("mods"))
async def cmd_mods(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    rows = await mods.listing()
    if not rows:
        await message.answer("Модераторов нет.")
        return
    text = "🛡 <b>Модераторы</b>\n\n"
    for uid, perms, until in rows:
        text += f"• <b>{esc(await name_of(uid))}</b> {code(str(uid))}: {mods.until_text(until)}\n"
    await message.answer(text)


@router.message(Command("weekly"))
async def cmd_weekly(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    from services import report_week
    await message.answer(await report_week.build())
