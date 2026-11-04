from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_ID

router = Router()


@router.message(Command("emoji_id"))
async def cmd_emoji_id(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    ents = [en for en in (message.entities or []) if en.type == "custom_emoji"]
    if not ents:
        await message.answer("Отправь команду вместе с премиум-эмодзи: <code>/emoji_id 😀</code>")
        return
    out = "<b>ID премиум-эмодзи:</b>\n\n"
    for en in ents:
        out += f"<code>{en.custom_emoji_id}</code>\n"
    await message.answer(out)
