import re

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, Message

from config import GROUP_ID
from database.messages import save_message
from utils import ui
from utils.emoji import CHAR_IDS

VARIATION = "\ufe0f\ufe0e"
TAG = re.compile(r"(<[^>]*>)")


def norm(s: str) -> str:
    return s.replace("\ufe0f", "").replace("\ufe0e", "")


def build_pattern():
    parts = []
    for key in sorted(CHAR_IDS, key=len, reverse=True):
        parts.append("[\ufe0f\ufe0e]?".join(re.escape(ch) for ch in key) + "[\ufe0f\ufe0e]?")
    return re.compile("|".join(parts)) if parts else None


PATTERN = build_pattern()


def wrap(match) -> str:
    raw = match.group(0)
    return f'<tg-emoji emoji-id="{CHAR_IDS[norm(raw)]}">{raw}</tg-emoji>'


def convert(text: str) -> str:
    if not text or PATTERN is None:
        return text
    out, skip = [], 0
    for part in TAG.split(text):
        if part.startswith("<") and part.endswith(">"):
            low = part.lower()
            if low.startswith(("<tg-emoji", "<code", "<pre")):
                skip += 1
            elif low.startswith(("</tg-emoji", "</code", "</pre")):
                skip = max(0, skip - 1)
            out.append(part)
        elif skip:
            out.append(part)
        else:
            out.append(PATTERN.sub(wrap, part))
    return "".join(out)


async def remember(result):
    items = result if isinstance(result, list) else [result]
    for m in items:
        if isinstance(m, Message) and m.chat.id == GROUP_ID:
            await save_message(0, GROUP_ID, "", m.message_id)


class PremiumEmoji(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        changes = {}
        mode = getattr(method, "parse_mode", "none")
        if mode == "HTML" or isinstance(mode, Default):
            for field in ("text", "caption"):
                value = getattr(method, field, None)
                if isinstance(value, str):
                    new = convert(value)
                    if new != value:
                        changes[field] = new
        markup = getattr(method, "reply_markup", None)
        if isinstance(markup, InlineKeyboardMarkup) and ui.ICONS_ENABLED:
            rows = [[ui.iconize(b) for b in row] for row in markup.inline_keyboard]
            changes["reply_markup"] = InlineKeyboardMarkup(inline_keyboard=rows)
        if not changes:
            result = await make_request(bot, method)
        else:
            try:
                result = await make_request(bot, method.model_copy(update=changes))
            except TelegramBadRequest as ex:
                if not any(w in str(ex).lower() for w in ("entit", "emoji", "parse", "icon")):
                    raise
                result = await make_request(bot, method)
        await remember(result)
        return result
