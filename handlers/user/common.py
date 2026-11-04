from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_ID
from services import mods
from services.commands import help_sections
from utils.emoji import e
from utils.fmt import esc

router = Router()


@router.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Привет!</b>\n\nЯ игровой бот для группы. Все команды: /help\n"
        "<i>Для игр с ролями оставайся в этом чате: роли приходят сюда.</i>")


@router.message(Command("help"))
async def cmd_help(message: Message):
    uid = message.from_user.id if message.from_user else 0
    admin = uid == ADMIN_ID
    private = message.chat.type == "private"
    perms = None if admin else await mods.perms_of(uid)
    text = f"{e('info')} <b>Все команды</b>\n"
    for emoji, title, lines in help_sections(admin, private, perms):
        # Команды каждого раздела скрыты в раскрываемой HTML-цитате Telegram.
        text += (f"\n{e(emoji)} <b>{esc(title)}</b>\n"
                 f"<blockquote expandable>" + "\n".join(esc(l) for l in lines) + "</blockquote>")
    await message.answer(text)
