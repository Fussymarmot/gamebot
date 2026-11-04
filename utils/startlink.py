"""Helpers for asking a user to open the bot's private chat."""
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup


async def prompt_start(cb: CallbackQuery, payload: str = "game") -> None:
    """Tell the user to press Start and provide a deep-link button when possible.

    A player's own Telegram username is not required. The link uses the bot's
    username; if Telegram does not return one, avoid constructing an invalid URL.
    """
    await cb.answer("Сначала открой личный чат с ботом и нажми Start", show_alert=True)
    message = cb.message
    if message is None:
        return

    me = await cb.bot.me()
    if me.username:
        markup = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="▶️ Открыть бота",
                url=f"https://t.me/{me.username}?start={payload}",
            )
        ]])
        await message.answer(
            "Чтобы играть, сначала запусти бота в личных сообщениях:",
            reply_markup=markup,
        )
    else:
        await message.answer(
            "Не удалось сформировать ссылку на бота. Найди бота в Telegram "
            "по его имени, открой чат и нажми Start."
        )
