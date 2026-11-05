"""Открытый набор для игр на двоих.

Запуск: кнопка в /games (бот спросит ставку) или команда /ttt [ставка] (так же /rps, /duel, /seabattle, /hangman).
Бот отправляет сообщение с кнопками «Присоединиться / Отменить». Соперником становится первый, кто нажмёт
«Присоединиться», и игра начинается сразу, кнопки «Старт» нет.

Ставка необязательна: победитель забирает её у проигравшего (сколько есть на балансе, как в рулетке).

Каждая игра один раз регистрируется через register_duel(...) в своём модуле: по этой записи её умеют
запускать и команда, и кнопка меню.
"""
import asyncio
import secrets
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional, Tuple

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database import game_stats
from database.moderation import is_user_banned
from games import pause
from games.core import can_dm
from games import invites
from games.invites import hold, release
from services import economy as ec
from utils.emoji import e
from utils.fmt import esc
from utils.logs import logger
from utils.ui import kb

router = Router()

MAX_STAKE = 1000  # потолок любой ставки: и быстрых кнопок, и своей суммы, и команд (/ttt 500)
STAKE_STEPS = (10, 25, 50, 100, 200, 500)  # быстрые ставки в меню игр
WAIT_SEC = 300
NEED_DM = "need_dm"  # create_challenge вернёт это, если создателю надо сначала нажать Start у бота
StartFn = Callable[[Message, int, str, int, str], Awaitable[None]]


@dataclass
class DuelDef:
    key: str
    title: str
    emoji: str
    start: StartFn
    dm: str = "none"  # none | creator | both: кому из игроков нужна личка с ботом
    roles: Optional[Tuple[str, str]] = None


DUELS: Dict[str, DuelDef] = {}


def register_duel(key: str, title: str, emoji: str, start: StartFn, dm: str = "none",
                  roles: Optional[Tuple[str, str]] = None):
    DUELS[key] = DuelDef(key, title, emoji, start, dm, roles)


challenges: Dict[str, dict] = {}
# Ставка текущей партии для каждого игрока: {user_id: {game, opp, stake, bot, chat_id}}
stakes: Dict[int, dict] = {}


def _name(user) -> str:
    return user.first_name or user.username or "Игрок"


def stake_text(stake: int) -> str:
    return f"<b>{stake}</b> ♦️" if stake else "без ставки"


def text(c: dict, note: str = "") -> str:
    roles = c["roles"]
    first = f"{roles[0]}: " if roles else ""
    second = f"{roles[1]}: " if roles else "Соперник: "
    joiner = f"<b>{esc(c['jname'])}</b>" if c["joiner"] else "<i>пока никого</i>"
    t = (f"{e(c['emoji'])} <b>{esc(c['title'])}</b>\n\n{first}<b>{esc(c['cname'])}</b>\n"
         f"{second}{joiner}\nСтавка: {stake_text(c['stake'])}\n\n")
    if note:
        return t + note
    return t + "<i>Нажми «Присоединиться»: игра начнётся сразу, как только найдётся соперник.</i>"


def markup(cid: str):
    return kb([[("➕ Присоединиться", f"oc:{cid}:j"), ("🚫 Отменить", f"oc:{cid}:x")]])


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
        return None, f"❌ Ставка: число рубинов от 1 до {MAX_STAKE}. Пример: <code>{esc(parts[0])} 20</code>"
    return int(arg), None


async def create_challenge(bot, chat_id: int, user, key: str, stake: int = 0) -> Optional[str]:
    """Открывает набор. Возвращает None, если всё хорошо, иначе текст ошибки (или NEED_DM)."""
    d = DUELS.get(key)
    if not d:
        return "Игра не найдена."
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if stake and (await ec.wallet(user.id))[0] < stake:
        return "У тебя не хватает рубинов на такую ставку."
    if d.dm != "none" and not await can_dm(bot, user.id):
        return NEED_DM
    if not hold(user.id, key):
        return "Ты уже участвуешь в игре."
    cid = secrets.token_hex(3)
    c = {"id": cid, "key": key, "title": d.title, "emoji": d.emoji, "start": d.start, "dm": d.dm, "roles": d.roles,
         "creator": user.id, "cname": _name(user), "joiner": None, "jname": "", "stake": stake, "msg": None}
    try:
        c["msg"] = await bot.send_message(chat_id, text(c), reply_markup=markup(cid))
    except Exception:
        release(user.id)
        raise
    challenges[cid] = c
    asyncio.create_task(_expire(cid))
    return None


async def open_challenge(message: Message, key: str):
    """Команда /ttt [ставка] и ей подобные."""
    stake, err = parse_stake(message)
    if err:
        await message.answer(err)
        return
    err = await create_challenge(message.bot, message.chat.id, message.from_user, key, stake)
    if err == NEED_DM:
        bot_me = await message.bot.me()
        await message.answer(f"❌ Не могу написать тебе в личку. Открой @{bot_me.username}, "
                             "нажми Start и повтори команду.")
    elif err:
        await message.answer(f"⚠️ {esc(err)}")


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


async def _launch(msg: Message, c: dict):
    """Соперник найден: проверяем ставку и сразу запускаем партию."""
    cid, a, b = c["id"], c["creator"], c["joiner"]
    ref = f"duel:{cid}"
    if c["stake"]:
        # Ставки обоих списываются сразу и атомарно (все или никто)
        if not await ec.escrow_hold(ref, {a: c["stake"], b: c["stake"]}, strict=True):
            uid = a if (await ec.wallet(a))[0] < c["stake"] else b
            if uid == b:
                poor_name = c["jname"]
                release(b)
                c["joiner"], c["jname"] = None, ""
                await refresh(c)
                await msg.answer(f"❌ У <b>{esc(poor_name)}</b> не хватает рубинов на ставку, "
                                 "ждём другого соперника.")
            elif challenges.pop(cid, None) is c:
                release(a, b)
                try:
                    await msg.edit_text(text(c, "<i>Набор отменён: у создателя не хватает рубинов на ставку.</i>"))
                except TelegramAPIError:
                    pass
            return
    if challenges.pop(cid, None) is not c:
        await ec.escrow_refund(ref)
        return
    stakes[a] = {"game": c["key"], "opp": b, "stake": c["stake"], "ref": ref, "bot": msg.bot, "chat_id": msg.chat.id}
    stakes[b] = {"game": c["key"], "opp": a, "stake": c["stake"], "ref": ref, "bot": msg.bot, "chat_id": msg.chat.id}
    try:
        await msg.edit_text(text(c, "<b>Игра началась!</b>"))
    except TelegramAPIError:
        pass
    try:
        await c["start"](msg, a, c["cname"], b, c["jname"])
    except Exception:
        logger.exception("Не удалось запустить дуэль %s", c.get("key"))
        release(a, b)
        stakes.pop(a, None)
        stakes.pop(b, None)
        await ec.escrow_refund(ref)
        await msg.answer("⚠️ Не удалось запустить игру.")


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
            await cb.answer("Не хватает рубинов на ставку", show_alert=True)
            return
        if c["dm"] == "both" and not await can_dm(cb.bot, uid):
            from utils.startlink import prompt_start
            await prompt_start(cb, "game")
            return
        # пока шли проверки, место могли занять, а набор отменить
        if challenges.get(cid) is not c or c["joiner"]:
            await cb.answer("Место уже занято", show_alert=True)
            return
        if not hold(uid, c["key"]):
            await cb.answer("Ты уже участвуешь в игре", show_alert=True)
            return
        c["joiner"], c["jname"] = uid, _name(user)
        await cb.answer("Ты в игре!")
        await _launch(cb.message, c)
    elif act == "x":
        if uid not in (c["creator"], ADMIN_ID):
            await cb.answer("Отменить может создатель или админ", show_alert=True)
            return
        challenges.pop(cid, None)
        release(c["creator"], c["joiner"] or 0)
        await cb.message.edit_text(text(c, "<i>Набор отменён.</i>"))
        await cb.answer()
    else:
        await cb.answer()


GRACE_SEC = 30
_bg: set = set()


async def _late_refund(ref: str):
    """Страховка: игра закончилась (игроки освобождены), а результата так и не было (таймаут, сбой, отмена)."""
    await asyncio.sleep(GRACE_SEC)
    for u in [u for u, e in stakes.items() if e.get("ref") == ref]:
        stakes.pop(u, None)
    await ec.escrow_refund(ref)


def _on_release(ids):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:      # синхронный вызов вне event loop (например, из теста)
        return
    for ref in {stakes[u]["ref"] for u in ids if u in stakes and stakes[u].get("ref")}:
        t = loop.create_task(_late_refund(ref))
        _bg.add(t)
        t.add_done_callback(_bg.discard)


if _on_release not in invites.RELEASE_HOOKS:
    invites.RELEASE_HOOKS.append(_on_release)


async def stake_hook(user_id: int, game_type: str, result: str):
    """Победитель забирает эскроу (обе ставки), при ничьей ставки возвращаются. Идемпотентно."""
    entry = stakes.get(user_id)
    if not entry or entry["game"] != game_type:
        return
    stakes.pop(user_id, None)
    ref = entry.get("ref")
    if not ref or not entry["stake"]:
        return
    if result == "draw":
        await ec.escrow_refund(ref)
        return
    if result != "win":
        return
    held = await ec.escrow_get(ref)
    total = sum(held.values())
    if not total or await ec.escrow_payout(ref, {user_id: total}) is None:
        return
    pay = total - held.get(user_id, 0)
    try:
        await entry["bot"].send_message(
            entry["chat_id"], f"💰 Ставка: победитель получает <b>{pay}</b> ♦️")
    except Exception:
        logger.exception("Не удалось сообщить о выплате ставки в чат %s", entry["chat_id"])


if stake_hook not in game_stats.RESULT_HOOKS:
    game_stats.RESULT_HOOKS.append(stake_hook)
