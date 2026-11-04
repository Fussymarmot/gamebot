import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Dict, Optional

from aiogram import F, Router
from aiogram.filters import Filter
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.game_stats import add_game_result
from database.moderation import is_user_banned
from games import gamemute, pause
from services.quiet import quiet_config, quiet_state
from utils.emoji import e
from utils.fmt import esc
from utils.timeutils import MSK
from utils.ui import kb

router = Router()
log = logging.getLogger(__name__)

registry: Dict[int, dict] = {}
lobbies: Dict[int, "Lobby"] = {}
GAMES: Dict[str, "GameDef"] = {}
_gid = 1000


def claim(chat_id: int, key: str, title: str, stop: Optional[Callable] = None) -> bool:
    if chat_id in registry:
        return False
    registry[chat_id] = {"key": key, "title": title, "stop": stop, "warned": False}
    return True


def release(chat_id: int):
    registry.pop(chat_id, None)
    gamemute.schedule_unmute(chat_id)


def active(chat_id: int) -> Optional[dict]:
    return registry.get(chat_id)


async def stop_active(bot, chat_id: int, reason: str) -> bool:
    info = registry.get(chat_id)
    if not info:
        return False
    stop = info.get("stop")
    if stop:
        await stop(bot, chat_id, reason)
    release(chat_id)
    return True


async def can_dm(bot, uid: int) -> bool:
    try:
        await bot.send_chat_action(uid, "typing")
        return True
    except TelegramAPIError:
        return False


async def dm(bot, uid: int, text: str, markup=None):
    try:
        return await bot.send_message(uid, text, reply_markup=markup)
    except TelegramAPIError as ex:
        log.warning("Не удалось написать игроку %s: %s", uid, ex)
        return None


@dataclass
class GameDef:
    key: str
    title: str
    emoji: str
    description: str
    min_players: int
    max_players: int
    start: Callable[["Lobby"], Awaitable[None]]
    needs_dm: bool = False
    command: str = ""


def register(game: GameDef):
    GAMES[game.key] = game


class Lobby:
    def __init__(self, bot, game: GameDef, chat_id: int, creator_id: int, gid: int):
        self.bot = bot
        self.game = game
        self.chat_id = chat_id
        self.creator_id = creator_id
        self.gid = gid
        self.players: Dict[int, str] = {}
        self.msg: Optional[Message] = None
        self.task: Optional[asyncio.Task] = None
        self.started = False
        self.data: dict = {}

    def text(self) -> str:
        g = self.game
        names = "\n".join(f"• {esc(n)}" for n in self.players.values()) or "<i>пока никого</i>"
        mx = f"–{g.max_players}" if g.max_players != g.min_players else ""
        dm_note = ("\n<i>Роли приходят в личку: один раз нажми Start у бота.</i>" if g.needs_dm else "")
        note = f"\n<b>{esc(self.data['note'])}</b>" if self.data.get("note") else ""
        return (f"{e(g.emoji)} <b>{esc(g.title)}</b>\n<i>{esc(g.description)}</i>{note}\n\n"
                f"Игроков: <b>{len(self.players)}</b> (нужно {g.min_players}{mx})\n\n{names}{dm_note}")

    def markup(self):
        return kb([[("➕ Войти", f"lb:j:{self.gid}"), ("➖ Выйти", f"lb:l:{self.gid}")],
                   [("▶️ Старт", f"lb:s:{self.gid}"), ("🚫 Отмена", f"lb:x:{self.gid}")]])

    async def refresh(self):
        try:
            await self.msg.edit_text(self.text(), reply_markup=self.markup())
        except TelegramBadRequest:
            pass

    def names(self) -> Dict[int, str]:
        return dict(self.players)


def finish(lobby: Lobby):
    if lobbies.get(lobby.chat_id) is lobby:
        lobbies.pop(lobby.chat_id, None)
    release(lobby.chat_id)


async def _lobby_stop(bot, chat_id: int, reason: str):
    lobby = lobbies.get(chat_id)
    if not lobby:
        return
    if lobby.task and lobby.task is not asyncio.current_task():
        lobby.task.cancel()
    lobbies.pop(chat_id, None)
    try:
        await bot.send_message(chat_id, f"{e('stop')} <b>{esc(lobby.game.title)} остановлена</b>\n"
                                        f"<i>{esc(reason)}</i>")
    except Exception:
        pass


async def open_lobby(bot, chat_id: int, user, game: GameDef) -> Optional[str]:
    global _gid
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    info = active(chat_id)
    if info:
        return f"Сейчас уже идёт игра: {info['title']}. Дождись окончания."
    if not claim(chat_id, game.key, game.title, _lobby_stop):
        return "Сейчас уже идёт игра."
    _gid += 1
    lobby = Lobby(bot, game, chat_id, user.id, _gid)
    lobbies[chat_id] = lobby
    lobby.players[user.id] = user.first_name or user.username or "Игрок"
    lobby.msg = await bot.send_message(chat_id, lobby.text(), reply_markup=lobby.markup())
    asyncio.create_task(_lobby_expire(lobby))
    return None


async def _lobby_expire(lobby: Lobby):
    await asyncio.sleep(900)
    if lobbies.get(lobby.chat_id) is lobby and not lobby.started:
        finish(lobby)
        try:
            await lobby.msg.edit_text(f"{e('stop')} <b>Лобби закрыто</b>\n<i>Время ожидания вышло.</i>")
        except Exception:
            pass


@router.callback_query(F.data.startswith("lb:"))
async def lobby_cb(cb: CallbackQuery):
    _, act, gid = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby:
        await cb.answer("Это лобби уже закрыто", show_alert=True)
        return
    if lobby.started:
        await cb.answer("Игра уже началась", show_alert=True)
        return
    user = cb.from_user
    g = lobby.game
    name = user.first_name or user.username or "Игрок"

    if act == "j":
        if await is_user_banned(user.id):
            await cb.answer("Ты в чёрном списке игр", show_alert=True)
            return
        if user.id in lobby.players:
            await cb.answer("Ты уже в лобби")
            return
        if len(lobby.players) >= g.max_players:
            await cb.answer("Лобби заполнено", show_alert=True)
            return
        if g.needs_dm:
            if not await can_dm(cb.bot, user.id):
                from utils.startlink import prompt_start
                await prompt_start(cb, "game")
                return
        lobby.players[user.id] = name
        await cb.answer("Ты в игре!")
        await lobby.refresh()
    elif act == "l":
        if user.id in lobby.players and user.id != lobby.creator_id:
            del lobby.players[user.id]
            await cb.answer("Ты вышел из лобби")
            await lobby.refresh()
        elif user.id == lobby.creator_id:
            await cb.answer("Создатель может только отменить игру", show_alert=True)
        else:
            await cb.answer("Тебя нет в лобби")
    elif act == "x":
        if user.id not in (lobby.creator_id, ADMIN_ID):
            await cb.answer("Отменить может создатель или админ", show_alert=True)
            return
        finish(lobby)
        await cb.message.edit_text(f"{e('stop')} <b>{esc(g.title)}: лобби отменено</b>")
        await cb.answer()
    elif act == "s":
        if user.id not in (lobby.creator_id, ADMIN_ID):
            await cb.answer("Начать может создатель лобби или админ", show_alert=True)
            return
        if len(lobby.players) < g.min_players:
            await cb.answer(f"Нужно минимум {g.min_players} игроков", show_alert=True)
            return
        await cb.answer()
        if g.needs_dm:
            bad = []
            for uid, n in lobby.players.items():
                if not await can_dm(cb.bot, uid):
                    bad.append(n)
            if bad:
                await cb.message.answer(
                    "❌ Не могу написать в личку: " + ", ".join(f"<b>{esc(n)}</b>" for n in bad) +
                    "\n<i>Откройте бота, нажмите Start и снова нажмите «Старт».</i>")
                return
        lobby.started = True
        try:
            await lobby.msg.edit_text(f"{e(g.emoji)} <b>{esc(g.title)}</b>\n\n"
                                      f"<i>Игра началась! Игроков: {len(lobby.players)}</i>")
        except TelegramBadRequest:
            pass
        try:
            await g.start(lobby)
        except Exception:
            log.exception("Ошибка запуска игры %s", g.key)
            finish(lobby)
            await cb.message.answer("⚠️ Не удалось запустить игру.")


async def quiet_watch(bot):
    if not registry:
        return
    enabled, start, end = await quiet_config()
    if not enabled:
        return
    quiet, _ = await quiet_state()
    if quiet:
        for chat_id in list(registry):
            await stop_active(bot, chat_id,
                              f"Наступило тихое время (до {end:02d}:00 по Москве).")
        return
    now = datetime.now(MSK)
    target = now.replace(hour=start, minute=0, second=0, microsecond=0)
    minutes = (target - now).total_seconds() / 60
    if 0 < minutes <= 5:
        for chat_id, info in registry.items():
            if not info["warned"]:
                info["warned"] = True
                try:
                    await bot.send_message(
                        chat_id, f"🌙 Через {int(minutes) + 1} мин. начнётся тихое время, "
                                 "игра будет завершена.")
                except Exception:
                    pass


class InGame(Filter):

    def __init__(self, key: str):
        self.key = key

    async def __call__(self, message: Message) -> bool:
        if message.chat.id != GROUP_ID or not message.from_user or not message.text:
            return False
        if message.text.startswith("/"):
            return False
        lobby = lobbies.get(message.chat.id)
        return bool(lobby and lobby.started and lobby.game.key == self.key
                    and message.from_user.id in lobby.players)


async def record_results(game_key: str, players, winners):
    players = list(players)
    if len(players) < 2:
        return
    win = set(winners)
    for uid in players:
        await add_game_result(uid, game_key, "win" if uid in win else "loss")
