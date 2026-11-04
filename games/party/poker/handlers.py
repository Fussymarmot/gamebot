"""Покер (Техасский холдем) прямо в чате.

/poker [бай-ин] или кнопка в /games открывает лобби (2–8 игроков, у каждого должна быть личка с ботом).
Карты и текущая комбинация приходят игроку в личку и обновляются на каждой улице; то же самое можно
увидеть кнопкой «Мои карты» прямо под столом (видит только нажавший). Ходы делаются кнопками, свой размер
рейза можно написать в чат числом.

Бай-ин необязателен: без него играют на 1000 фишек, с ним каждый получает столько фишек, сколько рубинов
поставил, а рубины переходят от проигравших к выигравшим по итогам всей игры. Блайнды растут каждые
BLIND_EVERY раздач, так что игра всегда заканчивается.
"""
import asyncio
import logging
import random
import re
from typing import Dict, List, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import GROUP_ID
from database.core import x
from games import pause
from games.core import dm, finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from games.duel.challenge import MAX_STAKE
from games.party.poker.engine import (STREET_RU, Seat, Table, card_str, describe_cards)
from services import economy as ec
from utils import ui
from utils.emoji import EMOJI, RUBY, e
from utils.fmt import esc

router = Router()
log = logging.getLogger(__name__)

KEY = "poker"
MAX_PLAYERS = 8
START_CHIPS = 1000        # фишек у каждого, если играют без ставки
TURN_SEC = 45             # время на ход
MAX_TIMEOUTS = 2          # столько пропусков подряд, и игрок уходит из-за стола
BLIND_EVERY = 5           # блайнды удваиваются каждые N раздач
BETWEEN_HANDS_SEC = 7
RUNOUT_SEC = 2.5          # пауза между картами, когда все в олл-ине
NOTIFY_TURN = True        # писать игроку в личку «твой ход» (из группы уведомление об изменении сообщения не придёт)
ICON_ID = EMOJI["poker"][0]


# ---------- оформление ----------

def cards_html(cards: List[int]) -> str:
    # <code>, чтобы масть ♦ не превращалась в кастомный эмодзи рубина
    return " ".join(f"<code>{card_str(c)}</code>" for c in cards) or "—"


def cards_plain(cards: List[int]) -> str:
    return " ".join(card_str(c) for c in cards)


def money(d: dict, n: int) -> str:
    return f"{n} {RUBY if d.get('stake') else '🪙'}"


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def pb(text: str, data: str) -> InlineKeyboardButton:
    return ui.btn(text, data)


def my_cards_btn(gid: int) -> InlineKeyboardButton:
    data = f"pk:{gid}:0:m"
    if ui.ICONS_ENABLED:
        return InlineKeyboardButton(text="Мои карты", callback_data=data, icon_custom_emoji_id=ICON_ID)
    return InlineKeyboardButton(text="🃏 Мои карты", callback_data=data)


def table_text(lobby: Lobby, note: str = "") -> str:
    d, t = lobby.data, lobby.data["table"]
    head = f"{e('poker')} <b>Покер · раздача #{t.hand_no}</b>\nБлайнды: {t.sb}/{t.bb}"
    if d.get("stake"):
        head += f" · бай-ин {d['stake']} {RUBY}"
    lines = [head, f"<b>{STREET_RU[t.street]}</b>", "",
             f"Стол: {cards_html(t.board) if t.board else '<i>карты ещё не открыты</i>'}",
             f"Банк: <b>{money(d, t.pot())}</b>", ""]
    cur = t.current()
    for s in t.seats:
        if s.out:
            continue
        mark = "👉" if s is cur else "🏳" if s.folded else "🔥" if s.allin else "▫️"
        role = t.role_of(s)
        row = f"{mark} <b>{esc(s.name)}</b>{f' ({role})' if role else ''}: {money(d, s.stack)}"
        if s.bet:
            row += f" · в ставке {s.bet}"
        if s.folded:
            row += " · фолд"
        elif s.last:
            row += f" · {esc(s.last)}"
        lines.append(row)
    if cur and not t.runout:
        to_call = min(max(0, t.cur_bet - cur.bet), cur.stack)
        need = f"колл {to_call}" if to_call else "можно сделать чек"
        lines += ["", f"👉 Ход: {mention(cur.uid, cur.name)} · {need} · на ход {TURN_SEC} с"]
        lim = t.raise_limits(cur)
        if d.get("await_raise") == cur.uid and lim:
            lines.append(f"✏️ <i>Напиши в чат число: до какой суммы поднять ({lim[0]}–{lim[1]}).</i>")
    if note:
        lines += ["", note]
    return "\n".join(lines)


def table_markup(lobby: Lobby) -> InlineKeyboardMarkup:
    d, t, gid = lobby.data, lobby.data["table"], lobby.gid
    cur = t.current()
    mine = [my_cards_btn(gid), pb("🚪 Выйти", f"pk:{gid}:0:l")]
    if not cur or t.runout or t.street == "done":
        return InlineKeyboardMarkup(inline_keyboard=[mine])
    pre = f"pk:{gid}:{d['seq']}:"
    lim = t.raise_limits(cur)
    to_call = max(0, t.cur_bet - cur.bet)
    rows: List[List[InlineKeyboardButton]] = []
    if d.get("raise_menu") and lim:
        lo, hi = lim
        pot = t.pot()
        cands = [("Мин.", lo), ("½ банка", t.cur_bet + (pot + to_call) // 2),
                 ("Банк", t.cur_bet + pot + to_call), ("Олл-ин", hi)]
        seen, opts = set(), []
        for label, v in cands:
            v = max(lo, min(hi, v))
            if v not in seen:
                seen.add(v)
                opts.append(pb(f"{label} {v}", f"{pre}ra:{v}"))
        rows += [opts[i:i + 2] for i in range(0, len(opts), 2)]
        rows.append([pb("✏️ Своя сумма", pre + "cu")])
        rows.append([pb("◀️ Назад", pre + "b")])
    else:
        call = "✅ Чек" if to_call <= 0 else f"✅ Колл {min(to_call, cur.stack)}"
        rows.append([pb("🏳 Фолд", pre + "f"), pb(call, pre + "c")])
        if lim:
            rows.append([pb("⬆️ Ставка" if t.cur_bet == 0 else "⬆️ Рейз", pre + "r"),
                         pb(f"🔥 Олл-ин {cur.stack}", pre + "a")])
    rows.append(mine)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def hole_text(lobby: Lobby, s: Seat, final: bool = False) -> str:
    d, t = lobby.data, lobby.data["table"]
    txt = (f"{e('poker')} <b>Покер · раздача #{t.hand_no}</b>\n\n"
           f"Твои карты: {cards_html(s.hole)}\n")
    if t.board:
        txt += f"Стол: {cards_html(t.board)}\n"
    txt += f"Комбинация: <b>{esc(describe_cards(s.hole, t.board))}</b>\n\nСтек: {money(d, s.stack)}"
    if s.folded:
        txt += "\n<i>Ты сбросил карты.</i>"
    if final and t.result:
        net = t.result["won"].get(s.uid, 0) - s.total
        txt += f"\n<b>Итог раздачи: {'+' if net > 0 else '−' if net < 0 else ''}{abs(net)}</b>"
    txt += "\n<i>Эти карты видишь только ты.</i>"
    return txt


def my_cards_alert(lobby: Lobby, uid: int) -> str:
    t = lobby.data["table"]
    s = t.seat_of(uid)
    if not s:
        return "Ты не участвуешь в этой игре"
    if s.out:
        return "Ты уже не за столом"
    if not s.in_hand or not s.hole:
        return "Ты не в этой раздаче, дождись следующей"
    txt = f"Твои карты: {cards_plain(s.hole)}"
    if t.board:
        txt += f"\nСтол: {cards_plain(t.board)}"
    txt += f"\n{describe_cards(s.hole, t.board)}"
    if s.folded:
        txt += "\n(ты сбросил карты)"
    return txt


# ---------- сообщения ----------

async def _delete(bot, chat_id: int, message_id: Optional[int]):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def edit_table(lobby: Lobby, note: str = ""):
    d = lobby.data
    if not d.get("msg_id"):
        return
    try:
        await lobby.bot.edit_message_text(table_text(lobby, note), chat_id=lobby.chat_id,
                                          message_id=d["msg_id"], reply_markup=table_markup(lobby))
    except TelegramAPIError as ex:
        if "not modified" not in str(ex).lower():
            log.info("Покер: не удалось обновить стол: %s", ex)


async def send_table(lobby: Lobby, note: str = ""):
    """Новое сообщение со столом (старое удаляется): так чат не засоряется и стол всегда внизу."""
    d = lobby.data
    old = d.get("msg_id")
    msg = await lobby.bot.send_message(lobby.chat_id, table_text(lobby, note), reply_markup=table_markup(lobby))
    d["msg_id"] = msg.message_id
    await _delete(lobby.bot, lobby.chat_id, old)


async def send_holes(lobby: Lobby):
    """Раздача: каждому в личку приходят его карты и комбинация."""
    d, t = lobby.data, lobby.data["table"]
    bad = []
    for s in t.seats:
        if not s.in_hand:
            continue
        old = d["dm"].pop(s.uid, None)
        await _delete(lobby.bot, s.uid, old)
        msg = await dm(lobby.bot, s.uid, hole_text(lobby, s))
        if msg:
            d["dm"][s.uid] = msg.message_id
        elif s.uid not in d["dm_warned"]:
            d["dm_warned"].add(s.uid)
            bad.append(s.name)
    if bad:
        await lobby.bot.send_message(
            lobby.chat_id, "⚠️ Не смог написать в личку: " + ", ".join(f"<b>{esc(n)}</b>" for n in bad) +
                           "\n<i>Свои карты можно смотреть кнопкой «Мои карты» под столом.</i>")


async def refresh_holes(lobby: Lobby, final: bool = False):
    d, t = lobby.data, lobby.data["table"]

    async def one(s: Seat):
        mid = d["dm"].get(s.uid)
        if not mid:
            return
        try:
            await lobby.bot.edit_message_text(hole_text(lobby, s, final), chat_id=s.uid, message_id=mid)
        except TelegramAPIError:
            pass

    await asyncio.gather(*(one(s) for s in t.seats if s.in_hand and s.hole))


async def ping_turn(lobby: Lobby, s: Seat):
    if not NOTIFY_TURN:
        return
    d, t = lobby.data, lobby.data["table"]
    to_call = min(max(0, t.cur_bet - s.bet), s.stack)
    need = f"Колл: {to_call}" if to_call else "Можно сделать чек"
    markup = None
    cid = str(lobby.chat_id)
    if cid.startswith("-100") and d.get("msg_id"):
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
            text="Перейти к столу", url=f"https://t.me/c/{cid[4:]}/{d['msg_id']}")]])
    msg = await dm(lobby.bot, s.uid, f"👉 <b>Твой ход в покере!</b>\nКарты: {cards_html(s.hole)}\n"
                                     f"{need} · банк {money(d, t.pot())}", markup)
    d["ping"] = (s.uid, msg.message_id) if msg else None


async def clear_ping(lobby: Lobby):
    ping = lobby.data.get("ping")
    lobby.data["ping"] = None
    if ping:
        await _delete(lobby.bot, ping[0], ping[1])


# ---------- ход игры ----------

async def start_game(lobby: Lobby):
    stake = lobby.data.get("stake", 0)
    if stake:
        poor = {u: n for u, n in lobby.players.items() if (await ec.wallet(u))[0] < stake}
        for u in poor:
            lobby.players.pop(u, None)
        if poor and len(lobby.players) < 2:
            await lobby.bot.send_message(lobby.chat_id, "❌ Не у всех хватило рубинов на бай-ин, игра отменена.")
            finish(lobby)
            return
        if poor:
            await lobby.bot.send_message(
                lobby.chat_id, "⚠️ Без рубинов на бай-ин остаются за бортом: " +
                               ", ".join(f"<b>{esc(n)}</b>" for n in poor.values()))
    chips = stake or START_CHIPS
    order = list(lobby.players.items())
    random.shuffle(order)
    sb = max(1, chips // 100)
    table = Table([Seat(uid, name, chips) for uid, name in order], sb, sb * 2)
    lobby.data.update(table=table, chips0=chips, event=asyncio.Event(), pending=None, seq=0, raise_menu=False,
                      await_raise=None, msg_id=None, dm={}, ping=None, timeouts={}, dm_warned=set(), raised=0)
    lobby.task = asyncio.create_task(run(lobby))


async def wait_action(lobby: Lobby, seat: Seat):
    d, t = lobby.data, lobby.data["table"]
    try:
        await pause.wait_for(d["event"], TURN_SEC)
        d["timeouts"][seat.uid] = 0
        return d["pending"]
    except asyncio.TimeoutError:
        n = d["timeouts"].get(seat.uid, 0) + 1
        d["timeouts"][seat.uid] = n
        if n >= MAX_TIMEOUTS:
            seat.leaving = True
            await lobby.bot.send_message(
                lobby.chat_id, f"⌛ <b>{esc(seat.name)}</b> молчит уже {n} хода подряд и уходит из-за стола.")
            return (seat.uid, "fold", 0)
        return (seat.uid, "call" if t.cur_bet <= seat.bet else "fold", 0)


def perform(t: Table, act):
    uid, kind, amount = act
    try:
        if kind == "fold":
            t.fold(uid)
        elif kind == "call":
            t.check_call(uid)
        elif kind == "raise":
            t.raise_to(uid, amount)
        elif kind == "allin":
            t.all_in(uid)
    except ValueError:
        # на случай устаревшего действия: безопасный ход вместо зависания
        s = t.current()
        if s:
            (t.check_call if t.cur_bet <= s.bet else t.fold)(s.uid)


def result_text(lobby: Lobby) -> str:
    d, t = lobby.data, lobby.data["table"]
    res = t.result
    names = {s.uid: s.name for s in t.seats}
    lines = [f"🏁 <b>Раздача #{t.hand_no}</b>"]
    if res["kind"] == "fold":
        uid = res["pots"][0]["winners"][0]
        lines.append(f"<b>{esc(names[uid])}</b> забирает банк <b>{money(d, res['pots'][0]['amount'])}</b>: "
                     "все остальные сбросили карты.")
        return "\n".join(lines)
    lines.append(f"Стол: {cards_html(t.board)}\n")
    for uid, hole, descr in res["shown"]:
        mark = " 🏆" if res["won"].get(uid) else ""
        lines.append(f"• <b>{esc(names[uid])}</b>: {cards_html(hole)} — {esc(descr)}{mark}")
    lines.append("")
    for pot in res["pots"]:
        who = ", ".join(f"<b>{esc(names[u])}</b>" for u in pot["winners"])
        lines.append(f"💰 {esc(pot['label'])} {money(d, pot['amount'])} → {who}")
    return "\n".join(lines)


async def tidy(lobby: Lobby, after_hand: bool):
    """Убирает выбывших и ушедших, повышает блайнды. Пишет в чат, что изменилось."""
    d, t = lobby.data, lobby.data["table"]
    busted, left = t.cleanup()
    notes = []
    if busted:
        notes.append("💀 Остались без фишек: " + ", ".join(f"<b>{esc(s.name)}</b>" for s in busted))
    if left:
        notes.append("🚪 Покинули стол: " + ", ".join(f"<b>{esc(s.name)}</b> ({money(d, s.stack)})" for s in left))
    if after_hand and t.hand_no % BLIND_EVERY == 0 and d["raised"] != t.hand_no and len(t.alive()) > 1:
        d["raised"] = t.hand_no
        t.sb, t.bb = t.sb * 2, t.bb * 2
        notes.append(f"📈 Блайнды растут: <b>{t.sb}/{t.bb}</b>")
    if notes:
        await lobby.bot.send_message(lobby.chat_id, "\n".join(notes))


async def play_hand(lobby: Lobby):
    d, t, bot = lobby.data, lobby.data["table"], lobby.bot
    await send_holes(lobby)
    await send_table(lobby)
    revealed = False
    while t.street != "done":
        if t.runout:
            if not revealed:
                revealed = True
                live = [s for s in t.seats if s.in_hand and not s.folded]
                await bot.send_message(
                    lobby.chat_id, "🔥 <b>Все в олл-ине, карты открываются:</b>\n" + "\n".join(
                        f"• <b>{esc(s.name)}</b>: {cards_html(s.hole)}" for s in live))
            await pause.sleep(RUNOUT_SEC)
            t.step_runout()
            if t.street != "done":
                await send_table(lobby)
                await refresh_holes(lobby)
            continue
        seat = t.current()
        street = t.street
        d["seq"] += 1
        d["raise_menu"], d["await_raise"], d["pending"] = False, None, None
        d["event"].clear()
        await edit_table(lobby)
        await ping_turn(lobby, seat)
        act = await wait_action(lobby, seat)
        await clear_ping(lobby)
        perform(t, act)
        if t.street != street and t.street != "done":
            await send_table(lobby)
            await refresh_holes(lobby)
    await finish_hand(lobby)


async def run(lobby: Lobby):
    d, t, bot = lobby.data, lobby.data["table"], lobby.bot
    try:
        await bot.send_message(
            lobby.chat_id,
            f"{e('poker')} <b>Покер начинается!</b>\nИгроков: {len(t.seats)}, у каждого "
            f"<b>{money(d, d['chips0'])}</b>, блайнды {t.sb}/{t.bb}. Блайнды удваиваются каждые "
            f"{BLIND_EVERY} раздач. Карты и комбинации придут в личку, а кнопка «Мои карты» покажет их тут же.")
        while True:
            if not t.start_hand():
                break
            await play_hand(lobby)
            await tidy(lobby, after_hand=True)
            if len(t.alive()) < 2:
                break
            await pause.sleep(BETWEEN_HANDS_SEC)
            await tidy(lobby, after_hand=False)   # кто-то мог уйти за время паузы
            if len(t.alive()) < 2:
                break
        await finish_game(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в покере")
        try:
            await bot.send_message(lobby.chat_id, "⚠️ В покере случилась ошибка, игра остановлена. Рубины не списаны.")
        except Exception:
            pass
    finally:
        await clear_ping(lobby)
        await _delete(bot, lobby.chat_id, d.get("msg_id"))
        finish(lobby)


async def finish_hand(lobby: Lobby):
    d = lobby.data
    await _delete(lobby.bot, lobby.chat_id, d.get("msg_id"))
    d["msg_id"] = None
    await lobby.bot.send_message(lobby.chat_id, result_text(lobby))
    await refresh_holes(lobby, final=True)


async def settle(lobby: Lobby, final: Dict[int, int]) -> Dict[int, int]:
    """Рубины по итогам игры: проигравшие отдают (сколько есть на балансе), выигравшие делят собранное."""
    stake = lobby.data.get("stake", 0)
    if not stake:
        return {}
    net = {u: v - stake for u, v in final.items()}
    winners = {u: v for u, v in net.items() if v > 0}
    if not winners:
        return {}
    delta, collected = {}, 0
    for u, v in net.items():
        if v >= 0:
            continue
        pay = min(-v, (await ec.wallet(u))[0])
        if pay:
            await x("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (pay, u))
            collected += pay
            delta[u] = -pay
    total, given = sum(winners.values()), 0
    ranked = sorted(winners.items(), key=lambda kv: -kv[1])
    for i, (u, v) in enumerate(ranked):
        share = collected - given if i == len(ranked) - 1 else collected * v // total
        given += share
        if share:
            await ec.ensure(u)
            await x("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (share, u))
            delta[u] = share
    return delta


async def finish_game(lobby: Lobby):
    d, t = lobby.data, lobby.data["table"]
    final = {s.uid: s.stack for s in t.seats}
    delta = await settle(lobby, final)
    order = t.standings()
    top = max(final.values())
    winners = [u for u, v in final.items() if v == top]
    medals = [e("gold"), e("silver"), e("bronze")]
    lines = [f"{e('party')} <b>Покер: игра окончена</b>", ""]
    for i, s in enumerate(order):
        mark = medals[i] if i < 3 else f"{i + 1}."
        row = f"{mark} <b>{esc(s.name)}</b> — {money(d, s.stack)}"
        if d.get("stake") and delta.get(s.uid):
            row += f" ({'+' if delta[s.uid] > 0 else '−'}{abs(delta[s.uid])} {RUBY})"
        lines.append(row)
    lines.append("")
    lines.append(f"{e('crown')} Победитель: " + ", ".join(f"<b>{esc(t.seat_of(u).name)}</b>" for u in winners))
    if d.get("stake"):
        lines.append("<i>Рубины переведены по итогам игры.</i>")
    await lobby.bot.send_message(lobby.chat_id, "\n".join(lines))
    await record_results(KEY, [s.uid for s in t.seats], winners)


# ---------- кнопки ----------

def find_lobby(gid: int) -> Optional[Lobby]:
    lobby = next((l for l in lobbies.values() if l.gid == gid), None)
    if lobby and lobby.game.key == KEY and lobby.started and "table" in lobby.data:
        return lobby
    return None


@router.callback_query(F.data.startswith("pk:"))
async def poker_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    gid, seq, act = int(parts[1]), int(parts[2]), parts[3]
    lobby = find_lobby(gid)
    if not lobby:
        await cb.answer("Игра завершена", show_alert=True)
        return
    d, t, uid = lobby.data, lobby.data["table"], cb.from_user.id
    seat = t.seat_of(uid)
    if not seat:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return

    if act == "m":
        await cb.answer(my_cards_alert(lobby, uid), show_alert=True)
        return
    if act == "l":
        if seat.out or seat.leaving:
            await cb.answer("Ты уже уходишь из-за стола" if seat.leaving else "Ты уже не за столом", show_alert=True)
            return
        seat.leaving = True
        cur = t.current()
        if cur is seat and not d["event"].is_set():
            d["pending"] = (uid, "fold", 0)
            d["event"].set()
        await cb.answer("Ты уйдёшь из-за стола: карты в этой раздаче сбросятся, остаток фишек останется за тобой",
                        show_alert=True)
        return

    cur = t.current()
    if t.street in ("idle", "done") or t.runout or cur is None:
        await cb.answer("Сейчас ходить нельзя")
        return
    if cur.uid != uid:
        await cb.answer("Сейчас не твой ход", show_alert=True)
        return
    if seq != d["seq"]:
        await cb.answer("Эта кнопка устарела")
        return
    if d["event"].is_set():
        await cb.answer()
        return

    lim = t.raise_limits(cur)
    if act in ("f", "c", "a"):
        d["pending"] = (uid, {"f": "fold", "c": "call", "a": "allin"}[act], 0)
        d["event"].set()
        await cb.answer({"f": "Фолд", "c": "Принято", "a": "Олл-ин!"}[act])
    elif act == "ra":
        if not lim:
            await cb.answer("Рейз сейчас недоступен", show_alert=True)
            return
        to = int(parts[4])
        if not lim[0] <= to <= lim[1]:
            await cb.answer("Недопустимая сумма", show_alert=True)
            return
        d["pending"] = (uid, "raise", to)
        d["event"].set()
        await cb.answer(f"Рейз до {to}")
    elif act in ("r", "b", "cu"):
        if act != "b" and not lim:
            await cb.answer("Рейз сейчас недоступен", show_alert=True)
            return
        d["raise_menu"] = act != "b"
        d["await_raise"] = uid if act == "cu" else None
        await edit_table(lobby)
        await cb.answer("Напиши число в чат" if act == "cu" else None)
    else:
        await cb.answer()


class AwaitingRaise(Filter):
    """Сообщение-число от игрока, у которого сейчас ход и который выбрал «Своя сумма»."""

    async def __call__(self, message: Message) -> bool:
        if message.chat.id != GROUP_ID or not message.from_user or not message.text:
            return False
        lobby = lobbies.get(message.chat.id)
        if not lobby or not lobby.started or lobby.game.key != KEY or "table" not in lobby.data:
            return False
        return (lobby.data.get("await_raise") == message.from_user.id
                and bool(re.fullmatch(r"\d{1,9}", message.text.strip())))


@router.message(AwaitingRaise())
async def custom_raise(message: Message):
    lobby = lobbies.get(message.chat.id)
    d, t, uid = lobby.data, lobby.data["table"], message.from_user.id
    cur = t.current()
    if cur is None or cur.uid != uid or d["event"].is_set():
        return
    lim = t.raise_limits(cur)
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    if not lim:
        return
    n = min(int(message.text.strip()), lim[1])
    if n < lim[0]:
        tmp = await message.answer(f"⚠️ Минимум {lim[0]} (это «поднять до», а не «добавить»). Напиши число ещё раз.")
        asyncio.create_task(_forget(tmp))
        return
    d["pending"] = (uid, "raise", n)
    d["event"].set()


async def _forget(msg: Message, sec: int = 6):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


# ---------- запуск ----------

async def open_poker(bot, chat_id: int, user, stake: int = 0) -> Optional[str]:
    """Открывает лобби покера (stake = бай-ин в рубинах, 0 = игра на фишки). None или текст ошибки."""
    if stake and (await ec.wallet(user.id))[0] < stake:
        return "У тебя не хватает рубинов на такой бай-ин."
    err = await open_lobby(bot, chat_id, user, POKER)
    if err:
        return err
    lobby = lobbies.get(chat_id)
    if lobby and stake:
        lobby.data["stake"] = stake
        lobby.data["note"] = (f"Бай-ин: {stake} ♦️ = {stake} фишек. Рубины переходят от проигравших "
                              "к выигравшим по итогам игры (сколько есть на балансе).")
        await lobby.refresh()
    return None


@router.message(Command("poker"))
async def cmd_poker(message: Message):
    parts = (message.text or "").split()
    stake = 0
    if len(parts) > 1:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= MAX_STAKE:
            await message.answer(f"❌ Бай-ин: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/poker 50</code>")
            return
        stake = int(parts[1])
    err = await open_poker(message.bot, message.chat.id, message.from_user, stake)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


POKER = GameDef(
    key=KEY, title="Покер: Техасский холдем", emoji="poker", command="poker",
    description=("Без лимита, как в настоящем покере. Карты и комбинации приходят в личку. "
                 f"Без бай-ина играют на {START_CHIPS} фишек."),
    min_players=2, max_players=MAX_PLAYERS, start=start_game, needs_dm=True,
    dm_note="Карты приходят в личку: один раз нажми Start у бота.")
register(POKER)
