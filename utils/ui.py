import logging
from typing import Optional

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from utils.emoji import CHAR_IDS, EMOJI

log = logging.getLogger(__name__)
ICONS_ENABLED = False


def _norm(ch: str) -> str:
    return ch.replace("\ufe0f", "").replace("\ufe0e", "")


_CHAR2ID = CHAR_IDS


def set_icons(enabled: bool):
    global ICONS_ENABLED
    ICONS_ENABLED = enabled


def btn(text: str, data: Optional[str] = None, url: Optional[str] = None,
        icon: bool = True) -> InlineKeyboardButton:
    params = {}
    if icon and ICONS_ENABLED and text:
        first = _norm(text[0])
        rest = text[1:].lstrip("\ufe0f\ufe0e").strip()
        if first in _CHAR2ID and rest:
            params["icon_custom_emoji_id"] = _CHAR2ID[first]
            text = rest
    return InlineKeyboardButton(text=text, callback_data=data, url=url, **params)


def iconize(b: InlineKeyboardButton) -> InlineKeyboardButton:
    text = b.text or ""
    if getattr(b, "icon_custom_emoji_id", None) or not text:
        return b
    first = _norm(text[0])
    rest = text[1:].lstrip("\ufe0f\ufe0e").strip()
    if first in _CHAR2ID and rest:
        return b.model_copy(update={"text": rest, "icon_custom_emoji_id": _CHAR2ID[first]})
    return b


def name_rows(pairs, make_data, per_row: int = 2, limit: int = 20):
    rows, row = [], []
    for key, name in pairs:
        row.append(InlineKeyboardButton(text=name[:limit], callback_data=make_data(key)))
        if len(row) == per_row:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn(t, d) for t, d in r] for r in rows])


def probe_markup() -> InlineKeyboardMarkup:
    eid = EMOJI["check"][0]
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="ok", callback_data="probe", icon_custom_emoji_id=eid)]])


async def probe_icons(bot) -> bool:
    from aiogram.exceptions import TelegramBadRequest
    from config import ADMIN_ID
    try:
        m = await bot.send_message(ADMIN_ID, "⚙️ проверка иконок", reply_markup=probe_markup())
        try:
            await m.delete()
        except Exception:
            pass
        set_icons(True)
        return True
    except TelegramBadRequest as ex:
        log.warning("Иконки на кнопках не приняты Telegram: %s", ex)
    except Exception as ex:
        log.warning("Проверка иконок не удалась: %s", ex)
    set_icons(False)
    return False
