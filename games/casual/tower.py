"""Башня: игра в одиночку со ставкой в рубинах.

Запуск:
  • кнопка «Башня» в /games: бот спрашивает ставку (кнопкой или своей суммой), см. games/menu.py;
  • команда /tower <ставка>, например /tower 50.
Дальше игрок выбирает, сколько мин будет в ряду на верхних этажах (2, 3 или 4). Ставка списывается,
когда башня построена.

Башня из FLOORS этажей. На каждом этаже ряд клеток, часть из них мины. Игрок выбирает одну клетку:
  • безопасная: этаж пройден, бонус (множитель ставки) растёт, можно подниматься дальше или нажать «Забрать»;
  • мина: бонус обнуляется, ставка сгорает (конец игры).
Нижние этажи лёгкие: там ряд короче и мин меньше (внизу 1 мина на 4 клетки). Выше ряд становится шире и мин
больше, пока их число не дойдёт до выбранного на самом верхнем этаже. Мин на этаже f (с нуля) растёт равномерно
по всей башне: ceil(MAX * (f + 1) / FLOORS), а клеток на 3 больше, чем мин. Поэтому 2, 3 и 4 мины отличаются
по сложности с самых нижних этажей.

Множитель после k пройденных этажей считается честно:
  mult = (1 - EDGE) * Π w_f / (w_f - m_f), f = 0..k-1,
где w_f клеток и m_f мин на этаже f, то есть он обратно пропорционален шансу дойти до этого этажа;
EDGE это небольшая комиссия бота. Множитель ограничен MAX_MULT: дойдя до него (или до верхнего этажа),
выигрыш забирается автоматически.

Если игрок бросил башню и ничего не нажимает IDLE_SEC секунд, выигрыш забирается сам
(а если не пройдено ни одного этажа, ставка возвращается).
Игра одиночная, поэтому не занимает «слот» активной игры в чате. У игрока может быть одна башня за раз.
"""
import asyncio
import random
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import GROUP_ID
from database.game_stats import add_game_result
from database.moderation import is_user_banned
from games import pause
from games.casual.coinflip import check_stake
from games.duel.challenge import MAX_STAKE
from services import economy as ec
from utils.emoji import RUBY, e
from utils.fmt import esc
from utils.premium import raw

router = Router()

KEY = "tower"
TITLE = "Башня"
FLOORS = 12                                # сколько этажей в башне
MINE_OPTIONS = (2, 3, 4)                   # сколько мин в ряду на верхних этажах можно выбрать
EDGE = 0.03                                # комиссия бота: множитель чуть меньше «честного»
MAX_MULT = 1000.0                          # потолок множителя (дальше выигрыш забирается сам)
CHOICE_SEC = 60                            # сколько живёт меню выбора числа мин
IDLE_SEC = 120                             # через сколько бездействия выигрыш забирается автоматически

# Клетки башни выводим внутри raw(): бот заменяет обычные эмодзи на премиум-кастомные (они белые и сливаются),
# а в башне нужны обычные ⬜️ ✅ 💣.
TOWER, HIDDEN, SAFE, MINE, BOOM = "🗼", "\u2b1c\ufe0f", "\u2705", "💣", "💥"   # закрытая клетка, выбранная безопасная, мина, взрыв


@dataclass
class Game:
    token: str
    uid: int
    name: str
    stake: int
    max_mines: int
    layout: List[Set[int]]                   # мины каждого этажа (номера клеток)
    bot: object
    chat_id: int
    msg: Optional[Message] = None
    picks: List[int] = field(default_factory=list)   # выбранные клетки на пройденных этажах
    over: bool = False
    boom: Optional[int] = None               # клетка, на которой подорвался игрок
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: Optional[asyncio.Task] = None


games: Dict[str, Game] = {}                    # token -> идущая игра
by_user: Dict[int, str] = {}                   # uid -> token (одна башня на игрока)
pending: Dict[str, dict] = {}                  # token -> {uid, stake, msg, name, task}: меню выбора числа мин
pending_by_user: Dict[int, str] = {}
locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------- математика ----------

def floor_cfg(max_mines: int, f: int):
    """(клеток, мин) на этаже f (с нуля): ряд постепенно растёт, пока мин не станет max_mines."""
    m = max(1, (max_mines * (f + 1) + FLOORS - 1) // FLOORS)
    return m + 3, m


def raw_multiplier(passed: int, max_mines: int) -> float:
    """Множитель после passed пройденных этажей, без потолка."""
    if passed <= 0:
        return 1.0
    m = 1.0
    for f in range(passed):
        w, k = floor_cfg(max_mines, f)
        m *= w / (w - k)
    return m * (1 - EDGE)


def multiplier(passed: int, max_mines: int) -> float:
    return min(raw_multiplier(passed, max_mines), MAX_MULT)


def payout_of(stake: int, passed: int, max_mines: int) -> int:
    return int(stake * multiplier(passed, max_mines))


def fm(x: float) -> str:
    return f"×{x:.2f}"


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


# ---------- тексты и кнопки ----------

def _row(g: Game, f: int, picked: int, boom: bool = False) -> str:
    """Ряд этажа f после хода: выбранная клетка, мины и остальные закрытые клетки."""
    w, _ = floor_cfg(g.max_mines, f)
    out = []
    for i in range(w):
        if i == picked:
            out.append(BOOM if boom else SAFE)
        elif i in g.layout[f]:
            out.append(MINE)
        else:
            out.append(HIDDEN)
    return "".join(out)


def _history(g: Game, boom: bool = False) -> str:
    lines = [f"{f + 1}. {_row(g, f, c)}" for f, c in enumerate(g.picks)]
    if boom and g.boom is not None:
        f = len(g.picks)
        lines.append(f"{f + 1}. {_row(g, f, g.boom, boom=True)}")
    return "\n".join(lines)


def _widths_text() -> str:
    return ", ".join(f"<b>{n}</b> (ряд из {n + 3})" for n in MINE_OPTIONS)


def pick_text(name: str, stake: int, balance: int) -> str:
    return (f"{TOWER} <b>{TITLE}</b>\n"
            f"Игрок: <b>{esc(name)}</b>\n"
            f"Ставка: <b>{stake}</b> {RUBY}\n\n"
            f"Поднимайся на {FLOORS} этажей: на каждом выбирай одну безопасную клетку. Чем выше, тем шире ряд и "
            f"больше мин, пока их число не дойдёт до выбранного.\n"
            f"Выбери, сколько мин будет в ряду на верхних этажах: {_widths_text()}. Чем их больше, тем быстрее растёт бонус "
            f"и выше риск.\n"
            f"<i>Забирай выигрыш в любой момент: мина сжигает ставку. Максимум {fm(MAX_MULT)}.</i>\n"
            f"У тебя: <b>{balance}</b> {RUBY}")


def pick_markup(token: str) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text=f"{n} {MINE}", callback_data=f"twp:{token}:{n}") for n in MINE_OPTIONS]
    return InlineKeyboardMarkup(inline_keyboard=[
        row, [InlineKeyboardButton(text="🚫 Отмена", callback_data=f"twp:{token}:x")]])


def game_text(g: Game) -> str:
    k = len(g.picks)
    w, m = floor_cfg(g.max_mines, k)
    lines = [f"Ставка: <b>{g.stake}</b> {RUBY} · Мин в ряду: <b>{m}</b> {raw(MINE)} из <b>{w}</b> клеток"]
    now = f"Этаж <b>{k + 1}</b>/{FLOORS} · множитель <b>{fm(multiplier(k, g.max_mines))}</b>"
    if k:
        now += f" · выигрыш <b>{payout_of(g.stake, k, g.max_mines)}</b> {RUBY}"
    lines.append(now)
    lines.append(f"Следующий этаж: <b>{fm(multiplier(k + 1, g.max_mines))}</b>")
    hint = ("Выбери клетку: безопасная поднимает на этаж выше, мина сжигает ставку." if not k
            else "Нажми «Забрать», чтобы сохранить выигрыш, или рискни дальше.")
    hist = _history(g)
    return (f"{TOWER} <b>{TITLE}</b> · <b>{esc(g.name)}</b>\n"
            f"<blockquote>{chr(10).join(lines)}</blockquote>\n"
            + (f"\n{raw(hist)}\n\n" if hist else "") + f"<i>{hint}</i>")


def game_markup(g: Game) -> InlineKeyboardMarkup:
    k = len(g.picks)
    w, _ = floor_cfg(g.max_mines, k)
    rows = [[InlineKeyboardButton(text=HIDDEN, callback_data=f"twg:{g.token}:{i}") for i in range(w)]]
    if k:
        rows.append([InlineKeyboardButton(text=f"💰 Забрать {payout_of(g.stake, k, g.max_mines)} {RUBY}",
                                          callback_data=f"twg:{g.token}:c")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def final_markup(g: Game, balance: int) -> Optional[InlineKeyboardMarkup]:
    if balance < g.stake:
        return None
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🔄 Ещё раз ({g.stake} · {g.max_mines} {MINE})",
        callback_data=f"twa:{g.uid}:{g.stake}:{g.max_mines}")]])


# ---------- вспомогательное ----------

async def _drop(msg: Optional[Message]):
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


def _forget_pending(token: str) -> Optional[dict]:
    p = pending.pop(token, None)
    if p:
        if pending_by_user.get(p["uid"]) == token:
            pending_by_user.pop(p["uid"], None)
        task = p.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
    return p


async def _expire_pending(token: str):
    await asyncio.sleep(CHOICE_SEC)
    p = _forget_pending(token)
    if p:
        await _drop(p["msg"])


def _restart_idle(g: Game):
    if g.task and g.task is not asyncio.current_task():
        g.task.cancel()
    g.task = asyncio.create_task(_idle(g.token))


async def _idle(token: str):
    await asyncio.sleep(IDLE_SEC)
    g = games.get(token)
    if not g:
        return
    async with g.lock:
        if not g.over:
            await _end(g, "idle")


async def _edit(g: Game, text: str, markup: InlineKeyboardMarkup):
    try:
        await g.msg.edit_text(text, reply_markup=markup)
    except TelegramAPIError:
        pass


class _Refund(Exception):
    pass


async def _end(g: Game, kind: str, boom: Optional[int] = None):
    """Завершает игру (вызывать под g.lock). kind: boom | cash | max | top | idle."""
    if g.over:                                 # уже завершена: повторно не платим
        return
    k, m = len(g.picks), g.max_mines
    who = f"<b>{esc(g.name)}</b>"
    result = None
    if kind == "boom":
        won = 0
    elif k == 0:                               # бросил башню, не сделав ни шага: ставка возвращается
        won = g.stake
    else:
        won = payout_of(g.stake, k, m)
    if won:
        # Деньги раньше изменения состояния: если credit упадёт, игра останется живой и выплату можно повторить.
        await ec.credit(g.uid, won)
    g.over = True
    games.pop(g.token, None)
    if by_user.get(g.uid) == g.token:
        by_user.pop(g.uid, None)
    if g.task and g.task is not asyncio.current_task():
        g.task.cancel()
    g.boom = boom
    try:
        balance = (await ec.wallet(g.uid))[0]
    except Exception:
        balance = 0
    bal = f"Баланс: <b>{balance}</b> {RUBY}"
    info = f"Мин в ряду: до <b>{m}</b> · пройдено этажей: <b>{k}</b> из {FLOORS}"

    if kind == "boom":
        result = "loss"
        text = (f"{e('skull')} {who} наступил(а) на мину на этаже <b>{k + 1}</b>: <b>−{g.stake}</b> {RUBY}\n"
                f"<blockquote>{info}\n{bal}</blockquote>")
    elif k == 0:
        text = (f"⌛ {who} не сделал(а) ход, ставка <b>{g.stake}</b> {RUBY} возвращена\n"
                f"<blockquote>{bal}</blockquote>")
    else:
        mult = fm(multiplier(k, m))
        result = "win" if won > g.stake else "draw"
        tail = {"idle": "\n<i>Время вышло, выигрыш забран автоматически.</i>",
                "top": "\n<i>Башня покорена, выигрыш забран.</i>",
                "max": f"\n<i>Достигнут максимум {fm(MAX_MULT)}, выигрыш забран.</i>"}.get(kind, "")
        head = (f"{e('party')} {who} забирает <b>{mult}</b>: <b>+{won - g.stake}</b> {RUBY}" if won > g.stake
                else f"🤝 {who} забирает ставку обратно")
        text = (f"{head}\n<blockquote>Ставка <b>{g.stake}</b> → <b>{won}</b> {RUBY}\n{info}\n{bal}</blockquote>{tail}")

    hist = _history(g, boom=(kind == "boom"))
    if hist:
        text += f"\n\n{raw(hist)}"

    markup = final_markup(g, balance)
    try:
        await g.msg.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await g.bot.send_message(g.chat_id, text, reply_markup=markup)
        except Exception:
            pass
    if result:
        try:
            await add_game_result(g.uid, KEY, result)
        except Exception:
            pass


async def _check(chat_id: int, user) -> Optional[str]:
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if user.id in by_user:
        return "У тебя уже идёт игра в башню, доиграй её."
    return None


# ---------- запуск ----------

async def open_tower(bot, chat_id: int, user, stake: int = 0) -> Optional[str]:
    """Показывает выбор числа мин. Возвращает None или текст ошибки."""
    err = await _check(chat_id, user)
    if err:
        return err
    balance = (await ec.wallet(user.id))[0]
    err = check_stake(stake, balance)
    if err:
        return err
    old = pending_by_user.get(user.id)
    if old:                                   # старое меню убираем, остаётся одно
        p = _forget_pending(old)
        if p:
            await _drop(p["msg"])
    token = secrets.token_hex(3)
    name = _name(user)
    msg = await bot.send_message(chat_id, pick_text(name, stake, balance), reply_markup=pick_markup(token))
    pending[token] = {"uid": user.id, "stake": stake, "msg": msg, "name": name,
                      "task": asyncio.create_task(_expire_pending(token))}
    pending_by_user[user.id] = token
    return None


def _build_layout(max_mines: int) -> List[Set[int]]:
    rng = random.SystemRandom()
    layout = []
    for f in range(FLOORS):
        w, m = floor_cfg(max_mines, f)
        layout.append(set(rng.sample(range(w), m)))
    return layout


async def _begin(bot, chat_id: int, user, stake: int, max_mines: int, msg: Optional[Message] = None) -> Optional[str]:
    """Списывает ставку и строит башню. Возвращает None или текст ошибки."""
    async with locks[user.id]:
        if user.id in by_user:
            return "У тебя уже идёт игра в башню, доиграй её."
        # Ставка списывается сразу и только если рубины есть: повторное нажатие не потратит её дважды.
        if not await ec.debit(user.id, stake):
            return "У тебя не хватает рубинов на такую ставку."
        try:
            g = Game(token=secrets.token_hex(4), uid=user.id, name=_name(user), stake=stake, max_mines=max_mines,
                     layout=_build_layout(max_mines), bot=bot, chat_id=chat_id)
            text, markup = game_text(g), game_markup(g)
            try:
                if msg:
                    await msg.edit_text(text, reply_markup=markup)
                    g.msg = msg
                else:
                    g.msg = await bot.send_message(chat_id, text, reply_markup=markup)
            except TelegramAPIError:
                try:
                    g.msg = await bot.send_message(chat_id, text, reply_markup=markup)
                except TelegramAPIError:
                    raise _Refund()
            games[g.token] = g
            by_user[user.id] = g.token
            _restart_idle(g)
        except _Refund:
            await asyncio.shield(ec.credit(user.id, stake))
            return "Не удалось запустить игру, ставка возвращена."
        except BaseException:
            if user.id not in by_user or games.get(by_user[user.id]) is None:
                await asyncio.shield(ec.credit(user.id, stake))
            raise
        return None


@router.message(Command("tower"))
async def cmd_tower(message: Message):
    parts = (message.text or "").split()
    usage = (f"{TOWER} <b>{TITLE}</b>\nСделай ставку командой: <code>/tower 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
             "Или нажми «Башня» в /games, там ставка выбирается кнопками.\n\n"
             f"<i>Выбираешь число мин в ряду и поднимаешься на {FLOORS} этажей: безопасная клетка растит бонус, "
             f"мина {raw(MINE)} сжигает ставку. Выигрыш можно забрать в любой момент.</i>")
    if len(parts) < 2:
        await message.answer(usage)
        return
    if not parts[1].isdigit():
        await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/tower 50</code>")
        return
    err = await open_tower(message.bot, message.chat.id, message.from_user, int(parts[1]))
    if err:
        await message.answer(f"⚠️ {esc(err)}")


# ---------- кнопки ----------

@router.callback_query(F.data.startswith("twp:"))
async def twp_cb(cb: CallbackQuery):
    _, token, act = cb.data.split(":")
    p = pending.get(token)
    if not p:
        await cb.answer("Это меню уже закрыто", show_alert=True)
        return
    if cb.from_user.id != p["uid"]:
        await cb.answer("Это ставка другого игрока", show_alert=True)
        return
    if act == "x":
        _forget_pending(token)
        await _drop(p["msg"])
        await cb.answer("Ставка отменена")
        return
    if not act.isdigit() or int(act) not in MINE_OPTIONS:
        await cb.answer()
        return
    from services import features
    if not features.is_on(KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await _check(cb.message.chat.id, cb.from_user)
    if err:
        await cb.answer(err, show_alert=True)
        return
    if pending.get(token) is not p:           # пока шли проверки, меню уже обработали
        await cb.answer()
        return
    _forget_pending(token)                    # забираем меню до игры: двойной клик не создаст две башни
    err = await _begin(cb.bot, cb.message.chat.id, cb.from_user, p["stake"], int(act), msg=p["msg"])
    if err:
        await cb.answer(err, show_alert=True)
        await _drop(p["msg"])
        return
    await cb.answer("Башня готова!")


@router.callback_query(F.data.startswith("twg:"))
async def twg_cb(cb: CallbackQuery):
    _, token, act = cb.data.split(":")
    g = games.get(token)
    if not g:
        await cb.answer("Эта игра уже закончена", show_alert=True)
        return
    if cb.from_user.id != g.uid:
        await cb.answer("Это башня другого игрока", show_alert=True)
        return
    async with g.lock:
        if g.over:
            await cb.answer()
            return
        if act == "c":
            if not g.picks:
                await cb.answer("Сначала поднимись хотя бы на один этаж", show_alert=True)
                return
            await _end(g, "cash")
            await cb.answer("Выигрыш забран!")
            return
        f = len(g.picks)
        w, _ = floor_cfg(g.max_mines, f)
        if not act.isdigit() or not 0 <= int(act) < w:
            await cb.answer()
            return
        cell = int(act)
        if cell in g.layout[f]:
            await _end(g, "boom", boom=cell)
            await cb.answer("Бум! 💥")
            return
        g.picks.append(cell)
        k = len(g.picks)
        if k >= FLOORS:
            await _end(g, "top")
            await cb.answer("Вершина!")
            return
        if raw_multiplier(k, g.max_mines) >= MAX_MULT:
            await _end(g, "max")
            await cb.answer("Максимум!")
            return
        _restart_idle(g)
        await _edit(g, game_text(g), game_markup(g))
        await cb.answer()


@router.callback_query(F.data.startswith("twa:"))
async def tower_again(cb: CallbackQuery):
    _, uid, stake, max_mines = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    if int(max_mines) not in MINE_OPTIONS:
        await cb.answer()
        return
    from services import features
    if not features.is_on(KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await _check(cb.message.chat.id, cb.from_user)
    if not err:
        err = check_stake(int(stake), (await ec.wallet(cb.from_user.id))[0])
    if not err:
        err = await _begin(cb.bot, cb.message.chat.id, cb.from_user, int(stake), int(max_mines))
    if err:
        await cb.answer(err, show_alert=True)
        return
    try:                                      # у старого итога убираем кнопку «Ещё раз»
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await cb.answer("Новая башня!")
