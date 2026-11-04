"""Открытый набор для игр на двоих.

/ttt [ставка] (и так же /rps, /duel, /checkers, /seabattle, /hangman) открывает сообщение с кнопками
«Присоединиться / Выйти / Старт / Отменить». Соперником становится первый, кто нажмёт «Присоединиться»,
играть начинает создатель по кнопке «Старт».

Ставка необязательна: победитель забирает её у проигравшего (сколько есть на балансе, как в рулетке).
"""
import asyncio
import logging
import secrets
from typing import Awaitable, Callable, Dict, Optional, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database import game_stats
from database.core import x
from database.moderation import is_user_banned
from games import pause
from games.core import can_dm
from games.invites import hold, release
from services import economy as ec
from utils.emoji import e
from utils.fmt import esc
from utils.ui import kb

router = Router()
log = logging.getLogger(__name__)

MAX_STAKE = 200
WAIT_SEC = 300
StartFn = Callable[[Message, int, str, int, str], Awaitable[None]]

challenges: Dict[str, dict] = {}
# Ставка текущей партии для каждого игрока: {user_id: {game, opp, stake, bot, chat_id}}
stakes: Dict[int, dict] = {}


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


def stake_text(stake: int) -> str:
    return f"<b>{stake}</b> 🪙" if stake else "без ставки"


def text(c: dict, note: str = "") -> str:
    roles = c["roles"]
    first = f"{roles[0]}: " if roles else ""
    second = f"{roles[1]}: " if roles else "Соперник: "
    joiner = f"<b>{esc(c['jname'])}</b>" if c["joiner"] else "<i>пока никого</i>"
    t = (f"{e(c['emoji'])} <b>{esc(c['title'])}</b>\n\n{first}<b>{esc(c['cname'])}</b>\n"
         f"{second}{joiner}\nСтавка: {stake_text(c['stake'])}\n\n")
    if note:
        return t + note
    if c["joiner"]:
        return t + "<i>Соперник найден. Создатель нажимает «Старт».</i>"
    return t + "<i>Нажми «Присоединиться», чтобы сыграть.</i>"


def markup(cid: str):
    return kb([[("➕ Присоединиться", f"oc:{cid}:j"), ("➖ Выйти", f"oc:{cid}:l")],
               [("▶️ Старт", f"oc:{cid}:s"), ("🚫 Отменить", f"oc:{cid}:x")]])


async def refresh(c: dict):
    try:
        await c["msg"].edit_text(text(c), reply_markup=markup(c["id"]))
    except TelegramAPIError:
        pass


def parse_stake(message: Message) -> Tuple[Optional[int], Optional[str]]:
    parts = (message.text or "").split()
    if len(parts) < 2:
        return 0, None
    arg = parts[1]
    if not arg.isdigit() or not 1 <= int(arg) <= MAX_STAKE:
        return None, f"❌ Ставка: число монет от 1 до {MAX_STAKE}. Пример: <code>{esc(parts[0])} 20</code>"
    return int(arg), None


async def open_challenge(message: Message, key: str, title: str, emoji: str, start: StartFn,
                         dm: str = "none", roles: Optional[Tuple[str, str]] = None):
    """dm: none | creator | both — кому из игроков нужна личка с ботом."""
    me = message.from_user
    if message.chat.id != GROUP_ID:
        await message.answer("Игры запускаются в группе.")
        return
    if pause.is_paused():
        await message.answer("⏸ Сейчас игры на паузе.")
        return
    if await is_user_banned(me.id):
        await message.answer("❌ Ты в чёрном списке игр")
        return
    stake, err = parse_stake(message)
    if err:
        await message.answer(err)
        return
    if stake and (await ec.wallet(me.id))[0] < stake:
        await message.answer("❌ У тебя не хватает монет на такую ставку.")
        return
    if dm != "none" and not await can_dm(message.bot, me.id):
        bot_me = await message.bot.me()
        await message.answer(f"❌ Не могу написать тебе в личку. Открой @{bot_me.username}, "
                             "нажми Start и повтори команду.")
        return
    if not hold(me.id, key):
        await message.answer("⚠️ Ты уже участвуешь в игре")
        return
    cid = secrets.token_hex(3)
    c = {"id": cid, "key": key, "title": title, "emoji": emoji, "start": start, "dm": dm, "roles": roles,
         "creator": me.id, "cname": _name(me), "joiner": None, "jname": "", "stake": stake, "msg": None}
    challenges[cid] = c
    c["msg"] = await message.answer(text(c), reply_markup=markup(cid))
    asyncio.create_task(_expire(cid))


async def _expire(cid: str):
    await asyncio.sleep(WAIT_SEC)
    c = challenges.pop(cid, None)
    if not c:
        return
    release(c["creator"], c["joiner"] or 0)
    try:
        await c["msg"].edit_text(text(c, "<i>Время ожидания вышло, набор закрыт.</i>"))
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("oc:"))
async def challenge_cb(cb: CallbackQuery):
    _, cid, act = cb.data.split(":")
    c = challenges.get(cid)
    if not c:
        await cb.answer("Этот набор уже закрыт", show_alert=True)
        return
    user = cb.from_user
    uid = user.id

    if act == "j":
        if pause.is_paused():
            await cb.answer("Сейчас игры на паузе", show_alert=True)
            return
        if uid == c["creator"]:
            await cb.answer("Ты создатель, ждём соперника", show_alert=True)
            return
        if c["joiner"]:
            await cb.answer("Место уже занято", show_alert=True)
            return
        if await is_user_banned(uid):
            await cb.answer("Ты в чёрном списке игр", show_alert=True)
            return
        if c["stake"] and (await ec.wallet(uid))[0] < c["stake"]:
            await cb.answer("Не хватает монет на ставку", show_alert=True)
            return
        if c["dm"] == "both" and not await can_dm(cb.bot, uid):
            bot_me = await cb.bot.me()
            await cb.answer("Сначала нажми Start у бота", url=f"https://t.me/{bot_me.username}?start=game")
            return
        if not hold(uid, c["key"]):
            await cb.answer("Ты уже участвуешь в игре", show_alert=True)
            return
        c["joiner"], c["jname"] = uid, _name(user)
        await cb.answer("Ты в игре!")
        await refresh(c)
    elif act == "l":
        if uid != c["joiner"]:
            await cb.answer("Тебя нет в этой игре")
            return
        release(uid)
        c["joiner"], c["jname"] = None, ""
        await cb.answer("Ты вышел")
        await refresh(c)
    elif act == "x":
        if uid not in (c["creator"], ADMIN_ID):
            await cb.answer("Отменить может создатель или админ", show_alert=True)
            return
        challenges.pop(cid, None)
        release(c["creator"], c["joiner"] or 0)
        await cb.message.edit_text(text(c, "<i>Набор отменён.</i>"))
        await cb.answer()
    elif act == "s":
        if uid not in (c["creator"], ADMIN_ID):
            await cb.answer("Начать может создатель или админ", show_alert=True)
            return
        if not c["joiner"]:
            await cb.answer("Пока нет соперника", show_alert=True)
            return
        if pause.is_paused():
            await cb.answer("Сейчас игры на паузе", show_alert=True)
            return
        a, b = c["creator"], c["joiner"]
        if c["stake"]:
            poor = [n for u, n in ((a, c["cname"]), (b, c["jname"])) if (await ec.wallet(u))[0] < c["stake"]]
            if poor:
                await cb.answer("Не хватает монет на ставку: " + ", ".join(poor), show_alert=True)
                return
        challenges.pop(cid, None)
        stakes[a] = {"game": c["key"], "opp": b, "stake": c["stake"], "bot": cb.bot, "chat_id": cb.message.chat.id}
        stakes[b] = {"game": c["key"], "opp": a, "stake": c["stake"], "bot": cb.bot, "chat_id": cb.message.chat.id}
        try:
            await cb.message.edit_text(text(c, "<b>Игра началась!</b>"))
        except TelegramAPIError:
            pass
        await cb.answer()
        try:
            await c["start"](cb.message, a, c["cname"], b, c["jname"])
        except Exception:
            log.exception("Ошибка запуска игры %s", c["key"])
            release(a, b)
            await cb.message.answer("⚠️ Не удалось запустить игру.")


async def stake_hook(user_id: int, game_type: str, result: str):
    """Победитель забирает ставку у проигравшего (сколько есть на балансе)."""
    entry = stakes.get(user_id)
    if not entry or entry["game"] != game_type:
        return
    stakes.pop(user_id, None)
    if result != "win" or not entry["stake"]:
        return
    loser = entry["opp"]
    pay = min(entry["stake"], (await ec.wallet(loser))[0])
    if pay:
        await ec.ensure(user_id)
        await x("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (pay, loser))
        await x("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (pay, user_id))
    try:
        await entry["bot"].send_message(
            entry["chat_id"], f"💰 Ставка: победитель получает <b>{pay}</b> 🪙")
    except Exception:
        pass


if stake_hook not in game_stats.RESULT_HOOKS:
    game_stats.RESULT_HOOKS.append(stake_hook)
