"""Аркады: слот-машина, боулинг, дартс, кубик и баскетбол (одиночные игры со ставкой).

Запуск: кнопка в /games (бот спросит ставку) или команда /slot, /bowling, /darts, /dice, /basket с суммой, например /darts 50.
Ставка списывается сразу, бот бросает настоящую анимацию Telegram (🎰 🎳 🎯 🎲 🏀), результат решает сам Telegram.
Выплата зависит от результата броска (таблицы PAYOUTS), у каждой игры (кроме слота) есть свой небольшой бонус:

  • Боулинг 🎳 — «Серия страйков»: каждый страйк подряд поднимает множитель страйка на 0.5 (×3, ×3.5, … до ×5).
  • Дартс 🎯 — «Золотой дротик»: после яблочка бот кидает бонусный дротик, второе яблочко повышает выплату до ×4.
  • Кубик 🎲 — «Бонусный бросок»: после шестёрки бросок кубика добавляет к выплате «выпавшее число / 10» ставки.
  • Баскетбол 🏀 — «Хет-трик»: три попадания подряд дают бонус +1 ставку, потом серия начинается заново.
  • Слот-машина 🎰 — три разных символа проигрыш, два одинаковых возвращают ставку, три одинаковых умножают её (см. SLOT_TRIPLE_COEF).

Ставка обязательна (минимум 1 рубин). Игры одиночные: «слот» активной игры в чате не занимают.
"""
import asyncio
import math
from dataclasses import dataclass
from fractions import Fraction as Fr
from typing import Dict, Optional, Set, Tuple

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
from utils.premium import raw
from utils.ui import kb

router = Router()

THROW_PAUSE = 4.0    # сколько идёт анимация броска, сек (итог показываем после неё)
BOWLING_BASE = Fr(3)       # множитель одиночного страйка
BOWLING_STEP = Fr(1, 2)    # на сколько растёт множитель за каждый следующий страйк подряд
BOWLING_MAX = 5            # потолок множителя страйка в серии
DARTS_GOLD_BONUS = Fr(3, 2)  # добавка к выплате за второе яблочко («Золотой дротик»), итого ×4
DICE_BONUS_DIV = 10        # бонусный бросок кубика добавляет «выпавшее число / DICE_BONUS_DIV» ставки
HATTRICK = 3               # столько попаданий подряд нужно для бонуса в баскетболе
HATTRICK_BONUS = 1         # бонус хет-трика в ставках

ORDER = ("slot", "bowling", "darts", "dice", "basket")  # порядок аркад в меню и справке


def fmt(x: Fr) -> str:
    return str(x.numerator) if x.denominator == 1 else f"{float(x):g}"


@dataclass(frozen=True)
class Arcade:
    key: str
    title: str
    icon: str                      # эмодзи анимации Telegram
    command: str
    payouts: Dict[int, Fr]         # значение кубика -> во сколько раз умножается ставка
    labels: Dict[int, str]         # значение кубика -> подпись результата
    rules: str                     # короткие правила для меню и /help
    bonus_name: str
    intro: str                     # что сказать в справке по команде


GAMES: Dict[str, Arcade] = {}


def _add(g: Arcade):
    GAMES[g.key] = g


_add(Arcade(
    key="bowling", title="Боулинг", icon="🎳", command="bowling",
    payouts={1: Fr(0), 2: Fr(0), 3: Fr(0), 4: Fr(1), 5: Fr(3, 2), 6: Fr(3)},
    labels={1: "Шар в желобе", 2: "Сбита пара кеглей", 3: "Сбита половина кеглей",
            4: "Сбиты почти все кегли", 5: "Осталась всего одна кегля", 6: "СТРАЙК!"},
    rules=("страйк ×3, одна кегля осталась ×1.5, почти все кегли — ставка возвращается. "
           f"Бонус «Серия страйков»: каждый страйк подряд даёт +{fmt(BOWLING_STEP)} к множителю, до ×{BOWLING_MAX}"),
    bonus_name="Серия страйков",
    intro="Бросай шар: чем больше кеглей, тем больше выплата."))

_add(Arcade(
    key="darts", title="Дартс", icon="🎯", command="darts",
    payouts={1: Fr(0), 2: Fr(0), 3: Fr(1, 2), 4: Fr(1), 5: Fr(3, 2), 6: Fr(5, 2)},
    labels={1: "Мимо мишени", 2: "Внешнее кольцо", 3: "Среднее кольцо",
            4: "Близко к центру", 5: "Почти яблочко", 6: "ЯБЛОЧКО!"},
    rules=("яблочко ×2.5, почти яблочко ×1.5, близко к центру — ставка возвращается, среднее кольцо ×0.5. "
           "Бонус «Золотой дротик»: после яблочка бот кидает ещё один дротик, второе яблочко поднимает выплату до ×4"),
    bonus_name="Золотой дротик",
    intro="Кидай дротик в мишень: чем ближе к центру, тем больше выплата."))

_add(Arcade(
    key="dice", title="Кубик", icon="🎲", command="dice",
    payouts={1: Fr(0), 2: Fr(0), 3: Fr(1, 2), 4: Fr(1), 5: Fr(3, 2), 6: Fr(2)},
    labels={n: f"Выпало {n}" for n in range(1, 7)},
    rules=("шестёрка ×2, пятёрка ×1.5, четвёрка — ставка возвращается, тройка ×0.5, 1 и 2 — проигрыш. "
           f"Бонус «Бонусный бросок»: после шестёрки бот бросает кубик ещё раз и добавляет к выплате число/{DICE_BONUS_DIV} ставки"),
    bonus_name="Бонусный бросок",
    intro="Брось кубик: чем больше число, тем больше выплата."))

_add(Arcade(
    key="basket", title="Баскетбол", icon="🏀", command="basket",
    payouts={1: Fr(0), 2: Fr(0), 3: Fr(1, 2), 4: Fr(3, 2), 5: Fr(5, 2)},
    labels={1: "Мимо", 2: "Мимо, мяч отскочил", 3: "Мяч завис на кольце",
            4: "Попадание!", 5: "Чистое попадание!"},
    rules=("чистое попадание ×2.5, попадание ×1.5, мяч на кольце ×0.5, мимо — проигрыш. "
           f"Бонус «Хет-трик»: {HATTRICK} попадания подряд дают ещё +{HATTRICK_BONUS} ставки"),
    bonus_name="Хет-трик",
    intro="Бросай мяч в кольцо: попадёшь, и ставка вырастет."))

busy: Set[int] = set()                       # игроки, у которых бросок ещё идёт
streaks: Dict[Tuple[int, str], int] = {}     # (игрок, игра) -> текущая серия (страйки или попадания)


# ---------- правила ----------

def payout(stake: int, coef: Fr) -> int:
    return math.floor(stake * coef)


@dataclass
class Throw:
    coef: Fr                  # итоговый множитель за этот бросок (без бонусного броска)
    label: str
    note: str = ""            # строка про бонус для итогового сообщения
    bonus_throw: bool = False  # нужен ли ещё один бросок


def evaluate(key: str, uid: int, value: int) -> Throw:
    """Результат броска по таблице выплат плюс эффект бонуса игры (серия меняется здесь)."""
    cfg = GAMES[key]
    coef = cfg.payouts[value]
    t = Throw(coef, cfg.labels[value])
    sk = (uid, key)
    if key == "bowling":
        if value == 6:
            streaks[sk] = streaks.get(sk, 0) + 1
            n = streaks[sk]
            t.coef = min(BOWLING_BASE + BOWLING_STEP * (n - 1), Fr(BOWLING_MAX))
            if n > 1:
                t.note = f"🔥 <b>{cfg.bonus_name}: {n} подряд!</b> Множитель страйка ×{fmt(t.coef)}"
        else:
            streaks[sk] = 0
    elif key == "basket":
        if value >= 4:
            streaks[sk] = streaks.get(sk, 0) + 1
            n = streaks[sk]
            if n >= HATTRICK:
                streaks[sk] = 0
                t.coef += HATTRICK_BONUS
                t.note = f"🔥 <b>{cfg.bonus_name}!</b> {HATTRICK} попадания подряд: бонус +{HATTRICK_BONUS} ставки"
            else:
                t.note = f"Попаданий подряд: {n} (до хет-трика: {HATTRICK - n})"
        else:
            streaks[sk] = 0
    elif key in ("darts", "dice"):
        t.bonus_throw = value == 6
    return t


def bonus_result(key: str, value: int) -> Tuple[Fr, str]:
    """Что добавляет бонусный бросок: (добавка к множителю, подпись)."""
    cfg = GAMES[key]
    if key == "darts":
        if value == 6:
            total = GAMES["darts"].payouts[6] + DARTS_GOLD_BONUS
            return DARTS_GOLD_BONUS, f"🔥 <b>{cfg.bonus_name}: второе яблочко!</b> Выплата вырастает до ×{fmt(total)}"
        return Fr(0), f"{cfg.bonus_name} не попал в яблочко, но первое яблочко уже оплачено"
    return Fr(value, DICE_BONUS_DIV), f"🔥 <b>{cfg.bonus_name}: выпало {value}</b>, бонус +{value}/{DICE_BONUS_DIV} ставки"


def result_of(stake: int, total: int) -> str:
    return "win" if total > stake else "draw" if total == stake else "loss"


def icon_html(cfg: Arcade) -> str:
    """Эмодзи игры для текста. У кубика всегда стандартный 🎲: PremiumEmoji его не подменяет на кастомный."""
    return raw(cfg.icon) if cfg.key == "dice" else cfg.icon


def usage_text(cfg: Arcade) -> str:
    return (f"{icon_html(cfg)} <b>{esc(cfg.title)}</b>\n{cfg.intro}\n"
            f"Сделай ставку командой: <code>/{cfg.command} 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
            f"Или нажми «{esc(cfg.title)}» в /games, там ставка выбирается кнопками.\n\n"
            f"<i>{esc(cfg.rules)}.</i>")


def again_markup(key: str, uid: int, stake: int):
    return kb([[(f"🔄 Ещё раз ({stake})", f"arc:{key}:{uid}:{stake}")]])


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


# ---------- игра ----------

async def play(bot, chat_id: int, user, key: str, stake: int) -> Optional[str]:
    """Списывает ставку и бросает анимацию. Возвращает None или текст ошибки.

    Итог приходит позже, отдельным сообщением (после анимации), поэтому здесь не ждём.
    """
    cfg = GAMES[key]
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if user.id in busy:
        return "Бросок ещё идёт, дождись результата."
    balance = (await ec.wallet(user.id))[0]
    err = check_stake(stake, balance)
    if err:
        return err
    busy.add(user.id)
    debited = settled = False
    try:
        # Ставка списывается сразу и только если рубины есть: повторное нажатие не потратит её дважды.
        if not await ec.debit(user.id, stake):
            busy.discard(user.id)
            return "У тебя не хватает рубинов на такую ставку."
        debited = True
        try:
            dice = await bot.send_dice(chat_id, emoji=cfg.icon)
            value = dice.dice.value
        except (TelegramAPIError, AttributeError):
            await asyncio.shield(ec.credit(user.id, stake))
            settled = True                # ставка возвращена, повторно не возвращаем
            busy.discard(user.id)
            return "Не удалось сделать бросок, ставка возвращена."
        throw = evaluate(key, user.id, value)
        won = payout(stake, throw.coef)
        if won:
            await asyncio.shield(ec.credit(user.id, won))
        settled = True
    except BaseException:
        # Любая ошибка после списания, но до расчёта, возвращает ставку: игрок не теряет рубины молча.
        if debited and not settled:
            await asyncio.shield(ec.credit(user.id, stake))
        busy.discard(user.id)
        raise
    asyncio.create_task(_finish(bot, chat_id, user, cfg, stake, dice, throw, won))
    return None


async def _finish(bot, chat_id: int, user, cfg: Arcade, stake: int, dice: Message, throw: Throw, won: int):
    uid, total = user.id, won
    bonus_note = ""
    try:
        await pause.sleep(THROW_PAUSE)
        if throw.bonus_throw:
            await bot.send_message(chat_id, f"{icon_html(cfg)} <b>{esc(_name(user))}</b>: {esc(throw.label)} "
                                            f"Бонус «{cfg.bonus_name}»: бросаем ещё раз!")
            try:
                bdice = await bot.send_dice(chat_id, emoji=cfg.icon)
                await pause.sleep(THROW_PAUSE)
                extra, bonus_note = bonus_result(cfg.key, bdice.dice.value)
                add = payout(stake, extra)
                if add:
                    # Ключ операции: повторный вызов не выплатит бонус второй раз.
                    await ec.credit_once(f"arcade:{chat_id}:{dice.message_id}:bonus", uid, add)
                    total += add
            except (TelegramAPIError, AttributeError):
                bonus_note = "Бонусный бросок не удался, выплата за первый бросок сохранена."
        balance = (await ec.wallet(uid))[0]
        who = f"<b>{esc(_name(user))}</b>"
        card = lambda *rows: "<blockquote>" + "\n".join(r for r in rows if r) + "</blockquote>"
        result = result_of(stake, total)
        if result == "win":
            lead = f"{e('party')} {who} выигрывает: <b>+{total - stake}</b> {RUBY}"
        elif result == "draw":
            lead = f"🤝 {who} остаётся при своих: ставка <b>{stake}</b> {RUBY} вернулась"
        elif total:
            lead = f"{icon_html(cfg)} {who} возвращает часть ставки: <b>−{stake - total}</b> {RUBY}"
        else:
            lead = f"{e('skull')} {who} не повезло: <b>−{stake}</b> {RUBY}"
        line = (f"Ставка <b>{stake}</b> ×{fmt(throw.coef)} = <b>{won}</b> {RUBY}" if won
                else "")
        text = (f"{icon_html(cfg)} <b>{esc(throw.label)}</b>\n{lead}\n"
                + card(line, throw.note, bonus_note, f"Баланс: <b>{balance}</b> {RUBY}"))
        markup = again_markup(cfg.key, uid, stake) if balance >= stake else None
        try:
            await dice.reply(text, reply_markup=markup)
        except TelegramAPIError:
            await bot.send_message(chat_id, text, reply_markup=markup)
        await add_game_result(uid, cfg.key, result)
    except Exception:
        logger.exception("Ошибка завершения аркады %s у игрока %s", cfg.key, uid)
    finally:
        busy.discard(uid)


# ---------- команды и кнопки ----------

def _command(cfg: Arcade):
    async def handler(message: Message):
        parts = (message.text or "").split()
        if len(parts) < 2:
            await message.answer(usage_text(cfg))
            return
        if not parts[1].isdigit():
            await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. "
                                 f"Пример: <code>/{cfg.command} 50</code>")
            return
        err = await play(message.bot, message.chat.id, message.from_user, cfg.key, int(parts[1]))
        if err:
            await message.answer(f"⚠️ {esc(err)}")

    handler.__name__ = f"cmd_{cfg.key}"
    return handler


for _cfg in GAMES.values():
    router.message.register(_command(_cfg), Command(_cfg.command))


@router.callback_query(F.data.startswith("arc:"))
async def arcade_again(cb: CallbackQuery):
    _, key, uid, stake = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    from services import features
    if key not in GAMES or not features.is_on(key):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await play(cb.bot, cb.message.chat.id, cb.from_user, key, int(stake))
    if err:
        await cb.answer(err, show_alert=True)
        return
    try:                                      # у старого итога убираем кнопку, чтобы не бросать дважды
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await cb.answer("Бросаем!")


# ---------- слот-машина ----------
# Три барабана, на каждом по одному из четырёх символов (BAR, виноград, лимон, семёрка):
#   • все три разные: проигрыш, ставка остаётся у бота;
#   • два одинаковых: ставка возвращается;
#   • три одинаковых: ставка умножается, во сколько раз зависит от символа (SLOT_TRIPLE_COEF).
# Автомат делает настоящую анимацию Telegram (эмодзи 🎰), результат решает сам Telegram.

SLOT_KEY = "slot"
SLOT_TITLE = "Слоты"
SLOT_SPIN_PAUSE = 3.2    # сколько идёт анимация автомата, сек (итог показываем после неё)
# Во сколько раз умножается ставка при трёх одинаковых символах (ключ: номер символа из SLOT_SYMBOLS).
# Все тройки выпадают одинаково часто (1 из 64), поэтому множитель зависит только от «красоты» комбинации.
SLOT_TRIPLE_COEF = {3: 10, 0: 6, 1: 5, 2: 4}  # 777 ×10, BAR ×6, виноград ×5, лимон ×4
SLOT_SYMBOLS = ("BAR", "🍇", "🍋", "7️⃣")  # порядок символов в значении кубика Telegram (в итоговом сообщении не показываем)


def slot_decode(value: int) -> Tuple[int, int, int]:
    """Значение кубика 🎰 (1..64) -> символы трёх барабанов. Левый барабан меняется быстрее всех."""
    v = value - 1
    return v % 4, (v // 4) % 4, v // 16


def slot_result_of(reels: Tuple[int, int, int]) -> str:
    """win: три одинаковых, draw: два одинаковых, loss: все разные."""
    return {1: "win", 2: "draw", 3: "loss"}[len(set(reels))]


def slot_coef_of(reels: Tuple[int, int, int]) -> int:
    """Множитель ставки: у тройки зависит от символа, у остальных исходов он не используется."""
    return SLOT_TRIPLE_COEF[reels[0]] if len(set(reels)) == 1 else 0


def slot_payout(result: str, stake: int, coef: int = 0) -> int:
    return {"win": stake * coef, "draw": stake, "loss": 0}[result]


def slot_table_text() -> str:
    """Таблица выплат для подсказок: «7️⃣ ×10, BAR ×6, 🍇 ×5, 🍋 ×4»."""
    return ", ".join(f"{SLOT_SYMBOLS[i]} ×{c}" for i, c in sorted(SLOT_TRIPLE_COEF.items(), key=lambda kv: -kv[1]))


def slot_again_markup(uid: int, stake: int):
    return kb([[(f"🔄 Ещё раз ({stake})", f"sla:{uid}:{stake}")]])


async def spin_slot(bot, chat_id: int, user, stake: int) -> Optional[str]:
    """Списывает ставку и запускает автомат. Возвращает None или текст ошибки.

    Итог приходит позже, отдельным сообщением (после анимации), поэтому здесь не ждём.
    """
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if user.id in busy:
        return "Автомат ещё крутится, дождись результата."
    balance = (await ec.wallet(user.id))[0]
    err = check_stake(stake, balance)
    if err:
        return err
    busy.add(user.id)
    debited = settled = False
    try:
        # Ставка списывается сразу и только если рубины есть: повторное нажатие или другая игра не потратят её дважды.
        if not await ec.debit(user.id, stake):
            busy.discard(user.id)
            return "У тебя не хватает рубинов на такую ставку."
        debited = True
        try:
            dice = await bot.send_dice(chat_id, emoji="🎰")
            value = dice.dice.value
        except (TelegramAPIError, AttributeError):
            await asyncio.shield(ec.credit(user.id, stake))
            settled = True                # ставка возвращена, повторно не возвращаем
            busy.discard(user.id)
            return "Не удалось запустить автомат, ставка возвращена."
        reels = slot_decode(value)
        result = slot_result_of(reels)
        coef = slot_coef_of(reels)
        won = slot_payout(result, stake, coef)
        if won:
            await asyncio.shield(ec.credit(user.id, won))
        settled = True
    except BaseException:
        # Любая ошибка после списания, но до расчёта, возвращает ставку: игрок не теряет рубины молча.
        if debited and not settled:
            await asyncio.shield(ec.credit(user.id, stake))
        busy.discard(user.id)
        raise
    asyncio.create_task(_slot_finish(bot, chat_id, user, stake, dice, result, coef, reels))
    return None


async def _slot_finish(bot, chat_id: int, user, stake: int, dice: Message, result: str, coef: int, reels):
    uid = user.id
    try:
        await pause.sleep(SLOT_SPIN_PAUSE)
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
        markup = slot_again_markup(uid, stake) if balance >= stake else None
        try:
            await dice.reply(text, reply_markup=markup)
        except TelegramAPIError:
            await bot.send_message(chat_id, text, reply_markup=markup)
        await add_game_result(uid, SLOT_KEY, result)
    except Exception:
        logger.exception("Ошибка завершения слотов у игрока %s", uid)
    finally:
        busy.discard(uid)


@router.message(Command("slot"))
async def cmd_slot(message: Message):
    parts = (message.text or "").split()
    usage = (f"🎰 <b>{SLOT_TITLE}</b>\nСделай ставку командой: <code>/slot 50</code> (от 1 до {MAX_STAKE} {RUBY}).\n"
             "Или нажми «Слоты» в /games, там ставка выбирается кнопками.\n\n"
             "<i>Три разных символа: проигрыш. Два одинаковых: ставка возвращается. "
             f"Три одинаковых: ставка умножается ({slot_table_text()}).</i>")
    if len(parts) < 2:
        await message.answer(usage)
        return
    if not parts[1].isdigit():
        await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/slot 50</code>")
        return
    err = await spin_slot(message.bot, message.chat.id, message.from_user, int(parts[1]))
    if err:
        await message.answer(f"⚠️ {esc(err)}")


@router.callback_query(F.data.startswith("sla:"))
async def slot_again(cb: CallbackQuery):
    _, uid, stake = cb.data.split(":")
    if cb.from_user.id != int(uid):
        await cb.answer("Это кнопка другого игрока", show_alert=True)
        return
    from services import features
    if not features.is_on(SLOT_KEY):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    err = await spin_slot(cb.bot, cb.message.chat.id, cb.from_user, int(stake))
    if err:
        await cb.answer(err, show_alert=True)
        return
    try:                                      # у старого итога убираем кнопку, чтобы не крутить дважды
        await cb.message.edit_reply_markup(reply_markup=None)
    except TelegramAPIError:
        pass
    await cb.answer("Крутим!")
