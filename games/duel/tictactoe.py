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

router = Router()
games = {}
locks = {}
SYM = {"X": "❌", "O": "⭕", " ": "▫️"}


class TTT:
    def __init__(self, p1, n1, p2, n2):
        self.p1, self.n1, self.p2, self.n2 = p1, n1, p2, n2
        self.board = [[" "] * 3 for _ in range(3)]
        self.turn = p1
        self.sym = {p1: "X", p2: "O"}
        self.msg = None
        self.last = datetime.now()

    def move(self, r, c, uid):
        if self.turn != uid or self.board[r][c] != " ":
            return False
        self.board[r][c] = self.sym[uid]
        self.turn = self.p2 if uid == self.p1 else self.p1
        self.last = datetime.now()
        return True

    def winner(self):
        b = self.board
        lines = [b[i] for i in range(3)] + [[b[r][i] for r in range(3)] for i in range(3)]
        lines += [[b[i][i] for i in range(3)], [b[i][2 - i] for i in range(3)]]
        for ln in lines:
            if ln[0] != " " and ln[0] == ln[1] == ln[2]:
                return ln[0]
        if all(c != " " for row in b for c in row):
            return "draw"
        return None

    def kb(self, gid, over=False):
        rows = []
        for i in range(3):
            rows.append([InlineKeyboardButton(
                text=SYM[self.board[i][j]],
                callback_data="noop" if over else f"ttt:{gid}:{i}:{j}") for j in range(3)])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def text(self):
        t = f"{e('game')} <b>Крестики-нолики</b>\n\n❌ <b>{esc(self.n1)}</b>\n⭕ <b>{esc(self.n2)}</b>\n\n"
        return t + f"<i>Ход: {esc(self.n1 if self.turn == self.p1 else self.n2)}</i>"


@router.message(Command("ttt"))
async def cmd_ttt(message: Message):
    await open_challenge(message, "ttt")


async def start_game(msg, p1, n1, p2, n2):
    gid = secrets.token_hex(3)
    g = TTT(p1, n1, p2, n2)
    games[gid] = g
    locks[gid] = asyncio.Lock()
    g.msg = await msg.answer(g.text(), reply_markup=g.kb(gid))
    asyncio.create_task(_watch(gid))


register_duel("ttt", "Крестики-нолики", "game", start_game)


async def _finish(gid):
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
            await _finish(gid)
            try:
                await g.msg.edit_text(g.text() + "\n\n<i>Игра закрыта по таймауту</i>")
            except Exception:
                logger.debug("Не удалось показать таймаут крестиков-ноликов %s", gid, exc_info=True)
            return


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data.startswith("ttt:"))
async def ttt_cb(cb: CallbackQuery):
    _, gid, r, c = cb.data.split(":")
    g = games.get(gid)
    if not g:
        await cb.answer("Игра завершена", show_alert=True)
        return
    uid = cb.from_user.id
    if uid not in (g.p1, g.p2):
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if g.turn != uid:
        await cb.answer("Сейчас не твой ход", show_alert=True)
        return
    async with locks[gid]:
        if not g.move(int(r), int(c), uid):
            await cb.answer("Эта клетка занята", show_alert=True)
            return
        w = g.winner()
        if not w:
            await cb.message.edit_text(g.text(), reply_markup=g.kb(gid))
            await cb.answer()
            return
        await _finish(gid)
        if w == "draw":
            res = f"\n\n{e('sparkles')} <b>Ничья!</b>"
            results = [(g.p1, "draw"), (g.p2, "draw")]
        else:
            wid, wn, lid = (g.p1, g.n1, g.p2) if w == "X" else (g.p2, g.n2, g.p1)
            res = f"\n\n{e('party')} Победа: <b>{esc(wn)}</b>!"
            results = [(wid, "win"), (lid, "loss")]
        try:
            await cb.message.edit_text(g.text().rsplit("\n\n", 1)[0] + res, reply_markup=g.kb(gid, over=True))
        finally:
            for u, r in results:
                try:
                    await add_game_result(u, "ttt", r)
                except Exception:
                    logger.exception("Не удалось записать результат крестиков-ноликов для игрока %s", u)
        await cb.answer()
