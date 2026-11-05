"""Блэкджек (очко) в чате.

/blackjack [ставка] или кнопка в /games открывает лобби (2–8 игроков, у каждого должна быть личка с ботом).
Каждый получает две карты в личку и сам решает: «Ещё» (взять карту), «Хватит» (остановиться) или «Выйти».
Играют все одновременно. Побеждает тот, у кого сумма ближе всех к 21. Больше 21 — автоматическое поражение,
выход из игры — тоже поражение. При равенстве выигрыш делится.

В личке у каждого игрока ОДНО сообщение: его карты, сумма, статусы соперников и кнопки. Оно только редактируется.
В общем чате виден один «стол»: кто думает, кто остановился, кто перебрал. Карты открываются в конце игры.

Ставка необязательна: проигравшие отдают её победителям (сколько есть на балансе), как в рулетке.
"""
import asyncio
import time
from typing import Dict, List, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from games import pause
from games.core import dm, finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from games.duel.challenge import MAX_STAKE
from games.party.blackjack.engine import BUST, Game, LEFT, PLAY, Player, STAND, TARGET, card_str
from services import economy as ec
from utils import ui
from utils.emoji import RUBY, e
from utils.fmt import esc
from utils.logs import logger

router = Router()

KEY = "blackjack"
MAX_PLAYERS = 8
IDLE_SEC = 45     # сколько игрок может молчать: потом за него автоматически нажимается «Хватит»
TICK = 2.0        # как часто игра проверяет таймеры и обновляет сообщения (так Telegram не ограничит правки)
LOG_LINES = 4     # сколько последних событий показывать под столом в чате


# ---------- оформление ----------

def cards_html(cards: List[int]) -> str:
    # <code>, чтобы масть ♦ не превращалась в кастомный эмодзи рубина
    return " ".join(f"<code>{card_str(c)}</code>" for c in cards) or "—"


def cards_word(n: int) -> str:
    return "карты" if n < 5 else "карт"


def stake_line(d: dict) -> str:
    return f"Ставка: <b>{d['stake']}</b> {RUBY}" if d.get("stake") else "Без ставки"


def empty_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[])


def public_status(p: Player) -> str:
    """Что видят остальные: карты скрыты, видно только решение игрока."""
    if p.state == PLAY:
        return f"думает ({len(p.cards)} {cards_word(len(p.cards))})"
    if p.state == STAND:
        return f"хватит ({len(p.cards)} {cards_word(len(p.cards))})"
    if p.state == BUST:
        return "перебор 💥"
    return "вышел 🚪"


MARKS = {PLAY: "⏳", STAND: "✋", BUST: "💥", LEFT: "🚪"}


def add_log(d: dict, text: str):
    d["log"].append(text)
    del d["log"][:-LOG_LINES]


def table_text(lobby: Lobby) -> str:
    d, g = lobby.data, lobby.data["game"]
    lines = [f"{e('blackjack')} <b>Блэкджек</b>", stake_line(d),
             f"<i>Ближе к {TARGET} — победа, больше {TARGET} — проигрыш.</i>", ""]
    for p in g.players:
        lines.append(f"{MARKS[p.state]} <b>{esc(p.name)}</b> — {public_status(p)}")
    if d["log"]:
        lines += [""] + d["log"]
    lines += ["", f"<i>Карты и кнопки — в личке у каждого игрока. На решение {IDLE_SEC} с.</i>"]
    return "\n".join(lines)


def dm_text(lobby: Lobby, p: Player) -> str:
    """Единственное личное сообщение игрока: карты, сумма, соперники, кнопки."""
    d, g = lobby.data, lobby.data["game"]
    lines = [f"{e('blackjack')} <b>Блэкджек</b>", stake_line(d), "",
             f"Твои карты: {cards_html(p.cards)}"]
    lines.append(f"Сумма: <b>{p.score}</b>" + (" — перебор" if p.state == BUST else ""))
    if d["finished"]:
        lines += ["", d["final_text"] or "<i>Игра остановлена, рубины не списаны.</i>"]
        return "\n".join(lines)
    lines.append("")
    if p.state == PLAY:
        if d["confirm"].get(p.uid):
            lines.append(f"⚠️ <b>Выйти из игры?</b> Это поражение{', ставка уйдёт' if d.get('stake') else ''}.")
        else:
            lines.append(f"👉 <b>Твой ход:</b> «Ещё» или «Хватит». "
                         f"Если не ответишь за {IDLE_SEC} с, нажмётся «Хватит».")
    elif p.state == STAND:
        lines.append("✋ Ты остановился. Ждём остальных.")
    elif p.state == BUST:
        lines.append(f"💥 <b>Перебор!</b> Больше {TARGET}: это поражение.")
    else:
        lines.append("🚪 Ты вышел из игры: это поражение.")
    note = d["notes"].get(p.uid)
    if note:
        lines.append(note)
    lines += ["", "<b>Соперники:</b>"]
    for o in g.players:
        if o is not p:
            lines.append(f"{MARKS[o.state]} {esc(o.name)} — {public_status(o)}")
    return "\n".join(lines)


def dm_markup(lobby: Lobby, p: Player) -> InlineKeyboardMarkup:
    d, gid = lobby.data, lobby.gid
    if d["finished"] or p.state != PLAY:
        return empty_markup()
    pre = f"bj:{gid}:"
    if d["confirm"].get(p.uid):
        return InlineKeyboardMarkup(inline_keyboard=[[ui.btn("🚪 Да, выйти", pre + "ly"),
                                                      ui.btn("◀️ Назад", pre + "ln")]])
    return InlineKeyboardMarkup(inline_keyboard=[
        [ui.btn("🃏 Ещё", pre + "h"), ui.btn("✋ Хватит", pre + "s")],
        [ui.btn("🚪 Выйти", pre + "l")],
    ])


def result_text(lobby: Lobby, winners: List[Player], delta: Dict[int, int]) -> str:
    d, g = lobby.data, lobby.data["game"]
    win = {p.uid for p in winners}

    def order(p: Player):
        return (0 if p.in_race else 1 if p.state == BUST else 2, -p.score)

    lines = [f"{e('party')} <b>Блэкджек: итоги</b>", stake_line(d), ""]
    for p in sorted(g.players, key=order):
        if p.state == LEFT:
            row = f"🚪 <b>{esc(p.name)}</b> — вышел из игры"
        elif p.state == BUST:
            row = f"💥 <b>{esc(p.name)}</b>: {cards_html(p.cards)} = {p.score} — перебор"
        else:
            row = f"{'🏆' if p.uid in win else '▫️'} <b>{esc(p.name)}</b>: {cards_html(p.cards)} = {p.score}"
        if delta.get(p.uid):
            row += f" ({'+' if delta[p.uid] > 0 else '−'}{abs(delta[p.uid])} {RUBY})"
        lines.append(row)
    lines.append("")
    if not winners:
        lines.append("Победителя нет: все перебрали или вышли.")
    elif len(winners) == 1:
        lines.append(f"{e('crown')} Победитель: <b>{esc(winners[0].name)}</b> ({winners[0].score})")
    else:
        lines.append(f"{e('crown')} Ничья, победители: " + ", ".join(f"<b>{esc(p.name)}</b>" for p in winners)
                     + f" ({winners[0].score})")
    if delta:
        lines.append("<i>Рубины переведены.</i>")
    return "\n".join(lines)


# ---------- сообщения ----------

async def _delete(bot, chat_id: int, message_id: Optional[int]):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id, message_id)
    except TelegramAPIError:
        pass


async def edit_table(lobby: Lobby):
    d = lobby.data
    if not d.get("msg_id"):
        return
    try:
        await lobby.bot.edit_message_text(table_text(lobby), chat_id=lobby.chat_id, message_id=d["msg_id"])
    except TelegramAPIError:
        pass    # «не изменено» или лимит Telegram: следующее обновление всё исправит


async def push_dm(lobby: Lobby, p: Player) -> bool:
    """Обновляет личное сообщение игрока: редактирует его, а если его нет — отправляет новое."""
    d = lobby.data
    async with d["dm_lock"].setdefault(p.uid, asyncio.Lock()):
        text, markup = dm_text(lobby, p), dm_markup(lobby, p)
        fingerprint = (text, markup.model_dump_json())
        mid = d["dm"].get(p.uid)
        if mid and d["dm_last"].get(p.uid) == fingerprint:
            return True
        if mid:
            for _ in range(2):
                try:
                    await lobby.bot.edit_message_text(text, chat_id=p.uid, message_id=mid, reply_markup=markup)
                    d["dm_last"][p.uid] = fingerprint
                    return True
                except TelegramRetryAfter as ex:
                    await asyncio.sleep(min(ex.retry_after, 5))
                except TelegramBadRequest as ex:
                    if "not modified" in str(ex).lower():
                        d["dm_last"][p.uid] = fingerprint
                        return True
                    break
                except TelegramAPIError:
                    return False
            else:
                return False
        msg = await dm(lobby.bot, p.uid, text, markup)
        if msg:
            d["dm"][p.uid] = msg.message_id
            d["dm_last"][p.uid] = fingerprint
            return True
        if p.uid not in d["dm_warned"]:
            d["dm_warned"].add(p.uid)
            try:
                await lobby.bot.send_message(
                    lobby.chat_id, f"⚠️ Не смог написать в личку: <b>{esc(p.name)}</b>\n"
                                   "<i>Без лички с ботом играть нельзя: нажми Start у бота.</i>")
            except TelegramAPIError:
                pass
        return False


async def refresh_dms(lobby: Lobby):
    results = await asyncio.gather(*(push_dm(lobby, p) for p in lobby.data["game"].players),
                                   return_exceptions=True)
    for r in results:
        if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
            logger.warning("Не удалось обновить личное сообщение блэкджека", exc_info=r)


# ---------- ход игры ----------

async def start_game(lobby: Lobby):
    stake = lobby.data.get("stake", 0)
    if stake:
        ref = lobby.data["escrow"] = f"lobby:{lobby.gid}"
        held = await ec.escrow_hold(ref, {u: stake for u in lobby.players})
        poor = [u for u in lobby.players if u not in held]
        for u in poor:
            lobby.players.pop(u, None)
        if poor and len(lobby.players) < 2:
            await lobby.bot.send_message(lobby.chat_id, "❌ Не у всех хватило рубинов на ставку, игра отменена.")
            finish(lobby)
            return
    lobby.data.update(game=Game(dict(lobby.players)), wake=asyncio.Event(), dirty=False, closing=False,
                      finished=False, final_text="", msg_id=None, log=[], notes={}, confirm={},
                      dm={}, dm_lock={}, dm_last={}, dm_warned=set())
    lobby.task = asyncio.create_task(run(lobby))


def poke(d: dict):
    """Что-то изменилось: игра обновит стол и личные сообщения на ближайшем шаге."""
    d["dirty"] = True
    d["wake"].set()


async def run(lobby: Lobby):
    d, g, bot = lobby.data, lobby.data["game"], lobby.bot
    try:
        msg = await bot.send_message(
            lobby.chat_id, f"{e('blackjack')} <b>Блэкджек начинается!</b>\nИгроков: {len(g.players)}\n"
                           "Карты и кнопки придут в личку.")
        d["msg_id"] = msg.message_id
        await edit_table(lobby)
        await refresh_dms(lobby)
        last = time.monotonic()
        while not g.over():
            try:
                await pause.wait_for(d["wake"], TICK)
            except asyncio.TimeoutError:
                pass
            d["wake"].clear()
            now = time.monotonic()
            dt, last = now - last, now
            if not pause.is_paused():
                for p in g.active():
                    p.idle += dt
                    if p.idle >= IDLE_SEC:
                        g.stand(p.uid)
                        d["notes"][p.uid] = "⌛ Время вышло: автоматически нажалось «Хватит»."
                        add_log(d, f"⌛ {esc(p.name)} не ответил(а) и остановился")
                        d["dirty"] = True
            if d["dirty"]:
                d["dirty"] = False
                await edit_table(lobby)
                await refresh_dms(lobby)
        d["closing"] = True
        await finish_game(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Ошибка в игре в блэкджек в чате %s", lobby.chat_id)
        try:
            await bot.send_message(lobby.chat_id, "⚠️ В блэкджеке случилась ошибка, игра остановлена. Рубины не списаны.")
        except Exception:
            logger.debug("Не удалось сообщить об ошибке блэкджека в чат %s", lobby.chat_id, exc_info=True)
    finally:
        d["closing"] = True
        if not d["finished"]:           # игра прервана: убираем кнопки из личек, сами сообщения остаются
            d["finished"] = True
            try:
                await refresh_dms(lobby)
            except Exception:
                logger.debug("Не удалось обновить личные сообщения блэкджека", exc_info=True)
        await _delete(bot, lobby.chat_id, d.get("msg_id"))
        finish(lobby)


async def settle(lobby: Lobby, winners: List[Player]) -> Dict[int, int]:
    """Эскроу: проигравшие теряют ставку, победители получают свою и делят собранное поровну.
    Ничья (победителей нет) возвращает все ставки."""
    ref = lobby.data.get("escrow")
    if not ref:
        return {}
    held = await ec.escrow_get(ref)
    won = [p.uid for p in winners if p.uid in held]
    if not won:
        await ec.escrow_refund(ref)
        return {}
    pot = sum(a for u, a in held.items() if u not in won)
    payouts, delta = {u: held[u] for u in won}, {}
    share, rem = divmod(pot, len(won))
    for i, u in enumerate(won):
        got = share + (1 if i < rem else 0)
        payouts[u] += got
        if got:
            delta[u] = got
    if await ec.escrow_payout(ref, payouts) is None:
        return {}
    delta.update({u: -a for u, a in held.items() if u not in won})
    return delta


async def finish_game(lobby: Lobby):
    d, g, bot = lobby.data, lobby.data["game"], lobby.bot
    winners = g.winners()
    delta = await settle(lobby, winners)
    text = result_text(lobby, winners, delta)
    d["final_text"] = text
    d["finished"] = True
    await bot.send_message(lobby.chat_id, text)
    await _delete(bot, lobby.chat_id, d.get("msg_id"))
    d["msg_id"] = None
    await refresh_dms(lobby)
    await record_results(KEY, [p.uid for p in g.players], [p.uid for p in winners])


# ---------- кнопки ----------

def find_lobby(gid: int) -> Optional[Lobby]:
    lobby = next((l for l in lobbies.values() if l.gid == gid), None)
    if lobby and lobby.game.key == KEY and lobby.started and "game" in lobby.data:
        return lobby
    return None


@router.callback_query(F.data.startswith("bj:"))
async def bj_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    gid, act = int(parts[1]), parts[2]
    lobby = find_lobby(gid)
    if not lobby or lobby.data["finished"]:
        await cb.answer("Игра завершена", show_alert=True)
        return
    d, g, uid = lobby.data, lobby.data["game"], cb.from_user.id
    p = g.get(uid)
    if not p:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if d["closing"]:
        await cb.answer("Игра уже подводит итоги")
        return
    if p.state != PLAY:
        await cb.answer("Ты уже сделал выбор: ждём остальных")
        return
    name = esc(p.name)
    if act == "h":
        d["confirm"].pop(uid, None)
        d["notes"].pop(uid, None)
        g.hit(uid)
        if p.state == BUST:
            add_log(d, f"💥 {name}: перебор!")
            await cb.answer(f"Перебор: {p.score}", show_alert=True)
        elif p.state == STAND:
            add_log(d, f"🎯 {name} набрал(а) {TARGET}")
            await cb.answer(f"{TARGET}! Лучше уже не бывает")
        else:
            add_log(d, f"🃏 {name} берёт карту")
            await cb.answer(f"Сумма: {p.score}")
    elif act == "s":
        d["confirm"].pop(uid, None)
        d["notes"].pop(uid, None)
        g.stand(uid)
        add_log(d, f"✋ {name}: хватит")
        await cb.answer(f"Остановился на {p.score}")
    elif act == "l":
        d["confirm"][uid] = True
        await cb.answer()
        await push_dm(lobby, p)
        return
    elif act == "ln":
        d["confirm"].pop(uid, None)
        await cb.answer()
        await push_dm(lobby, p)
        return
    elif act == "ly":
        d["confirm"].pop(uid, None)
        g.leave(uid)
        add_log(d, f"🚪 {name} вышел(а) из игры")
        await cb.answer("Ты вышел из игры")
    else:
        await cb.answer()
        return
    poke(d)
    await push_dm(lobby, p)


# ---------- запуск ----------

async def open_blackjack(bot, chat_id: int, user, stake: int = 0) -> Optional[str]:
    """Открывает лобби блэкджека со ставкой (0 = без ставки). Возвращает None или текст ошибки."""
    if stake and (await ec.wallet(user.id))[0] < stake:
        return "У тебя не хватает рубинов на такую ставку."
    err = await open_lobby(bot, chat_id, user, BLACKJACK)
    if err:
        return err
    lobby = lobbies.get(chat_id)
    if lobby and stake:
        lobby.data["stake"] = stake
        lobby.data["note"] = f"Ставка: {stake} ♦️. Проигравшие отдают её победителю (сколько есть на балансе)."
        await lobby.refresh()
    return None


@router.message(Command("blackjack"))
async def cmd_blackjack(message: Message):
    parts = (message.text or "").split()
    stake = 0
    if len(parts) > 1:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= MAX_STAKE:
            await message.answer(f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>/blackjack 20</code>")
            return
        stake = int(parts[1])
    err = await open_blackjack(message.bot, message.chat.id, message.from_user, stake)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


BLACKJACK = GameDef(
    key=KEY, title="Блэкджек", emoji="blackjack", command="blackjack",
    description=(f"Карты приходят в личку: «Ещё», «Хватит» или «Выйти». Кто ближе к {TARGET}, тот выиграл, "
                 f"больше {TARGET} — автоматический проигрыш."),
    min_players=2, max_players=MAX_PLAYERS, start=start_game, needs_dm=True,
    dm_note="Карты приходят в личку: один раз нажми Start у бота.")
register(BLACKJACK)
