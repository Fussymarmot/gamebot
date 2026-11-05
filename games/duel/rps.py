import asyncio
import secrets
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.game_stats import add_game_result
from games.duel.challenge import open_challenge, register_duel
from games.invites import release
from utils.emoji import e
from utils.fmt import esc
from utils.logs import logger
from utils.tasks import spawn

router = Router()
games = {}
locks = {}
EMO = {"rock": "🪨 Камень", "scissors": "✂️ Ножницы", "paper": "📄 Бумага"}
BEATS = {"rock": "scissors", "scissors": "paper", "paper": "rock"}


class RPS:
    def __init__(self, p1, n1, p2, n2):
        self.p1, self.n1, self.p2, self.n2 = p1, n1, p2, n2
        self.c1 = self.c2 = None
        self.msg = None
        self.last = datetime.now()

    def kb(self, gid):
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t, callback_data=f"rps:{gid}:{k}") for k, t in EMO.items()]])

    def text(self):
        def mark(c):
            return "✅ выбрал" if c else "⏳ думает"
        return (f"{e('dice')} <b>Камень-ножницы-бумага</b>\n\n"
                f"<b>{esc(self.n1)}</b>: {mark(self.c1)}\n<b>{esc(self.n2)}</b>: {mark(self.c2)}\n\n"
                "<i>Выбор скрыт до тех пор, пока не выберут оба</i>")


@router.message(Command("rps"))
async def cmd_rps(message: Message):
    await open_challenge(message, "rps")


async def start_game(msg, p1, n1, p2, n2):
    gid = secrets.token_hex(3)
    g = RPS(p1, n1, p2, n2)
    games[gid] = g
    locks[gid] = asyncio.Lock()
    g.msg = await msg.answer(g.text(), reply_markup=g.kb(gid))
    spawn(_watch(gid))


register_duel("rps", "Камень-ножницы-бумага", "dice", start_game)


def _finish(gid):
    g = games.pop(gid, None)
    locks.pop(gid, None)
    if g:
        release(g.p1, g.p2)
    return g


async def _watch(gid):
    while gid in games:
        await asyncio.sleep(60)
        g = games.get(gid)
        if g and (datetime.now() - g.last).total_seconds() >= 600:
            _finish(gid)
            try:
                await g.msg.edit_text(g.text() + "\n\n<i>Игра закрыта по таймауту</i>")
            except Exception:
                logger.debug("Не удалось показать таймаут в КНБ %s", gid, exc_info=True)
            return


@router.callback_query(F.data.startswith("rps:"))
async def rps_cb(cb: CallbackQuery):
    _, gid, choice = cb.data.split(":")
    g = games.get(gid)
    if not g:
        await cb.answer("Игра завершена", show_alert=True)
        return
    uid = cb.from_user.id
    if uid not in (g.p1, g.p2):
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    async with locks[gid]:
        if (uid == g.p1 and g.c1) or (uid == g.p2 and g.c2):
            await cb.answer("Ты уже сделал выбор", show_alert=True)
            return
        if uid == g.p1:
            g.c1 = choice
        else:
            g.c2 = choice
        g.last = datetime.now()
        await cb.answer(f"Ты выбрал: {EMO[choice]}", show_alert=True)
        if not (g.c1 and g.c2):
            await cb.message.edit_text(g.text(), reply_markup=g.kb(gid))
            return
        _finish(gid)
        res = (f"{e('dice')} <b>Камень-ножницы-бумага</b>\n\n"
               f"<b>{esc(g.n1)}</b>: {EMO[g.c1]}\n<b>{esc(g.n2)}</b>: {EMO[g.c2]}\n\n")
        if g.c1 == g.c2:
            res += f"{e('sparkles')} <b>Ничья!</b>"
            results = [(g.p1, "draw"), (g.p2, "draw")]
        else:
            p1_wins = BEATS[g.c1] == g.c2
            wid, wn, lid = (g.p1, g.n1, g.p2) if p1_wins else (g.p2, g.n2, g.p1)
            res += f"{e('party')} Победа: <b>{esc(wn)}</b>!"
            results = [(wid, "win"), (lid, "loss")]
        # Сначала показываем итог, потом начисляем награды: сбой экономики не должен «подвесить» доску.
        try:
            await cb.message.edit_text(res)
        finally:
            for u, r in results:
                try:
                    await add_game_result(u, "rps", r)
                except Exception:
                    logger.exception("Не удалось записать результат КНБ для игрока %s", u)
