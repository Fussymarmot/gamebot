import asyncio
import secrets
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.game_stats import add_game_result
from games.duel.challenge import open_challenge
from games.invites import release
from utils import richui
from utils.emoji import e
from utils.fmt import esc

router = Router()
games = {}
locks = {}
_bot_ref = [None]
KEY = "checkers"
IDLE_SEC = 1800
DIRS = ((-1, -1), (-1, 1), (1, -1), (1, 1))
LOOK = {"w": "⚪", "W": "🤍", "b": "⚫", "B": "🖤"}


def inside(r, c):
    return 0 <= r < 8 and 0 <= c < 8


def owner(piece):
    return piece.lower() if piece else None


class Checkers:
    def __init__(self, p1, n1, p2, n2):
        self.p1, self.n1, self.p2, self.n2 = p1, n1, p2, n2
        self.board = [[None] * 8 for _ in range(8)]
        for r in range(8):
            for c in range(8):
                if (r + c) % 2 == 1:
                    if r < 3:
                        self.board[r][c] = "b"
                    elif r > 4:
                        self.board[r][c] = "w"
        self.turn = "w"
        self.sel = None
        self.chain = None
        self.quiet_moves = 0
        self.chat_id = 0
        self.mid = None
        self.note = ""
        self.last = datetime.now()
        self.over = None

    def uid(self, color):
        return self.p1 if color == "w" else self.p2

    def name(self, color):
        return self.n1 if color == "w" else self.n2

    def captures_from(self, r, c):
        piece = self.board[r][c]
        me = owner(piece)
        out = []
        for dr, dc in DIRS:
            if piece.islower():
                mr, mc = r + dr, c + dc
                lr, lc = r + 2 * dr, c + 2 * dc
                if inside(lr, lc) and self.board[mr][mc] and owner(self.board[mr][mc]) != me and not self.board[lr][lc]:
                    out.append(((lr, lc), (mr, mc)))
            else:
                rr, cc = r + dr, c + dc
                while inside(rr, cc) and not self.board[rr][cc]:
                    rr, cc = rr + dr, cc + dc
                if inside(rr, cc) and owner(self.board[rr][cc]) != me:
                    lr, lc = rr + dr, cc + dc
                    while inside(lr, lc) and not self.board[lr][lc]:
                        out.append(((lr, lc), (rr, cc)))
                        lr, lc = lr + dr, lc + dc
        return out

    def steps_from(self, r, c):
        piece = self.board[r][c]
        out = []
        for dr, dc in DIRS:
            if piece.islower():
                if (piece == "w" and dr > 0) or (piece == "b" and dr < 0):
                    continue
                nr, nc = r + dr, c + dc
                if inside(nr, nc) and not self.board[nr][nc]:
                    out.append(((nr, nc), None))
            else:
                nr, nc = r + dr, c + dc
                while inside(nr, nc) and not self.board[nr][nc]:
                    out.append(((nr, nc), None))
                    nr, nc = nr + dr, nc + dc
        return out

    def legal(self, color):
        if self.chain:
            r, c = self.chain
            return {self.chain: self.captures_from(r, c)}
        caps, steps = {}, {}
        for r in range(8):
            for c in range(8):
                if owner(self.board[r][c]) != color:
                    continue
                cp = self.captures_from(r, c)
                if cp:
                    caps[(r, c)] = cp
                st = self.steps_from(r, c)
                if st:
                    steps[(r, c)] = st
        return caps or steps

    def apply(self, frm, to, cap):
        r, c = frm
        piece = self.board[r][c]
        self.board[r][c] = None
        if cap:
            self.board[cap[0]][cap[1]] = None
        promoted = False
        if piece == "w" and to[0] == 0:
            piece, promoted = "W", True
        if piece == "b" and to[0] == 7:
            piece, promoted = "B", True
        self.board[to[0]][to[1]] = piece
        self.last = datetime.now()
        if cap:
            self.quiet_moves = 0
            if self.captures_from(*to):
                self.chain = to
                self.sel = to
                return
        else:
            self.quiet_moves = self.quiet_moves + 1 if piece.isupper() and not promoted else 0
        self.chain = None
        self.sel = None
        self.turn = "b" if self.turn == "w" else "w"

    def result(self):
        if not self.legal(self.turn):
            return "b" if self.turn == "w" else "w"
        if self.quiet_moves >= 30:
            return "draw"
        return None

    def count(self, color):
        return sum(1 for row in self.board for p in row if owner(p) == color)

    def kb(self, gid):
        targets = {}
        if self.sel and not self.over:
            targets = {to: True for to, _ in self.legal(self.turn).get(self.sel, [])}
        rows = []
        for r in range(8):
            row = []
            for c in range(8):
                piece = self.board[r][c]
                if (r, c) in targets:
                    text = "🟩"
                elif self.sel == (r, c):
                    text = "🟡"
                elif piece:
                    text = LOOK[piece]
                else:
                    text = "⬛" if (r + c) % 2 == 1 else "◻️"
                row.append(InlineKeyboardButton(text=text, callback_data=f"ck:{gid}:{r}{c}"))
            rows.append(row)
        if not self.over:
            rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data=f"ck:{gid}:rs")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def text(self):
        t = (f"{e('game')} <b>Шашки</b>\n\n⚪ <b>{esc(self.n1)}</b> ({self.count('w')})\n"
             f"⚫ <b>{esc(self.n2)}</b> ({self.count('b')})\n\n")
        if self.over:
            return t + self.note
        who = self.name(self.turn)
        tail = " Рубите дальше той же шашкой." if self.chain else ""
        return t + f"<i>Ход: {esc(who)}.{tail}</i>"


    def rich_html(self, gid):
        targets = {}
        if self.sel and not self.over:
            targets = {to: True for to, _ in self.legal(self.turn).get(self.sel, [])}
        cells = []
        for r in range(8):
            line = []
            for c in range(8):
                piece = self.board[r][c]
                if (r + c) % 2 == 0:
                    line.append("◻️")
                    continue
                if (r, c) in targets:
                    text, style = "🟩", "success"
                elif self.sel == (r, c):
                    text, style = "🟡", "primary"
                else:
                    text, style = (LOOK[piece] if piece else "⬛"), None
                line.append(richui.esc(text) if self.over else richui.btn(text, f"ck:{gid}:{r}{c}", style))
            cells.append(line)
        head = (f"<h3>Шашки</h3><p>⚪ <b>{richui.esc(self.n1)}</b> ({self.count('w')})<br>"
                f"⚫ <b>{richui.esc(self.n2)}</b> ({self.count('b')})</p>")
        if self.over:
            foot = f"<p>{self.note.strip()}</p>" if self.note else ""
        else:
            tail = " Рубите дальше той же шашкой." if self.chain else ""
            foot = f"<p><i>Ход: {richui.esc(self.name(self.turn))}.{tail}</i></p>"
        out = head + richui.table("abcdefgh", [str(8 - r) for r in range(8)], cells) + foot
        if not self.over:
            out += richui.row([richui.btn("🏳 Сдаться", f"ck:{gid}:rs", "danger")])
        return out


async def render(bot, gid, g):
    g.mid, _ = await richui.publish(bot, g.chat_id, g.mid, g.rich_html(gid), g.text(), g.kb(gid))


@router.message(Command("checkers"))
async def cmd_checkers(message: Message):
    await open_challenge(message, "checkers", "Шашки", "game", start_game)


async def start_game(msg, p1, n1, p2, n2):
    gid = secrets.token_hex(3)
    g = Checkers(p1, n1, p2, n2)
    games[gid] = g
    locks[gid] = asyncio.Lock()
    g.chat_id = msg.chat.id
    _bot_ref[0] = msg.bot
    await render(msg.bot, gid, g)
    asyncio.create_task(_watch(gid))


def _drop(gid):
    g = games.pop(gid, None)
    locks.pop(gid, None)
    if g:
        release(g.p1, g.p2)
    return g


async def _watch(gid):
    while gid in games:
        await asyncio.sleep(60)
        g = games.get(gid)
        if g and (datetime.now() - g.last).total_seconds() >= IDLE_SEC:
            _drop(gid)
            g.over = "timeout"
            g.note = "<i>Партия закрыта: 30 минут без хода.</i>"
            try:
                await render(_bot_ref[0], gid, g)
            except Exception:
                pass
            return


async def _finish(cb, gid, g, winner, note=""):
    _drop(gid)
    g.over = winner
    if winner == "draw":
        res = "✨ <b>Ничья!</b>"
        await add_game_result(g.p1, KEY, "draw")
        await add_game_result(g.p2, KEY, "draw")
    else:
        loser = "b" if winner == "w" else "w"
        res = f"🎉 Победа: <b>{esc(g.name(winner))}</b>!{note}"
        await add_game_result(g.uid(winner), KEY, "win")
        await add_game_result(g.uid(loser), KEY, "loss")
    g.note = res
    await render(cb.bot, gid, g)


@router.callback_query(F.data.startswith("ck:"))
async def ck_cb(cb: CallbackQuery):
    _, gid, cell = cb.data.split(":")
    g = games.get(gid)
    if not g:
        await cb.answer("Партия завершена", show_alert=True)
        return
    uid = cb.from_user.id
    if uid not in (g.p1, g.p2):
        await cb.answer("Ты не участвуешь в этой партии", show_alert=True)
        return
    async with locks[gid]:
        if gid not in games:
            await cb.answer("Партия завершена", show_alert=True)
            return
        if cell == "rs":
            winner = "b" if uid == g.p1 else "w"
            await _finish(cb, gid, g, winner, " (соперник сдался)")
            await cb.answer()
            return
        if g.uid(g.turn) != uid:
            await cb.answer("Сейчас не твой ход", show_alert=True)
            return
        pos = (int(cell[0]), int(cell[1]))
        moves = g.legal(g.turn)
        piece = g.board[pos[0]][pos[1]]
        if g.sel and pos in [to for to, _ in moves.get(g.sel, [])]:
            to, cap = next((t, c) for t, c in moves[g.sel] if t == pos)
            g.apply(g.sel, to, cap)
            w = g.result()
            if w:
                await _finish(cb, gid, g, w)
            else:
                await render(cb.bot, gid, g)
            await cb.answer()
            return
        if piece and owner(piece) == g.turn:
            if pos not in moves:
                capture_exists = any(m for m in moves.values() if any(cp for _, cp in m))
                await cb.answer("Нужно рубить другой шашкой" if capture_exists else "У этой шашки нет ходов",
                                show_alert=True)
                return
            if g.chain and pos != g.chain:
                await cb.answer("Продолжай рубить той же шашкой", show_alert=True)
                return
            g.sel = pos
            await render(cb.bot, gid, g)
            await cb.answer()
            return
        if g.sel and not g.chain:
            g.sel = None
            await render(cb.bot, gid, g)
        await cb.answer()
