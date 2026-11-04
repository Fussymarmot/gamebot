from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from database.core import x
from services import giveaways as gw
from utils.fmt import esc, rich
from utils.timeutils import now, parse_ts, ts
from utils.ui import btn

router = Router()


def join_kb(gid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("🎁 Участвую!", f"gw:j:{gid}", icon=False)]])


@router.callback_query(F.data.startswith("gw:j:"))
async def join_cb(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    g = await gw.get(gid)
    if not g or g["status"] != "active" or parse_ts(g["ends_at"]) <= now():
        await cb.answer("Этот розыгрыш уже закончился", show_alert=True)
        return
    if g["kind"] != "draw":
        await cb.answer("Здесь не нужно участвовать: победителей определят по рейтингу.", show_alert=True)
        return
    uid = cb.from_user.id
    if uid in await gw.entries(gid):
        await cb.answer("Ты уже участвуешь!")
        return
    ok, reason = await gw.check_eligible(g, uid)
    if not ok:
        await cb.answer(reason, show_alert=True)
        return
    await x("INSERT OR IGNORE INTO giveaway_entries (giveaway_id, user_id, joined_at) VALUES (?,?,?)",
            (gid, uid, ts()))
    await cb.answer("Ты участвуешь! Удачи 🍀")
    try:
        await cb.message.edit_text(gw.announce_text(g, await gw.entries_count(gid)),
                                   reply_markup=join_kb(gid))
    except TelegramBadRequest:
        pass


@router.message(Command("giveaways"))
async def cmd_giveaways(message: Message):
    items = await gw.active_list()
    if not items:
        await message.answer("🎁 <b>Розыгрыши</b>\n\n<i>Сейчас нет активных розыгрышей.</i>")
        return
    t = "🎁 <b>Активные розыгрыши</b>\n\n"
    for g in items:
        ends = parse_ts(g["ends_at"]).strftime("%d.%m %H:%M")
        t += (f"• <b>{rich(g['title'])}</b>: {rich(g['prize'])}\n  <i>{esc(gw.condition_text(g))}; "
              f"итоги {ends}</i>\n")
    await message.answer(t)
