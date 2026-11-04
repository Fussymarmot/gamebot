import asyncio
import random
import secrets

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.game_stats import add_game_result
from games.duel.challenge import open_challenge
from games.invites import release
from utils.emoji import e
from utils.fmt import esc

router = Router()
games = {}
locks = {}


class Duel:
    def __init__(self, p1, n1, p2, n2):
        self.p1, self.n1, self.p2, self.n2 = p1, n1, p2, n2
        self.state = "waiting"
        self.msg = None

    def head(self):
        return f"{e('swords')} <b>Дуэль на скорость</b>\n\n<b>{esc(self.n1)}</b> vs <b>{esc(self.n2)}</b>\n\n"

    def text(self):
        if self.state == "waiting":
            return self.head() + "<i>Приготовьтесь... Не нажимайте раньше времени!</i>"
        return self.head() + f"{e('fire')} <b>ЖМИ!</b>"

    def kb(self, gid):
        if self.state == "waiting":
            return InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="⏳ Ждём сигнала...", callback_data=f"duel:{gid}:early")]])
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⚡ ЖМИ!", callback_data=f"duel:{gid}:fire")]])


@router.message(Command("duel"))
async def cmd_duel(message: Message):
    await open_challenge(message, "duel", "Дуэль на скорость", "swords", start_duel)


async def start_duel(msg, p1, n1, p2, n2):
    gid = secrets.token_hex(3)
    d = Duel(p1, n1, p2, n2)
    games[gid] = d
    locks[gid] = asyncio.Lock()
    d.msg = await msg.answer(d.text(), reply_markup=d.kb(gid))
    await asyncio.sleep(random.uniform(2, 6))
    if gid in games and d.state == "waiting":
        d.state = "ready"
        try:
            await d.msg.edit_text(d.text(), reply_markup=d.kb(gid))
        except Exception:
            pass
    await asyncio.sleep(60)
    if gid in games:
        _finish(gid)
        try:
            await d.msg.edit_text(d.head() + "<i>Никто не нажал. Дуэль отменена.</i>")
        except Exception:
            pass


def _finish(gid):
    d = games.pop(gid, None)
    locks.pop(gid, None)
    if d:
        release(d.p1, d.p2)
    return d


@router.callback_query(F.data.startswith("duel:"))
async def duel_cb(cb: CallbackQuery):
    _, gid, kind = cb.data.split(":")
    d = games.get(gid)
    if not d:
        await cb.answer("Дуэль завершена", show_alert=True)
        return
    uid = cb.from_user.id
    if uid not in (d.p1, d.p2):
        await cb.answer("Ты не участвуешь в этой дуэли", show_alert=True)
        return
    async with locks[gid]:
        if gid not in games:
            await cb.answer("Дуэль завершена", show_alert=True)
            return
        me_name, other_id, other_name = (d.n1, d.p2, d.n2) if uid == d.p1 else (d.n2, d.p1, d.n1)
        if kind == "early" or d.state == "waiting":
            _finish(gid)
            await add_game_result(other_id, "duel", "win")
            await add_game_result(uid, "duel", "loss")
            await cb.message.edit_text(
                d.head() + f"{e('cross')} <b>{esc(me_name)}</b> фальстартовал!\n"
                           f"{e('party')} Победа: <b>{esc(other_name)}</b>!")
            await cb.answer("Фальстарт!", show_alert=True)
            return
        _finish(gid)
        d.state = "done"
        await add_game_result(uid, "duel", "win")
        await add_game_result(other_id, "duel", "loss")
        await cb.message.edit_text(d.head() + f"{e('party')} <b>Победа: {esc(me_name)}!</b>")
        await cb.answer()
