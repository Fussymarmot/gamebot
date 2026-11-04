import html as _html
import logging
from typing import Optional, Tuple

import aiohttp
from aiogram.exceptions import TelegramBadRequest

from config import BOT_TOKEN
from database.settings import get_setting

log = logging.getLogger(__name__)
API = f"https://api.telegram.org/bot{BOT_TOKEN}/"
_session: Optional[aiohttp.ClientSession] = None


async def enabled() -> bool:
    return (await get_setting("rich_games", "0")) == "1"


async def call(method: str, payload: dict) -> Optional[dict]:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    try:
        async with _session.post(API + method, json=payload) as resp:
            data = await resp.json()
    except Exception as ex:
        log.warning("%s: сеть недоступна: %s", method, ex)
        return None
    if data.get("ok"):
        return data
    if "not modified" in str(data.get("description", "")):
        return {"ok": True, "result": True}
    log.warning("%s отклонён: %s", method, data.get("description"))
    return None


def esc(text) -> str:
    return _html.escape(str(text), quote=True)


def btn(text: str, data: str, style: Optional[str] = None) -> str:
    st = f' style="{style}"' if style else ""
    return f'<tg-button type="callback_data" data="{esc(data)}"{st}>{esc(text)}</tg-button>'


def row(buttons, align: str = "center") -> str:
    return f'<tg-button-row align="{align}">{"".join(buttons)}</tg-button-row>'


def table(top, left, cells) -> str:
    out = ["<table>"]
    if top:
        out.append("<tr>" + ("<th></th>" if left else "") + "".join(f"<th>{esc(t)}</th>" for t in top) + "</tr>")
    for i, line in enumerate(cells):
        head = f"<th>{esc(left[i])}</th>" if left else ""
        out.append("<tr>" + head + "".join(f"<td>{c}</td>" for c in line) + "</tr>")
    out.append("</table>")
    return "".join(out)


async def publish(bot, chat_id: int, message_id: Optional[int], rich_html: Optional[str], text: str,
                  markup=None) -> Tuple[Optional[int], bool]:
    if rich_html and await enabled():
        if message_id is None:
            data = await call("sendRichMessage", {"chat_id": chat_id, "rich_message": {"html": rich_html}})
        else:
            data = await call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                                  "rich_message": {"html": rich_html}})
        if data:
            res = data.get("result")
            return (res.get("message_id") if isinstance(res, dict) else message_id), True
    try:
        if message_id is None:
            msg = await bot.send_message(chat_id, text, reply_markup=markup)
            return msg.message_id, False
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest as ex:
        if "not modified" not in str(ex):
            log.warning("Не удалось обновить сообщение: %s", ex)
    return message_id, False
