"""Орёл и решка: игра в одиночку со ставкой в рубинах.

Запуск:
  • кнопка «Орёл и решка» в /games: бот спрашивает ставку (кнопкой или своей суммой), см. games/menu.py;
  • команда /coin <ставка>, например /coin 50.
Дальше бот показывает кнопки «Орёл» и «Решка». После выбора ставка списывается, бот подбрасывает монету
(случайно 50/50) и правкой того же сообщения пишет итог (стикера и новых сообщений нет, чтобы не упираться в флуд-лимит группы): угадал — ставка умножается на COEF,
не угадал — ставка остаётся у бота.

Ставка обязательна (минимум 1 рубин). Игра одиночная, поэтому не занимает «слот» активной игры в чате
и не мешает лобби и играм на двоих.
"""
import asyncio
import secrets
from collections import defaultdict
from typing import Dict, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import GROUP_ID
from database.game_stats import add_game_result
from database.moderation import is_user_banned
from games import pause
from games.duel.challenge import MAX_STAKE
from services import economy as ec
from utils.emoji import RUBY, e
from utils.fmt import esc
from utils.logs import logger
from utils.ui import kb

router = Router()

KEY = "coin"
TITLE = "Орёл и решка"
COEF = 2          # во сколько раз умножается ставка при выигрыше (выплата = ставка * COEF)
TOSS_PAUSE = 0.8  # пауза между «Подбрасываю…» и итогом (оба раза правится одно и то же сообщение)
CHOICE_SEC = 60   # сколько живут кнопки выбора стороны

SIDE = {"heads": "Орёл", "tails": "Решка"}
SIDE_ICON = {"heads": "🦅", "tails": "🪙"}
SHORT = {"h": "heads", "t": "tails"}

pending: Dict[str, dict] = {}                  # token -> {uid, stake, msg, task, name}
by_user: Dict[int, str] = {}                   # uid -> token (у игрока одно открытое меню выбора)
locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


def choice_text(name: str, stake: int, balance: Optional[int] = None) -> str:
    t = (f"🪙 <b>{TITLE}</b>\n"
         f"Игрок: <b>{esc(name)}</b>\n"
         f"Ставка: <b>{stake}</b> {RUBY}\n"
         f"Выигрыш при угадывании: <b>{stake * COEF}</b> {RUBY}\n\n"
         f"<i>Выбери сторону монеты.</i>")
    if balance is not None:
        t += f"\nУ тебя: <b>{balance}</b> {RUBY}"
    return t


def choice_markup(token: str):
    return kb([[(f"{SIDE_ICON['heads']} Орёл", f"cf:{token}:h"), (f"{SIDE_ICON['tails']} Решка", f"cf:{token}:t")],
               [("🚫 Отмена", f"cf:{token}:x")]])


def again_markup(uid: int, stake: int):
    return kb([[(f"🔄 Ещё раз ({stake})", f"cfa:{uid}:{stake}")]])


async def _drop(msg: Optional[Message]):
    if not msg:
        return
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


def _forget(token: str) -> Optional[dict]:
    p = pending.pop(token, None)
    if p:
        if by_user.get(p["uid"]) == token:
            by_user.pop(p["uid"], None)
        task = p.get("task")
        if task and task is not asyncio.current_task():
            task.cancel()
    return p


async def _expire(token: str):
    await asyncio.sleep(CHOICE_SEC)
    p = _forget(token)
    if p:
        await _drop(p["msg"])


def check_stake(stake: int, balance: int) -> Optional[str]:
    if stake < 1:
        return "В этой игре нужна ставка: от 1 рубина."
    if stake > MAX_STAKE:
        return f"Максимальная ставка: {MAX_STAKE} рубинов."
    if balance < stake:
        return "У тебя не хватает рубинов на такую ставку."
    return None


async def open_coin(bot, chat_id: int, user, stake: int = 0, msg: Optional[Message] = None) -> Optional[str]:
    """Показывает кнопки выбора стороны. Возвращает None или текст ошибки."""
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    balance = (await ec.wallet(user.id))[0]
    err = check_stake(stake, balance)
    if err:
        return err
    old = by_user.get(user.id)
    if old:                                   # старое меню убираем, остаётся одно
        p = _forget(old)
        if p:
            await _drop(p["msg"])
    token = secrets.token_hex(3)
    name = _name(user)
    text, markup = choice_text(name, stake, balance), choice_markup(token)
    if msg is not None:                       # «Ещё раз»: меню рисуем на месте итога, без нового сообщения
        try:
            await msg.edit_text(text, reply_markup=markup)
        except TelegramAPIError:
            msg = None
    if msg is None:
        msg = await bot.send_message(chat_id, text, reply_markup=markup)
    pending[token] = {"uid": user.id, "stake": stake, "msg": msg, "name": name,
                      "task": asyncio.create_task(_expire(token))}
    by_user[user.id] = token
    return None


@router.message(Command("coin"))
async def cmd_coin(message: Message):
    parts = (message.text or "").split()
    usage = (f"🪙 <b>{TITLE}</b>\nСделай ставку командой: <code>/coin 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
             "Или нажми «Орёл и решка» в /games, там ставка выбирается кнопками.")
    if len(parts) < 2:
        await message.answer(usage)
        return
    if not parts[1].isdigit():
        await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/coin 50</code>")
        return
    err = await open_coin(message.bot, message.chat.id, message.from_user, int(parts[1]))
    if err:
        await message.answer(f"⚠️ {esc(err)}")


async def _play(cb: CallbackQuery, p: dict, pick: str):
    """Вся партия идёт в одном сообщении (меню → «Подбрасываю…» → итог): ни стикера, ни новых сообщений,
    поэтому флуд-лимит группы (около 20 сообщений в минуту) при спаме не срабатывает."""
    uid, stake, bot, chat_id = p["uid"], p["stake"], cb.bot, cb.message.chat.id
    async with locks[uid]:
        outcome = "heads" if secrets.randbelow(2) == 0 else "tails"
        won = outcome == pick
        # Списание и выплата одной транзакцией с условием coins >= stake: баланс не уйдёт в минус.
        if not await ec.bet(uid, stake, stake * COEF if won else 0):
            await cb.answer("Не хватает рубинов на ставку", show_alert=True)
            await _drop(p["msg"])
            return

    async def _ack():                         # ставка уже списана: сбой ответа на кнопку не должен рвать игру
        try:
            await cb.answer("Монета в воздухе...")
        except TelegramAPIError:
            pass

    async def _announce():
        try:
            await p["msg"].edit_text(
                f"🪙 <b>{TITLE}</b>\nИгрок: <b>{esc(p['name'])}</b>\n"
                f"Ставка: <b>{stake}</b> {RUBY} на <b>{SIDE[pick].lower()}</b>\n\n"
                "<i>Подбрасываю монету...</i>",
                reply_markup=None,
            )
        except TelegramAPIError:
            pass

    await asyncio.gather(_ack(), _announce(), asyncio.sleep(TOSS_PAUSE))

    try:
        balance = (await ec.wallet(uid))[0]
    except Exception:
        logger.exception("Не удалось получить баланс игрока %s после монеты", uid)
        balance = 0
    who = f"<b>{esc(p['name'])}</b>"
    icon = SIDE_ICON[outcome]
    if won:
        text = (f"{e('party')} {icon} Выпал <b>{SIDE[outcome].lower()}</b>, {who} угадал(а)!\n"
                f"Ставка <b>{stake}</b> {RUBY} умножена на {COEF}: выигрыш <b>+{stake * (COEF - 1)}</b> {RUBY}\n"
                f"Баланс: <b>{balance}</b> {RUBY}")
    else:
        text = (f"{e('skull')} {icon} Выпал <b>{SIDE[outcome].lower()}</b>, {who} не угадал(а).\n"
                f"Ставка <b>{stake}</b> {RUBY} уходит боту\n"
                f"Баланс: <b>{balance}</b> {RUBY}")
    markup = again_markup(uid, stake) if balance >= stake else None
    try:
        await add_game_result(uid, KEY, "win" if won else "loss")
    except Exception:
        logger.exception("Не удалось записать результат монеты для игрока %s", uid)
    # Итог правкой того же сообщения; если не вышло, пробуем ещё раз и в крайнем случае шлём новым.
    for attempt in range(3):
        try:
            await p["msg"].edit_text(text, reply_markup=markup)
            return
        except TelegramAPIError as ex:
            if "not modified" in str(ex).lower():
                return
            logger.warning("Монета: итог не отправлен, попытка %s (%s: %s)", attempt + 1, type(ex).__name__, ex)
            await asyncio.sleep(1.0 * (attempt + 1))
    try:
        await bot.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        logger.error("Монета: итог игрока %s (%s) не удалось доставить, ставка уже проведена", uid,
                     "win" if won else "loss")


@router.callback_query(F.data.startswith("cf:"))
async def cf_cb(cb: CallbackQuery):
    _, token, act = cb.data.split(":")
    p = pending.get(token)
    if not p:
        await cb.answer("Это меню уже закрыто", show_alert=True)
        return
    if cb.from_user.id != p["uid"]:
        await cb.answer("Это ставка другого игрока", show_alert=True)
        return
    if act == "x":
        _forget(token)
        await _drop(p["msg"])
        await cb.answer("Ставка отменена")
        return
    if act not in SHORT:
        await cb.answer()
        return
    if pause.is_paused():
        await cb.answer("Сейчас игры на паузе", show_alert=True)
        return
    if await is_user_banned(cb.from_user.id):
        await cb.answer("Ты в чёрном списке игр", show_alert=True)
        return
    from services import features
    if not features.is_on(KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    if pending.get(token) is not p:           # пока шли проверки, меню уже обработали
        await cb.answer()
        return
    _forget(token)                            # забираем меню до игры: двойной клик не сыграет дважды
    await _play(cb, p, SHORT[act])


@router.callback_query(F.data.startswith("cfa:"))
async def cf_again(cb: CallbackQuery):
    _, uid, stake = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    from services import features
    if not features.is_on(KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await open_coin(cb.bot, cb.message.chat.id, cb.from_user, int(stake), msg=cb.message)
    if err:
        await cb.answer(err, show_alert=True)
        return
    await cb.answer("Выбери сторону")
