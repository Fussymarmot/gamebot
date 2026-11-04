from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID
from utils import richui

router = Router()


@router.message(Command("richtest"))
async def cmd_richtest(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        return
    cells = [[richui.btn(str(r * 3 + c + 1), f"rt:{r}{c}", "primary" if (r + c) % 2 else None) for c in range(3)]
             for r in range(3)]
    body = ("<h3>Проверка Rich-сообщений</h3><p>Если ты видишь таблицу с кнопками, Rich-режим работает.</p>"
            + richui.table(["A", "B", "C"], ["1", "2", "3"], cells)
            + richui.row([richui.btn("Всё отлично", "rt:ok", "success")]))
    data = await richui.call("sendRichMessage", {"chat_id": message.chat.id, "rich_message": {"html": body}})
    if not data:
        await message.answer("❌ Telegram отклонил Rich-сообщение. Подробности в bot.log. "
                             "Игры останутся на обычных кнопках.")


@router.callback_query(F.data.startswith("rt:"))
async def richtest_cb(cb: CallbackQuery):
    await cb.answer("Кнопки в таблице работают ✅", show_alert=True)
