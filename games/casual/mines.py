"""Сапёр: игра в одиночку со ставкой в рубинах.

Запуск:
  • кнопка «Сапёр» в /games: бот спрашивает ставку (кнопкой или своей суммой), см. games/menu.py;
  • команда /mines <ставка>, например /mines 50.
Дальше игрок выбирает, сколько мин будет на поле 5×5. Ставка списывается, когда поле создано.
Каждая клетка либо алмаз (💎), либо мина (💣):
  • алмаз увеличивает бонус (множитель ставки), клетки можно открывать дальше или нажать «Забрать»;
  • мина обнуляет бонус, и ставка сгорает (конец игры).
Чем больше мин, тем быстрее растёт множитель за каждый алмаз и выше риск.

Множитель после k открытых алмазов при m минах на поле из N клеток считается честно:
  mult = (1 - EDGE) * Π (N - i) / (N - m - i), i = 0..k-1,
то есть он обратно пропорционален шансу дойти до этой клетки; EDGE это небольшая комиссия бота.
Множитель ограничен MAX_MULT: дойдя до него, выигрыш забирается автоматически.

Если игрок бросил поле и ничего не нажимает IDLE_SEC секунд, выигрыш забирается сам
(а если не открыто ни одной клетки, ставка возвращается).
Игра одиночная, поэтому не занимает «слот» активной игры в чате. У игрока может быть одно поле за раз.
"""
import asyncio
import random
import secrets
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

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
from utils.logs import logger
from utils.tasks import spawn

router = Router()

KEY = "mines"
TITLE = "Сапёр"
SIZE = 5                                   # поле SIZE × SIZE
CELLS = SIZE * SIZE
MINE_OPTIONS = (3, 6, 9, 12)  # сколько мин можно выбрать
EDGE = 0.03                                # комиссия бота: множитель чуть меньше «честного»
MAX_MULT = 50.0                            # потолок множителя (дальше выигрыш забирается сам)
CHOICE_SEC = 60                            # сколько живёт меню выбора числа мин
IDLE_SEC = 120                             # через сколько бездействия выигрыш забирается автоматически

HIDDEN, GEM, MINE, BOOM, EMPTY = "⬜", "💎", "💣", "💥", "▫️"


@dataclass
class Game:
    token: str
    uid: int
    name: str
    stake: int
    mines: Set[int]
    bot: object
    chat_id: int
    msg: Optional[Message] = None
    opened: Set[int] = field(default_factory=set)
    over: bool = False
    boom: Optional[int] = None               # клетка, на которой подорвался игрок
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    task: Optional[asyncio.Task] = None
    render_task: Optional[asyncio.Task] = None   # идущая правка сообщения (клики не ждут её)
    dirty: bool = False                          # пока шла правка, состояние поменялось ещё раз

    @property
    def safe_total(self) -> int:
        return CELLS - len(self.mines)


games: Dict[str, Game] = {}                    # token -> идущая игра
by_user: Dict[int, str] = {}                   # uid -> token (одно поле на игрока)
pending: Dict[str, dict] = {}                  # token -> {uid, stake, msg, name, task}: меню выбора числа мин
pending_by_user: Dict[int, str] = {}
locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


# ---------- математика ----------

def raw_multiplier(opened: int, mines: int) -> float:
    """Множитель после opened открытых алмазов, без потолка."""
    if opened <= 0:
        return 1.0
    if opened > CELLS - mines:               # алмазов на поле меньше, чем клеток «открыто»: недостижимо
        return float("inf")
    m = 1.0
    for i in range(opened):
        m *= (CELLS - i) / (CELLS - mines - i)
    return m * (1 - EDGE)


def multiplier(opened: int, mines: int) -> float:
    return min(raw_multiplier(opened, mines), MAX_MULT)


def payout_of(stake: int, opened: int, mines: int) -> int:
    return int(stake * multiplier(opened, mines))


def fm(x: float) -> str:
    return f"×{x:.2f}"


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


# ---------- тексты и кнопки ----------

def pick_text(name: str, stake: int, balance: int) -> str:
    return (f"{MINE} <b>{TITLE}</b>\n"
            f"Игрок: <b>{esc(name)}</b>\n"
            f"Ставка: <b>{stake}</b> {RUBY}\n\n"
            f"Выбери, сколько мин спрятано на поле {SIZE}×{SIZE}. Чем их больше, тем быстрее растёт бонус "
            f"за каждый алмаз и выше риск. Рядом с числом мин указан множитель за первый алмаз.\n"
            f"<i>Забирай выигрыш в любой момент: мина сжигает ставку. Максимум {fm(MAX_MULT)}.</i>\n"
            f"У тебя: <b>{balance}</b> {RUBY}")


def pick_markup(token: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for n in MINE_OPTIONS:
        row.append(InlineKeyboardButton(text=f"{n} {MINE} · {fm(multiplier(1, n))}", callback_data=f"mn:{token}:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="🚫 Отмена", callback_data=f"mn:{token}:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def game_text(g: Game) -> str:
    k, m = len(g.opened), len(g.mines)
    lines = [f"Ставка: <b>{g.stake}</b> {RUBY} · Мин: <b>{m}</b> {MINE}",
             f"Открыто: <b>{k}</b> из {g.safe_total}"]
    if k:
        lines.append(f"Сейчас: <b>{fm(multiplier(k, m))}</b> → <b>{payout_of(g.stake, k, m)}</b> {RUBY}")
    lines.append(f"Следующий алмаз: <b>{fm(multiplier(k + 1, m))}</b>")
    hint = ("Открывай клетки: алмаз растит бонус, мина сжигает ставку." if not k
            else "Нажми «Забрать», чтобы сохранить выигрыш, или рискни дальше.")
    return (f"{MINE} <b>{TITLE}</b> · <b>{esc(g.name)}</b>\n"
            f"<blockquote>{chr(10).join(lines)}</blockquote>\n<i>{hint}</i>")


def _grid(g: Game, reveal: bool = False, boom: Optional[int] = None):
    rows = []
    for r in range(SIZE):
        row = []
        for c in range(SIZE):
            i = r * SIZE + c
            if reveal:
                text = BOOM if i == boom else MINE if i in g.mines else GEM if i in g.opened else EMPTY
            else:
                text = GEM if i in g.opened else HIDDEN
            row.append(InlineKeyboardButton(text=text, callback_data=f"mg:{g.token}:{i}"))
        rows.append(row)
    return rows


def game_markup(g: Game) -> InlineKeyboardMarkup:
    rows = _grid(g)
    k = len(g.opened)
    if k:
        rows.append([InlineKeyboardButton(text=f"💰 Забрать {payout_of(g.stake, k, len(g.mines))} {RUBY}",
                                          callback_data=f"mg:{g.token}:c")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def final_markup(g: Game, balance: int) -> InlineKeyboardMarkup:
    rows = _grid(g, reveal=True, boom=g.boom)
    if balance >= g.stake:
        rows.append([InlineKeyboardButton(text=f"🔄 Ещё раз ({g.stake} · {len(g.mines)} {MINE})",
                                          callback_data=f"mga:{g.uid}:{g.stake}:{len(g.mines)}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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


async def _render_loop(g: Game):
    """Правит сообщение по последнему состоянию. Быстрые клики склеиваются: пока идёт запрос к Telegram,
    новые нажатия лишь помечают состояние «грязным», и после ответа сообщение правится ещё раз, но один раз."""
    while True:
        g.dirty = False
        if g.over:
            return
        await _edit(g, game_text(g), game_markup(g))
        if not g.dirty:
            return


def _schedule_render(g: Game):
    if g.render_task and not g.render_task.done():
        g.dirty = True
        return
    g.render_task = spawn(_render_loop(g), name="mines_render")


class _Refund(Exception):
    pass


async def _end(g: Game, kind: str, boom: Optional[int] = None):
    """Завершает игру (вызывать под g.lock). kind: boom | cash | max | idle."""
    if g.over:                                 # уже завершена: повторно не платим
        return
    k, m = len(g.opened), len(g.mines)
    who = f"<b>{esc(g.name)}</b>"
    result = None
    if kind == "boom":
        won = 0
    elif k == 0:                               # бросил поле, ничего не открыв: ставка возвращается
        won = g.stake
    else:
        won = payout_of(g.stake, k, m)
    if won:
        # Уникальный ключ не даст повторно выплатить выигрыш при повторном _end.
        await ec.credit_once(f"{KEY}:{g.token}:payout", g.uid, won)
    g.over = True
    games.pop(g.token, None)
    if by_user.get(g.uid) == g.token:
        by_user.pop(g.uid, None)
    if g.task and g.task is not asyncio.current_task():
        g.task.cancel()
    g.boom = boom
    rt = g.render_task
    if rt and not rt.done() and rt is not asyncio.current_task():
        try:                                   # итог должен прийти последним, а не затереться промежуточной правкой
            await asyncio.wait_for(asyncio.shield(rt), 5)
        except Exception:
            pass
    try:
        balance = (await ec.wallet(g.uid))[0]
    except Exception:
        logger.exception("Не удалось получить баланс игрока %s после игры в сапёр", g.uid)
        balance = 0
    bal = f"Баланс: <b>{balance}</b> {RUBY}"
    info = f"Мин: <b>{m}</b> · открыто: <b>{k}</b> из {g.safe_total}"

    if kind == "boom":
        result = "loss"
        text = (f"{e('skull')} {who} наступил(а) на мину: <b>−{g.stake}</b> {RUBY}\n"
                f"<blockquote>{info}\n{bal}</blockquote>")
    elif k == 0:
        text = (f"⌛ {who} не сделал(а) ход, ставка <b>{g.stake}</b> {RUBY} возвращена\n"
                f"<blockquote>{bal}</blockquote>")
    else:
        mult = fm(multiplier(k, m))
        result = "win" if won > g.stake else "draw"
        tail = {"idle": "\n<i>Время вышло, выигрыш забран автоматически.</i>",
                "max": f"\n<i>Достигнут максимум {fm(MAX_MULT)}, выигрыш забран.</i>"}.get(kind, "")
        head = (f"{e('party')} {who} забирает <b>{mult}</b>: <b>+{won - g.stake}</b> {RUBY}" if won > g.stake
                else f"🤝 {who} забирает ставку обратно")
        text = (f"{head}\n<blockquote>Ставка <b>{g.stake}</b> → <b>{won}</b> {RUBY}\n{info}\n{bal}</blockquote>{tail}")

    markup = final_markup(g, balance)
    try:
        await g.msg.edit_text(text, reply_markup=markup)
    except Exception:
        try:
            await g.bot.send_message(g.chat_id, text, reply_markup=markup)
        except Exception:
            logger.debug("Не удалось отправить итог сапёра в чат %s", g.chat_id, exc_info=True)
    if result:
        try:
            await add_game_result(g.uid, KEY, result)
        except Exception:
            logger.exception("Не удалось записать результат сапёра для игрока %s", g.uid)


async def _check(chat_id: int, user) -> Optional[str]:
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if user.id in by_user:
        return "У тебя уже идёт игра в сапёра, доиграй её."
    return None


# ---------- запуск ----------

async def open_mines(bot, chat_id: int, user, stake: int = 0) -> Optional[str]:
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


async def _begin(bot, chat_id: int, user, stake: int, mines: int, msg: Optional[Message] = None) -> Optional[str]:
    """Списывает ставку и создаёт поле. Возвращает None или текст ошибки."""
    async with locks[user.id]:
        if user.id in by_user:
            return "У тебя уже идёт игра в сапёра, доиграй её."
        # Ставка списывается сразу и только если рубины есть: повторное нажатие не потратит её дважды.
        if not await ec.debit(user.id, stake):
            return "У тебя не хватает рубинов на такую ставку."
        try:
            g = Game(token=secrets.token_hex(4), uid=user.id, name=_name(user), stake=stake,
                     mines=set(random.SystemRandom().sample(range(CELLS), mines)), bot=bot, chat_id=chat_id)
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


@router.message(Command("mines"))
async def cmd_mines(message: Message):
    parts = (message.text or "").split()
    usage = (f"{MINE} <b>{TITLE}</b>\nСделай ставку командой: <code>/mines 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
             "Или нажми «Сапёр» в /games, там ставка выбирается кнопками.\n\n"
             f"<i>Выбираешь число мин на поле {SIZE}×{SIZE} и открываешь клетки: каждый алмаз {GEM} растит бонус, "
             f"мина {MINE} сжигает ставку. Выигрыш можно забрать в любой момент.</i>")
    if len(parts) < 2:
        await message.answer(usage)
        return
    if not parts[1].isdigit():
        await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/mines 50</code>")
        return
    err = await open_mines(message.bot, message.chat.id, message.from_user, int(parts[1]))
    if err:
        await message.answer(f"⚠️ {esc(err)}")


# ---------- кнопки ----------

@router.callback_query(F.data.startswith("mn:"))
async def mn_cb(cb: CallbackQuery):
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
    _forget_pending(token)                    # забираем меню до игры: двойной клик не создаст два поля
    err = await _begin(cb.bot, cb.message.chat.id, cb.from_user, p["stake"], int(act), msg=p["msg"])
    if err:
        await cb.answer(err, show_alert=True)
        await _drop(p["msg"])
        return
    await cb.answer("Поле готово!")


@router.callback_query(F.data.startswith("mg:"))
async def mg_cb(cb: CallbackQuery):
    _, token, act = cb.data.split(":")
    g = games.get(token)
    if not g:
        await cb.answer("Эта игра уже закончена", show_alert=True)
        return
    if cb.from_user.id != g.uid:
        await cb.answer("Это поле другого игрока", show_alert=True)
        return
    async with g.lock:
        if g.over:
            await cb.answer()
            return
        if act == "c":
            if not g.opened:
                await cb.answer("Сначала открой хотя бы одну клетку", show_alert=True)
                return
            await cb.answer("Выигрыш забран!")
            await _end(g, "cash")
            return
        if not act.isdigit() or not 0 <= int(act) < CELLS:
            await cb.answer()
            return
        cell = int(act)
        if cell in g.opened:
            await cb.answer()
            return
        if cell in g.mines:
            await cb.answer("Бум! 💥")
            await _end(g, "boom", boom=cell)
            return
        g.opened.add(cell)
        if len(g.opened) >= g.safe_total or raw_multiplier(len(g.opened), len(g.mines)) >= MAX_MULT:
            await cb.answer("Максимум!")
            await _end(g, "max")
            return
        _restart_idle(g)
    await cb.answer()                         # сразу, без ожидания правки сообщения
    _schedule_render(g)


@router.callback_query(F.data.startswith("mga:"))
async def mines_again(cb: CallbackQuery):
    _, uid, stake, mines = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    if int(mines) not in MINE_OPTIONS:
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
        err = await _begin(cb.bot, cb.message.chat.id, cb.from_user, int(stake), int(mines))
    if err:
        await cb.answer(err, show_alert=True)
        return
    try:                                      # у старого итога убираем кнопку «Ещё раз», остаётся только поле
        rows = cb.message.reply_markup.inline_keyboard[:-1]
        await cb.message.edit_reply_markup(reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))
    except (TelegramAPIError, AttributeError):
        pass
    await cb.answer("Новое поле!")
