import asyncio
import json
import logging
from datetime import datetime
from typing import Dict, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_ID, GROUP_ID
from database.core import x
from database.game_stats import add_game_result
from database.moderation import is_user_banned
from database.settings import get_setting
from games import core as games_core, gamemute, pause
from games.core import can_dm
from games.party.mafia import texts
from games.party.mafia.engine import MafiaGame
from games.party.mafia.roles import DESC, EMOJI, NAME, TEAM
from utils.emoji import e
from utils.fmt import esc
from utils.timeutils import ts
from utils.ui import kb, name_rows

router = Router()
log = logging.getLogger(__name__)

sessions: Dict[int, "Session"] = {}
_gid = 0


class Session:
    def __init__(self, bot, game: MafiaGame):
        self.bot = bot
        self.game = game
        self.lobby_msg: Optional[Message] = None
        self.task: Optional[asyncio.Task] = None
        self.night_event = asyncio.Event()
        self.vote_event = asyncio.Event()
        self.kamikaze_event = asyncio.Event()
        self.vote_msg: Optional[Message] = None
        self.dm: Dict[int, Message] = {}
        self.dm_text: Dict[int, str] = {}
        self.night_msg: Optional[Message] = None
        self.muted = set()
        self.variant: Dict[int, str] = {}
        self.kamikaze_pick: Optional[int] = None
        self.stopped = False
        self.warned = False
        self.created = datetime.now()


async def cfg(key: str, default: str) -> str:
    return await get_setting(key, default)


async def disabled_roles():
    raw = await get_setting("mafia_disabled", "")
    return [r for r in raw.split(",") if r]


def uid_name(g: MafiaGame, uid: int) -> str:
    return esc(g.players[uid].name)


async def dm(bot, uid: int, text: str, kb=None) -> Optional[Message]:
    try:
        return await bot.send_message(uid, text, reply_markup=kb)
    except (TelegramForbiddenError, TelegramBadRequest, TelegramAPIError) as ex:
        log.warning("Не удалось написать игроку %s: %s", uid, ex)
        return None


def lobby_kb(gid: int):
    return kb([[("➕ Войти", f"mf:j:{gid}"), ("➖ Выйти", f"mf:l:{gid}")],
               [("▶️ Старт", f"mf:s:{gid}"), ("🚫 Отмена", f"mf:x:{gid}")]])


async def lobby_text(s: Session) -> str:
    g = s.game
    mn = await cfg("mafia_min_players", "5")
    names = "\n".join(f"• {esc(p.name)}" for p in g.players.values()) or "<i>пока никого</i>"
    return (f"{e('skull')} <b>Лобби мафии</b>\n\nИгроков: <b>{len(g.players)}</b> "
            f"(минимум {mn})\n\n{names}\n\n<i>Чтобы играть, нужно один раз нажать Start у бота в личке.</i>")


async def refresh_lobby(s: Session):
    try:
        await s.lobby_msg.edit_text(await lobby_text(s), reply_markup=lobby_kb(s.game.gid))
    except TelegramBadRequest:
        pass


def _drop(chat_id: int):
    s = sessions.pop(chat_id, None)
    games_core.release(chat_id)
    return s


async def _mafia_stop(bot, chat_id: int, reason: str):
    await stop_game(bot, chat_id, reason)


async def open_mafia_lobby(bot, chat_id: int, user) -> Optional[str]:
    global _gid
    if chat_id != GROUP_ID:
        return "Мафия запускается в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    info = games_core.active(chat_id)
    if info:
        return f"Сейчас уже идёт игра: {info['title']}."
    games_core.claim(chat_id, "mafia", "Мафия", _mafia_stop)
    _gid += 1
    g = MafiaGame(_gid, chat_id, user.id)
    s = Session(bot, g)
    sessions[chat_id] = s
    g.add_player(user.id, user.first_name or user.username or "Игрок")
    s.lobby_msg = await bot.send_message(chat_id, await lobby_text(s), reply_markup=lobby_kb(g.gid))
    asyncio.create_task(_lobby_expire(s))
    return None


@router.message(Command("mafia"))
async def cmd_mafia(message: Message):
    err = await open_mafia_lobby(message.bot, message.chat.id, message.from_user)
    if err:
        await message.answer(f"⚠️ {err}")


async def _lobby_expire(s: Session):
    await pause.sleep(900)
    if sessions.get(s.game.chat_id) is s and s.game.phase == "lobby":
        _drop(s.game.chat_id)
        try:
            await s.lobby_msg.edit_text("🎭 <b>Лобби мафии закрыто</b>\n<i>Время ожидания вышло.</i>")
        except Exception:
            pass


async def stop_game(bot, chat_id: int, reason: str):
    s = _drop(chat_id)
    if not s:
        return False
    s.stopped = True
    if s.task and s.task is not asyncio.current_task():
        s.task.cancel()
    try:
        await bot.send_message(chat_id, f"{e('skull')} <b>Мафия остановлена</b>\n<i>{esc(reason)}</i>")
    except Exception:
        pass
    return True


def role_dm(g: MafiaGame, uid: int) -> str:
    p = g.players[uid]
    t = f"{EMOJI[p.role]} <b>Твоя роль: {NAME[p.role]}</b>\n\n{esc(DESC[p.role])}"
    if TEAM[p.role] == "mafia":
        mates = [f"{esc(q.name)} ({NAME[q.role]})" for q in g.players.values()
                 if TEAM[q.role] == "mafia" and q.uid != uid]
        if mates:
            t += "\n\n<b>Твоя команда:</b>\n" + "\n".join(f"• {m}" for m in mates)
    if p.role == "sergeant":
        c = next((q for q in g.players.values() if q.role == "commissar"), None)
        t += f"\n\n<b>Комиссар:</b> {esc(c.name)}" if c else "\n\n<i>Комиссара в этой игре нет.</i>"
    return t


async def begin_game(s: Session, cb: CallbackQuery):
    g = s.game
    min_pl = int(await cfg("mafia_min_players", "5"))
    bad = []
    for uid, p in g.players.items():
        if not await can_dm(s.bot, uid):
            bad.append(p.name)
    if bad:
        await cb.message.answer(
            "❌ Не могу написать в личку: " + ", ".join(f"<b>{esc(n)}</b>" for n in bad) +
            "\n<i>Откройте бота и нажмите Start, потом снова нажмите «Старт».</i>")
        return
    if not g.start(await disabled_roles(), min_pl):
        await cb.message.answer(f"❌ Нужно минимум {min_pl} игроков.")
        return
    for uid in g.players:
        await dm(s.bot, uid, role_dm(g, uid))
    m = len(g.mafia_team_uids())
    try:
        await s.lobby_msg.edit_text(texts.START_TEXT.format(n=len(g.players), m=m))
    except Exception:
        pass
    s.task = asyncio.create_task(run_game(s))


@router.callback_query(F.data.startswith("mf:"))
async def mafia_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    act = parts[1]
    gid = int(parts[2])
    s = next((x_ for x_ in sessions.values() if x_.game.gid == gid), None)
    if not s:
        await cb.answer("Эта игра уже закончилась", show_alert=True)
        return
    g = s.game
    user = cb.from_user

    if act in ("j", "l", "s", "x"):
        if g.phase != "lobby":
            await cb.answer("Игра уже началась", show_alert=True)
            return
        if act == "j":
            if await is_user_banned(user.id):
                await cb.answer("Ты в чёрном списке игр", show_alert=True)
                return
            if not await can_dm(cb.bot, user.id):
                me = await cb.bot.me()
                await cb.answer("Сначала нажми Start у бота", url=f"https://t.me/{me.username}?start=mafia")
                return
            if not g.add_player(user.id, user.first_name or user.username or "Игрок"):
                await cb.answer("Ты уже в лобби")
                return
            await cb.answer("Ты в игре!")
            await refresh_lobby(s)
        elif act == "l":
            if g.remove_player(user.id):
                await cb.answer("Ты вышел из лобби")
                await refresh_lobby(s)
            else:
                await cb.answer("Тебя нет в лобби")
        elif act == "s":
            if user.id not in (g.creator_id, ADMIN_ID):
                await cb.answer("Начать игру может создатель лобби или админ", show_alert=True)
                return
            min_pl = int(await cfg("mafia_min_players", "5"))
            if len(g.players) < min_pl:
                await cb.answer(f"Нужно минимум {min_pl} игроков", show_alert=True)
                return
            await cb.answer()
            await begin_game(s, cb)
        else:
            if user.id not in (g.creator_id, ADMIN_ID):
                await cb.answer("Отменить может создатель или админ", show_alert=True)
                return
            _drop(g.chat_id)
            await cb.message.edit_text("🎭 <b>Лобби мафии отменено</b>")
            await cb.answer()
        return

    if act == "c":
        if g.phase != "night" or user.id not in g.players:
            await cb.answer("Сейчас нельзя", show_alert=True)
            return
        opts = g.night_options(user.id)
        variant = parts[3]
        if not opts or opts["mode"] != "commissar":
            await cb.answer("Сейчас нельзя", show_alert=True)
            return
        if variant == "skip":
            g.submit_night(user.id, None)
            s.dm_text[user.id] = "Ты пропустил ход."
            await cb.message.edit_text("Ты пропустил ход.")
            await cb.answer()
            _check_night_done(s)
            return
        if variant == "shoot" and not opts["can_shoot"]:
            await cb.answer("В первую ночь стрелять нельзя", show_alert=True)
            return
        s.variant[user.id] = variant
        await cb.message.edit_text(
            "🔍 <b>Кого проверить?</b>" if variant == "check" else "🔫 <b>В кого стрелять?</b>",
            reply_markup=targets_kb(g, opts["targets"], user.id))
        await cb.answer()
        return

    if act == "n":
        if g.phase != "night" or user.id not in g.players:
            await cb.answer("Сейчас нельзя", show_alert=True)
            return
        target = int(parts[3])
        opts = g.night_options(user.id)
        if not opts:
            await cb.answer("У тебя нет ночного действия", show_alert=True)
            return
        if target == 0:
            ok = g.submit_night(user.id, None)
            text = "Ты пропустил ход."
        else:
            ok = g.submit_night(user.id, target, s.variant.get(user.id))
            text = f"Ты выбрал: <b>{uid_name(g, target)}</b>" if ok else ""
        if not ok:
            await cb.answer("Нельзя выбрать этого игрока", show_alert=True)
            return
        s.dm_text[user.id] = text
        await cb.message.edit_text(text)
        await cb.answer()
        role = g.players[user.id].role
        if target and role in ("don", "mafia"):
            for mate in g.group_killers():
                if mate != user.id:
                    await dm(cb.bot, mate, f"🤵 {uid_name(g, user.id)} выбрал жертвой: "
                                           f"<b>{uid_name(g, target)}</b>")
        _check_night_done(s)
        return

    if act == "v":
        if g.phase != "vote":
            await cb.answer("Голосование закончилось", show_alert=True)
            return
        if user.id not in g.players or not g.players[user.id].alive:
            await cb.answer("Ты не можешь голосовать", show_alert=True)
            return
        if user.id not in g.voters():
            await cb.answer("Сегодня ты не можешь голосовать", show_alert=True)
            return
        target = int(parts[3])
        if not g.submit_vote(user.id, target or None):
            await cb.answer("Нельзя голосовать за этого игрока", show_alert=True)
            return
        await cb.answer("Голос принят" if target else "Ты воздержался")
        try:
            await s.vote_msg.edit_text(
                texts.VOTE_TEXT.format(done=len(g.votes), total=len(g.voters())),
                reply_markup=vote_kb(g))
        except TelegramBadRequest:
            pass
        if g.all_voted():
            s.vote_event.set()
        return

    if act == "k":
        if s.kamikaze_pick is None and user.id in g.players:
            target = int(parts[3])
            s.kamikaze_pick = target or -1
            await cb.message.edit_text("Выбор сделан." if target else "Ты никого не забираешь.")
            s.kamikaze_event.set()
        await cb.answer()


def _check_night_done(s: Session):
    if s.game.all_night_done():
        s.night_event.set()


def targets_kb(g: MafiaGame, targets, uid: int, prefix: str = "n", skip: bool = True):
    rows = name_rows(targets, lambda tid: f"mf:{prefix}:{g.gid}:{tid}")
    if skip:
        rows.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"mf:{prefix}:{g.gid}:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def vote_kb(g: MafiaGame):
    targets = [(p.uid, p.name) for p in g.alive()]
    kb = targets_kb(g, targets, 0, prefix="v", skip=False)
    kb.inline_keyboard.append([InlineKeyboardButton(text="🤐 Воздержаться",
                                                    callback_data=f"mf:v:{g.gid}:0")])
    return kb


async def send_night_prompt(s: Session, uid: int, opts: dict):
    g = s.game
    role = opts["role"]
    prompt = f"🌙 <b>Ночь {g.day}</b>\n\n{texts.NIGHT_PROMPTS[role]}"
    if opts["mode"] == "commissar":
        rows = [[InlineKeyboardButton(text="🔍 Проверить", callback_data=f"mf:c:{g.gid}:check")]]
        if opts["can_shoot"]:
            rows[0].append(InlineKeyboardButton(text="🔫 Стрелять", callback_data=f"mf:c:{g.gid}:shoot"))
        rows.append([InlineKeyboardButton(text="⏭ Пропустить", callback_data=f"mf:c:{g.gid}:skip")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
    else:
        kb = targets_kb(g, opts["targets"], uid)
    m = await dm(s.bot, uid, prompt, kb)
    if m:
        s.dm[uid] = m


async def _deliver_private(s: Session, priv):
    for uid, lines in priv.items():
        body = "\n".join(esc(l) for l in lines)
        m = s.dm.get(uid)
        if m:
            base = s.dm_text.get(uid, "")
            try:
                await m.edit_text((base + "\n\n" if base else "") + body)
                continue
            except Exception:
                pass
        await dm(s.bot, uid, body)


async def _clear_night_msg(s: Session):
    if s.night_msg:
        try:
            await s.night_msg.delete()
        except Exception:
            pass
        s.night_msg = None


async def night_phase(s: Session) -> str:
    g, bot = s.game, s.bot
    g.begin_night()
    s.dm.clear()
    s.dm_text.clear()
    s.variant.clear()
    s.night_event.clear()
    s.night_msg = await bot.send_message(g.chat_id, texts.NIGHT_TEXT.format(day=g.day))
    for p in g.alive():
        opts = g.night_options(p.uid)
        if opts:
            await send_night_prompt(s, p.uid, opts)
    if g.all_night_done():
        await pause.sleep(4)
    else:
        try:
            await pause.wait_for(s.night_event, int(await cfg("mafia_night_sec", "60")))
        except asyncio.TimeoutError:
            pass
    for uid, m in s.dm.items():
        if uid not in g.night:
            s.dm_text[uid] = "⌛ Время вышло, ты не успел сделать ход."
            try:
                await m.edit_text(s.dm_text[uid])
            except Exception:
                pass
    res = g.resolve_night()
    await _deliver_private(s, res.priv)
    dead = [(p.uid, p.name, NAME[p.role]) for p, _ in res.deaths]
    lines = await gamemute.mute_dead(bot, g.chat_id, dead, "убит")
    s.muted.update(u for u, _, _ in dead)
    text = "☀️ <b>Наступило утро</b>\n\n" + ("\n".join(lines) if lines else "<i>Этой ночью никто не погиб.</i>")
    for line in res.pub:
        text += f"\n\n🤓 {esc(line)}"
    return text


async def vote_phase(s: Session, morning: str):
    g, bot = s.game, s.bot
    alive = "\n".join(f"• {esc(p.name)}" for p in g.alive())
    sec = int(await cfg("mafia_day_sec", "90"))
    await bot.send_message(g.chat_id, morning + "\n\n" + texts.DAY_TEXT.format(day=g.day, sec=sec, alive=alive))
    await _clear_night_msg(s)
    await pause.sleep(sec)
    g.begin_vote()
    s.vote_event.clear()
    if not g.voters():
        g.resolve_vote()
        return
    s.vote_msg = await bot.send_message(
        g.chat_id, texts.VOTE_TEXT.format(done=0, total=len(g.voters())), reply_markup=vote_kb(g))
    try:
        await pause.wait_for(s.vote_event, int(await cfg("mafia_vote_sec", "45")))
    except asyncio.TimeoutError:
        pass
    tally = g.vote_tally()
    res = g.resolve_vote()
    rows = [f"• {esc(g.players[t].name)}: {c}" for t, c in tally.most_common()]
    head = "🗳 <b>Итоги голосования</b>\n\n" + ("\n".join(rows) + "\n\n" if rows else "")
    dead = [(p.uid, p.name, NAME[p.role]) for p in g.players.values() if not p.alive and p.uid not in s.muted]
    mute_lines = await gamemute.mute_dead(bot, g.chat_id, dead, "казнён")
    s.muted.update(u for u, _, _ in dead)
    text = head + ("\n".join(mute_lines) if mute_lines else esc(res.text))
    try:
        await s.vote_msg.edit_text(text)
    except Exception:
        await bot.send_message(g.chat_id, text)
    if res.kamikaze:
        victim = await kamikaze_phase(s, res.kamikaze)
        if victim:
            lines = await gamemute.mute_dead(bot, g.chat_id, [(victim.uid, victim.name, NAME[victim.role])],
                                             "унесён камикадзе")
            s.muted.add(victim.uid)
            text += "\n\n💣 " + "\n".join(lines)
        else:
            text += "\n\n💣 Камикадзе никого не забрал."
        try:
            await s.vote_msg.edit_text(text)
        except Exception:
            pass


async def kamikaze_phase(s: Session, kuid: int):
    g, bot = s.game, s.bot
    s.kamikaze_pick = None
    s.kamikaze_event.clear()
    targets = [(p.uid, p.name) for p in g.alive()]
    if not targets:
        return None
    await dm(bot, kuid, "💣 Тебя линчевали! Выбери, кто уйдёт с тобой:",
             targets_kb(g, targets, kuid, prefix="k"))
    try:
        await pause.wait_for(s.kamikaze_event, 30)
    except asyncio.TimeoutError:
        pass
    pick = s.kamikaze_pick if s.kamikaze_pick and s.kamikaze_pick > 0 else None
    return g.kamikaze_take(pick)


async def run_game(s: Session):
    g, bot = s.game, s.bot
    try:
        for _ in range(40):
            morning = await night_phase(s)
            w = g.check_win()
            if w:
                await _clear_night_msg(s)
                await bot.send_message(g.chat_id, morning)
                return await end_game(s, w)
            await vote_phase(s, morning)
            w = g.check_win()
            if w:
                return await end_game(s, w)
        await end_game(s, "nobody")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в игре мафии")
        _drop(g.chat_id)
        try:
            await bot.send_message(g.chat_id, "⚠️ В игре произошла ошибка, игра остановлена.")
        except Exception:
            pass


WIN_TEXT = {
    "town": "👨 <b>Победа мирных жителей!</b>\nВся мафия и опасные одиночки устранены.",
    "mafia": "🤵 <b>Победа мафии!</b>\nМафия захватила город.",
    "maniac": "🔪 <b>Победа маньяка!</b>",
    "arsonist": "🧟 <b>Победа поджигателя!</b>\nГород сгорел.",
    "suicide": "🤦 <b>Победа самоубийцы!</b>\nОн добился своего: его линчевали.",
    "nobody": "🕊 <b>Ничья.</b>\nПобедителей нет.",
}


async def end_game(s: Session, key: str):
    g, bot = s.game, s.bot
    g.phase = "end"
    _drop(g.chat_id)
    winners = g.winners(key)
    win_ids = {p.uid for p in winners}
    text = WIN_TEXT[key] + "\n\n<b>Роли игроков:</b>\n"
    for p in g.players.values():
        mark = "🏆" if p.uid in win_ids else ("☠️" if not p.alive else "▫️")
        text += f"{mark} {esc(p.name)} — {EMOJI[p.start_role]} {NAME[p.start_role]}\n"
    await bot.send_message(g.chat_id, text)
    try:
        roles = {p.name: p.start_role for p in g.players.values()}
        await x("INSERT INTO mafia_games (winner, roles_json, created_at) VALUES (?,?,?)",
                (key, json.dumps(roles, ensure_ascii=False), ts()))
        for p in g.players.values():
            await add_game_result(p.uid, "mafia", "win" if p.uid in win_ids else "loss")
    except Exception:
        log.exception("Не удалось сохранить итоги мафии")
