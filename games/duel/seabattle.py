import asyncio
import logging
import random
import secrets
import time
from typing import Optional

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from database.game_stats import add_game_result
from database.moderation import is_user_banned
from games import pause
from games.core import can_dm
from games.duel.challenge import NEED_DM, open_challenge, register_duel
from games.invites import acquire, is_busy, release
from utils import richui
from utils.fmt import esc

router = Router()
log = logging.getLogger(__name__)
KEY = "seabattle"
N = 8
SHIPS = (4, 3, 2, 2, 1, 1, 1)
TURN_SEC = 120
SETUP_SEC = 300
LETTERS = "ABCDEFGH"
games = {}
locks = {}


def near(cell):
    r, c = cell
    return {(r + dr, c + dc) for dr in (-1, 0, 1) for dc in (-1, 0, 1)}


def gen_fleet():
    while True:
        blocked, ships, ok = set(), [], True
        for size in SHIPS:
            placed = False
            for _ in range(200):
                horiz = random.random() < 0.5
                r = random.randrange(N if horiz else N - size + 1)
                c = random.randrange(N - size + 1 if horiz else N)
                cells = [(r, c + i) if horiz else (r + i, c) for i in range(size)]
                if any(x in blocked for x in cells):
                    continue
                ships.append(frozenset(cells))
                for x in cells:
                    blocked |= near(x)
                placed = True
                break
            if not placed:
                ok = False
                break
        if ok:
            return ships


class SB:
    def __init__(self, p1, n1, p2, n2, group_chat):
        self.p1, self.n1, self.p2, self.n2 = p1, n1, p2, n2
        self.group_chat = group_chat
        self.group_mid = None
        self.fleet = {p1: gen_fleet(), p2: gen_fleet()}
        self.shots = {p1: {}, p2: {}}
        self.ready = set() if p2 else {p2}
        self.phase = "setup"
        self.turn = p1
        self.dm = {}
        self.last = time.monotonic()
        self.strikes = {p1: 0, p2: 0}
        self.targets = []
        self.event = ""

    def humans(self):
        return [u for u in (self.p1, self.p2) if u]

    def opp(self, uid):
        return self.p2 if uid == self.p1 else self.p1

    def name(self, uid):
        return "Бот" if uid == 0 else (self.n1 if uid == self.p1 else self.n2)

    def sunk_ships(self, target):
        mine = self.shots[self.opp(target)]
        return [s for s in self.fleet[target] if all(mine.get(x) == "h" for x in s)]

    def all_sunk(self, target):
        return len(self.sunk_ships(target)) == len(self.fleet[target])

    def own_map(self, uid):
        taken = self.shots[self.opp(uid)]
        ship_cells = set().union(*self.fleet[uid])
        rows = []
        for r in range(N):
            line = ""
            for c in range(N):
                if (r, c) in ship_cells:
                    line += "🟥" if taken.get((r, c)) == "h" else "🟩"
                else:
                    line += "⚪" if taken.get((r, c)) == "m" else "🟦"
            rows.append(line)
        return rows

    def radar_cell(self, uid, cell):
        mark = self.shots[uid].get(cell)
        if mark == "m":
            return "⚪"
        if mark == "h":
            sunk = set().union(*self.sunk_ships(self.opp(uid))) if self.sunk_ships(self.opp(uid)) else set()
            return "⬛" if cell in sunk else "🟥"
        return "🟦"

    def alive(self, uid):
        return len(self.fleet[uid]) - len(self.sunk_ships(uid))


def setup_view(g, gid, uid):
    own = g.own_map(uid)
    text = ("⚓ <b>Морской бой: расстановка</b>\n\nТвой флот (🟩 корабли):\n" + "\n".join(own) +
            "\n\n<i>Нажми «Пересоздать», пока не понравится, потом «Готов».</i>")
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Пересоздать", callback_data=f"sbs:{gid}:re"),
        InlineKeyboardButton(text="✅ Готов", callback_data=f"sbs:{gid}:ok")]])
    h = ("<h3>Морской бой: расстановка</h3><p>Твой флот (🟩 корабли)</p>"
         + richui.table(range(1, N + 1), LETTERS, [[x for x in line] for line in own])
         + "<p><i>Нажми «Пересоздать», пока не понравится, потом «Готов».</i></p>"
         + richui.row([richui.btn("🔄 Пересоздать", f"sbs:{gid}:re"), richui.btn("✅ Готов", f"sbs:{gid}:ok", "success")]))
    return h, text, markup


def battle_view(g, gid, uid):
    own = g.own_map(uid)
    foe = g.opp(uid)
    mine_turn = g.phase == "battle" and g.turn == uid
    if g.phase == "over":
        status = g.event
    elif mine_turn:
        status = "Твой ход: выбери клетку на радаре."
    else:
        status = f"Ходит {g.name(g.turn)}."
    if g.event and g.phase != "over":
        status = g.event + "\n" + status
    head = f"⚓ <b>Морской бой</b>\nКорабли: ты {g.alive(uid)} | {g.name(foe)} {g.alive(foe)}"
    text = (head + f"\n\n{esc(status)}\n\n<b>Твой флот</b>\n" + "\n".join(own) + "\n\n<b>Радар</b> (нажми клетку):")
    rows = []
    for r in range(N):
        rows.append([InlineKeyboardButton(text=g.radar_cell(uid, (r, c)), callback_data=f"sb:{gid}:{r}{c}")
                     for c in range(N)])
    markup = InlineKeyboardMarkup(inline_keyboard=rows)
    cells = []
    for r in range(N):
        line = []
        for c in range(N):
            sym = g.radar_cell(uid, (r, c))
            line.append(richui.btn(sym, f"sb:{gid}:{r}{c}") if (sym == "🟦" and g.phase == "battle") else sym)
        cells.append(line)
    h = (f"<h3>Морской бой</h3><p>Корабли: ты {g.alive(uid)} | {richui.esc(g.name(foe))} {g.alive(foe)}</p>"
         f"<p><i>{richui.esc(status)}</i></p><p><b>Твой флот</b></p>"
         + richui.table(range(1, N + 1), LETTERS, [[x for x in line] for line in own])
         + "<p><b>Радар</b></p>" + richui.table(range(1, N + 1), LETTERS, cells))
    return h, text, markup


async def push(bot, g, gid, uid):
    if not uid:
        return
    view = setup_view(g, gid, uid) if g.phase == "setup" else battle_view(g, gid, uid)
    g.dm[uid], _ = await richui.publish(bot, uid, g.dm.get(uid), view[0], view[1], view[2])


async def group_status(bot, g, text):
    try:
        if g.group_mid:
            await bot.edit_message_text(text, chat_id=g.group_chat, message_id=g.group_mid)
        else:
            g.group_mid = (await bot.send_message(g.group_chat, text)).message_id
    except Exception:
        pass


def drop(gid):
    g = games.pop(gid, None)
    locks.pop(gid, None)
    if g:
        release(g.p1, g.p2 if g.p2 else -g.p1)
    return g


async def begin(bot, p1, n1, p2, n2, group_chat):
    gid = secrets.token_hex(3)
    g = SB(p1, n1, p2, n2, group_chat)
    games[gid] = g
    locks[gid] = asyncio.Lock()
    for u in g.humans():
        await push(bot, g, gid, u)
    who = f"<b>{esc(n1)}</b> vs <b>{esc(n2)}</b>" if p2 else f"<b>{esc(n1)}</b> vs бот"
    await group_status(bot, g, f"⚓ <b>Морской бой</b>\n{who}\n<i>Игроки расставляют корабли в личке с ботом.</i>")
    if not p2:
        await ready_up(bot, gid, g, 0)
    asyncio.create_task(watch(bot, gid))
    return g


async def start_game(msg, p1, n1, p2, n2):
    bad = [n for u, n in ((p1, n1), (p2, n2)) if not await can_dm(msg.bot, u)]
    if bad:
        release(p1, p2)
        me = await msg.bot.me()
        await msg.answer("❌ Не могу написать в личку: " + ", ".join(f"<b>{esc(n)}</b>" for n in bad) +
                         f"\n<i>Откройте @{me.username}, нажмите Start и создайте игру снова.</i>")
        return
    await begin(msg.bot, p1, n1, p2, n2, msg.chat.id)


register_duel(KEY, "Морской бой", "game", start_game, dm="both")


async def start_vs_bot(bot, chat_id: int, me) -> Optional[str]:
    """Партия против бота. Возвращает None или текст ошибки (NEED_DM: надо нажать Start у бота)."""
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(me.id):
        return "Ты в чёрном списке игр."
    if is_busy(me.id) or not acquire(me.id, -me.id, KEY):
        return "Ты уже участвуешь в игре."
    if not await can_dm(bot, me.id):
        release(me.id, -me.id)
        return NEED_DM
    await begin(bot, me.id, me.first_name or me.username or "Игрок", 0, "Бот", chat_id)
    return None


@router.message(Command("seabattle"))
async def cmd_seabattle(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip().lower() in ("bot", "бот"):
        err = await start_vs_bot(message.bot, message.chat.id, message.from_user)
        if err == NEED_DM:
            bot_me = await message.bot.me()
            await message.answer(f"❌ Не могу написать в личку. Открой @{bot_me.username}, нажми Start и повтори.")
        elif err:
            await message.answer(f"⚠️ {esc(err)}")
        return
    await open_challenge(message, KEY)


async def ready_up(bot, gid, g, uid):
    g.ready.add(uid)
    if g.ready >= {g.p1, g.p2}:
        g.phase = "battle"
        g.turn = g.p1
        g.last = time.monotonic()
        g.event = "Бой начался!"
        for u in g.humans():
            await push(bot, g, gid, u)
        await group_status(bot, g, f"⚓ <b>Морской бой</b>: бой идёт!\nХодит <b>{esc(g.name(g.turn))}</b>")


@router.callback_query(F.data.startswith("sbs:"))
async def setup_cb(cb: CallbackQuery):
    _, gid, act = cb.data.split(":")
    g = games.get(gid)
    uid = cb.from_user.id
    if not g or g.phase != "setup" or uid not in g.humans():
        await cb.answer("Расстановка уже закончилась", show_alert=True)
        return
    async with locks[gid]:
        if uid in g.ready:
            await cb.answer("Ты уже готов, ждём соперника")
            return
        if act == "re":
            g.fleet[uid] = gen_fleet()
            await push(cb.bot, g, gid, uid)
            await cb.answer()
            return
        await ready_up(cb.bot, gid, g, uid)
        if g.phase == "setup":
            try:
                await cb.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await cb.answer("Готов! Ждём соперника.")
        else:
            await cb.answer("Бой начался!")


def apply_shot(g, shooter, cell):
    foe = g.opp(shooter)
    ship_cells = set().union(*g.fleet[foe])
    if cell not in ship_cells:
        g.shots[shooter][cell] = "m"
        return "miss"
    g.shots[shooter][cell] = "h"
    for s in g.sunk_ships(foe):
        if cell in s:
            for x in set().union(*(near(y) for y in s)):
                if 0 <= x[0] < N and 0 <= x[1] < N and x not in g.shots[shooter]:
                    g.shots[shooter][x] = "m"
            return "sunk"
    return "hit"


async def finish(bot, gid, g, winner, note=""):
    drop(gid)
    g.phase = "over"
    g.event = f"Победил {g.name(winner)}!{note}"
    for u in g.humans():
        await push(bot, g, gid, u)
    await group_status(bot, g, f"🎉 <b>Морской бой</b>: победил <b>{esc(g.name(winner))}</b>!{note}")
    if g.p2:
        loser = g.opp(winner)
        await add_game_result(winner, KEY, "win")
        await add_game_result(loser, KEY, "loss")


def after_shot(g, shooter, res, cell):
    where = f"{LETTERS[cell[0]]}{cell[1] + 1}"
    word = {"miss": "мимо", "hit": "попадание", "sunk": "корабль потоплен"}[res]
    g.event = f"{g.name(shooter)}: {where}, {word}."


async def bot_turns(bot, gid, g):
    while gid in games and g.phase == "battle" and g.turn == 0:
        await pause.sleep(1.2)
        free = [(r, c) for r in range(N) for c in range(N) if (r, c) not in g.shots[0]]
        cell = None
        while g.targets and cell is None:
            t = g.targets.pop()
            if t in free:
                cell = t
        if cell is None:
            pref = [x for x in free if (x[0] + x[1]) % 2 == 0] or free
            cell = random.choice(pref)
        res = apply_shot(g, 0, cell)
        after_shot(g, 0, res, cell)
        if res == "hit":
            g.targets += [(cell[0] + dr, cell[1] + dc) for dr, dc in ((1, 0), (-1, 0), (0, 1), (0, -1))]
        elif res == "sunk":
            g.targets = []
        if g.all_sunk(g.p1):
            await finish(bot, gid, g, 0)
            return
        if res == "miss":
            g.turn = g.p1
        g.last = time.monotonic()
        await push(bot, g, gid, g.p1)


@router.callback_query(F.data.startswith("sb:"))
async def shot_cb(cb: CallbackQuery):
    _, gid, rc = cb.data.split(":")
    g = games.get(gid)
    uid = cb.from_user.id
    if not g or uid not in g.humans():
        await cb.answer("Партия завершена", show_alert=True)
        return
    if g.phase != "battle":
        await cb.answer("Бой ещё не начался", show_alert=True)
        return
    if g.turn != uid:
        await cb.answer("Сейчас не твой ход", show_alert=True)
        return
    cell = (int(rc[0]), int(rc[1]))
    async with locks[gid]:
        if gid not in games or g.turn != uid:
            await cb.answer()
            return
        if cell in g.shots[uid]:
            await cb.answer("Сюда уже стреляли", show_alert=True)
            return
        res = apply_shot(g, uid, cell)
        after_shot(g, uid, res, cell)
        g.strikes[uid] = 0
        g.last = time.monotonic()
        if g.all_sunk(g.opp(uid)):
            await finish(cb.bot, gid, g, uid)
            await cb.answer("Победа!")
            return
        if res == "miss":
            g.turn = g.opp(uid)
        for u in g.humans():
            await push(cb.bot, g, gid, u)
        await cb.answer({"miss": "Мимо", "hit": "Попал! Стреляй ещё", "sunk": "Потоплен! Стреляй ещё"}[res])
        if g.turn != uid:
            await group_status(cb.bot, g, f"⚓ <b>Морской бой</b>: бой идёт!\nХодит <b>{esc(g.name(g.turn))}</b>")
    if g.turn == 0:
        asyncio.create_task(bot_turns(cb.bot, gid, g))


async def watch(bot, gid):
    while gid in games:
        await asyncio.sleep(15)
        g = games.get(gid)
        if not g:
            return
        idle = time.monotonic() - g.last
        if g.phase == "setup" and idle > SETUP_SEC:
            drop(gid)
            g.phase = "over"
            g.event = "Расстановка заняла слишком много времени, игра отменена."
            for u in g.humans():
                await push(bot, g, gid, u)
            await group_status(bot, g, "⚓ <b>Морской бой</b> отменён: время на расстановку вышло.")
            return
        if g.phase == "battle" and g.turn and idle > TURN_SEC:
            async with locks.get(gid, asyncio.Lock()):
                if gid not in games:
                    return
                loser = g.turn
                g.strikes[loser] += 1
                if g.strikes[loser] >= 2:
                    await finish(bot, gid, g, g.opp(loser), " (соперник не ходил)")
                    return
                g.turn = g.opp(loser)
                g.last = time.monotonic()
                g.event = f"{g.name(loser)} не успел(а) походить, ход переходит."
                for u in g.humans():
                    await push(bot, g, gid, u)
                await group_status(bot, g, f"⚓ <b>Морской бой</b>: бой идёт!\nХодит <b>{esc(g.name(g.turn))}</b>")
            if g.turn == 0:
                asyncio.create_task(bot_turns(bot, gid, g))
