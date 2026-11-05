"""Слот-машина: игра в одиночку со ставкой в рубинах.

Запуск:
  • кнопка «Слот-машина» в /games: бот спрашивает ставку (кнопкой или своей суммой), см. games/menu.py;
  • команда /slot <ставка>, например /slot 50.
Ставка списывается сразу, бот крутит настоящий автомат Telegram (эмодзи 🎰, результат решает сам Telegram).
Три барабана, на каждом по одному из четырёх символов (BAR, виноград, лимон, семёрка):
  • все три разные: проигрыш, ставка остаётся у бота;
  • два одинаковых: ставка возвращается;
  • три одинаковых: ставка умножается, во сколько раз зависит от символа (таблица TRIPLE_COEF).

Ставка обязательна (минимум 1 рубин). Игра одиночная, поэтому не занимает «слот» активной игры в чате.
"""
import asyncio
from typing import Optional, Set, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

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
from utils.ui import kb

router = Router()

KEY = "slot"
TITLE = "Слоты"
SPIN_PAUSE = 3.2    # сколько идёт анимация автомата, сек (итог показываем после неё)
# Во сколько раз умножается ставка при трёх одинаковых символах (ключ: номер символа из SYMBOLS).
# Все тройки выпадают одинаково часто (1 из 64), поэтому множитель зависит только от «красоты» комбинации.
TRIPLE_COEF = {3: 10, 0: 6, 1: 5, 2: 4}  # 777 ×10, BAR ×6, виноград ×5, лимон ×4
SYMBOLS = ("BAR", "🍇", "🍋", "7️⃣")  # порядок символов в значении кубика Telegram (в итоговом сообщении не показываем)

spinning: Set[int] = set()  # игроки, у которых автомат сейчас крутится


def decode(value: int) -> Tuple[int, int, int]:
    """Значение кубика 🎰 (1..64) -> символы трёх барабанов. Левый барабан меняется быстрее всех."""
    v = value - 1
    return v % 4, (v // 4) % 4, v // 16


def result_of(reels: Tuple[int, int, int]) -> str:
    """win: три одинаковых, draw: два одинаковых, loss: все разные."""
    return {1: "win", 2: "draw", 3: "loss"}[len(set(reels))]


def coef_of(reels: Tuple[int, int, int]) -> int:
    """Множитель ставки: у тройки зависит от символа, у остальных исходов он не используется."""
    return TRIPLE_COEF[reels[0]] if len(set(reels)) == 1 else 0


def payout(result: str, stake: int, coef: int = 0) -> int:
    return {"win": stake * coef, "draw": stake, "loss": 0}[result]


def table_text() -> str:
    """Таблица выплат для подсказок: «7️⃣ ×10, BAR ×6, 🍇 ×5, 🍋 ×4»."""
    return ", ".join(f"{SYMBOLS[i]} ×{c}" for i, c in sorted(TRIPLE_COEF.items(), key=lambda kv: -kv[1]))


def again_markup(uid: int, stake: int):
    return kb([[(f"🔄 Ещё раз ({stake})", f"sla:{uid}:{stake}")]])


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


async def spin(bot, chat_id: int, user, stake: int) -> Optional[str]:
    """Списывает ставку и запускает автомат. Возвращает None или текст ошибки.

    Итог приходит позже, отдельным сообщением (после анимации), поэтому здесь не ждём.
    """
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if user.id in spinning:
        return "Автомат ещё крутится, дождись результата."
    balance = (await ec.wallet(user.id))[0]
    err = check_stake(stake, balance)
    if err:
        return err
    spinning.add(user.id)
    debited = settled = False
    try:
        # Ставка списывается сразу и только если рубины есть: повторное нажатие или другая игра не потратят её дважды.
        if not await ec.debit(user.id, stake):
            spinning.discard(user.id)
            return "У тебя не хватает рубинов на такую ставку."
        debited = True
        try:
            dice = await bot.send_dice(chat_id, emoji="🎰")
            value = dice.dice.value
        except (TelegramAPIError, AttributeError):
            await asyncio.shield(ec.credit(user.id, stake))
            settled = True                # ставка возвращена, повторно не возвращаем
            spinning.discard(user.id)
            return "Не удалось запустить автомат, ставка возвращена."
        reels = decode(value)
        result = result_of(reels)
        coef = coef_of(reels)
        won = payout(result, stake, coef)
        if won:
            await asyncio.shield(ec.credit(user.id, won))
        settled = True
    except BaseException:
        # Любая ошибка после списания, но до расчёта, возвращает ставку: игрок не теряет рубины молча.
        if debited and not settled:
            await asyncio.shield(ec.credit(user.id, stake))
        spinning.discard(user.id)
        raise
    asyncio.create_task(_finish(bot, chat_id, user, stake, dice, result, coef, reels))
    return None


async def _finish(bot, chat_id: int, user, stake: int, dice: Message, result: str, coef: int, reels):
    uid = user.id
    try:
        await pause.sleep(SPIN_PAUSE)
        balance = (await ec.wallet(uid))[0]
        who = f"<b>{esc(_name(user))}</b>"
        card = lambda *rows: "<blockquote>" + "\n".join(rows) + "</blockquote>"
        bal = f"Баланс: <b>{balance}</b> {RUBY}"
        # Сами барабаны не дублируем: их и так видно на анимации автомата.
        if result == "win":
            lead = "🎰 <b>ДЖЕКПОТ!</b> " if reels[0] == 3 else f"{e('party')} "
            text = (f"{lead}{who} срывает куш: <b>+{stake * (coef - 1)}</b> {RUBY}\n"
                    + card(f"Ставка <b>{stake}</b> ×{coef} = <b>{stake * coef}</b> {RUBY}", bal))
        elif result == "draw":
            text = (f"🤝 {who} остаётся при своих: ставка <b>{stake}</b> {RUBY} вернулась\n"
                    + card(bal))
        else:
            text = (f"{e('skull')} {who} не повезло: <b>−{stake}</b> {RUBY}\n"
                    + card(bal))
        markup = again_markup(uid, stake) if balance >= stake else None
        try:
            await dice.reply(text, reply_markup=markup)
        except TelegramAPIError:
            await bot.send_message(chat_id, text, reply_markup=markup)
        await add_game_result(uid, KEY, result)
    except Exception:
        logger.exception("Ошибка завершения слот-машины у игрока %s", uid)
    finally:
        spinning.discard(uid)


@router.message(Command("slot"))
async def cmd_slot(message: Message):
    parts = (message.text or "").split()
    usage = (f"🎰 <b>{TITLE}</b>\nСделай ставку командой: <code>/slot 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
             "Или нажми «Слот-машина» в /games, там ставка выбирается кнопками.\n\n"
             "<i>Три разных символа: проигрыш. Два одинаковых: ставка возвращается. "
             f"Три одинаковых: ставка умножается ({table_text()}).</i>")
    if len(parts) < 2:
        await message.answer(usage)
        return
    if not parts[1].isdigit():
        await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/slot 50</code>")
        return
    err = await spin(message.bot, message.chat.id, message.from_user, int(parts[1]))
    if err:
        await message.answer(f"⚠️ {esc(err)}")


@router.callback_query(F.data.startswith("sla:"))
async def slot_again(cb: CallbackQuery):
    _, uid, stake = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    from services import features
    if not features.is_on(KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await spin(cb.bot, cb.message.chat.id, cb.from_user, int(stake))
    if err:
        await cb.answer(err, show_alert=True)
        return
    try:                                      # у старого итога убираем кнопку, чтобы не крутить дважды
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await cb.answer("Крутим!")
