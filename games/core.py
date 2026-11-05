import asyncio
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
from utils.logs import logger
from utils.timeutils import MSK
from utils.ui import kb

router = Router()

registry: Dict[int, dict] = {}
lobbies: Dict[int, "Lobby"] = {}
GAMES: Dict[str, "GameDef"] = {}
_gid = 1000


def claim(chat_id: int, key: str, title: str, stop: Optional[Callable] = None) -> bool:
    if chat_id in registry:
        return False
    registry[chat_id] = {"key": key, "title": title, "stop": stop, "warned": False, "owner": None}
    return True


def release(chat_id: int, owner=None):
    """Снимает бронь чата. С owner снимает только если бронь всё ещё принадлежит ему:
    старая игра при завершении не должна снять бронь новой."""
    info = registry.get(chat_id)
    if owner is not None and (info is None or info.get("owner") is not owner):
        return
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
    if registry.get(chat_id) is info:      # за время stop бронь могла перейти новой игре
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
    except TelegramAPIError:
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
    dm_note: str = ""  # подсказка в лобби про личку; по умолчанию — про роли


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
        self.starting = False      # ставится синхронно, до первого await, чтобы второй «Старт» не прошёл
        self.stopped = False       # лобби закрыли (/stopgame, отмена, таймаут): запуск больше не продолжается
        self.data: dict = {}

    def text(self) -> str:
        g = self.game
        names = "\n".join(f"• {esc(n)}" for n in self.players.values()) or "<i>пока никого</i>"
        mx = f"–{g.max_players}" if g.max_players != g.min_players else ""
        dm_note = (f"\n<i>{esc(g.dm_note or 'Роли приходят в личку: один раз нажми Start у бота.')}</i>"
                   if g.needs_dm else "")
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
    lobby.stopped = True
    ref = lobby.data.get("escrow")
    if ref:      # если партия не рассчитана (отмена, сбой, остановка), ставки вернутся; после расчёта это no-op
        from services import economy as _ec
        _ec.escrow_refund_bg(ref)
    if lobbies.get(lobby.chat_id) is lobby:
        lobbies.pop(lobby.chat_id, None)
    release(lobby.chat_id, owner=lobby)


async def _lobby_stop(bot, chat_id: int, reason: str):
    lobby = lobbies.get(chat_id)
    if not lobby:
        return
    lobby.stopped = True
    if lobby.task and lobby.task is not asyncio.current_task():
        lobby.task.cancel()
    lobbies.pop(chat_id, None)
    if lobby.data.get("escrow"):
        from services import economy as _ec
        try:
            await _ec.escrow_refund(lobby.data["escrow"])
        except Exception:
            logger.exception("Не удалось вернуть эскроу при остановке лобби %s", chat_id)
    try:
        await bot.send_message(chat_id, f"{e('stop')} <b>{esc(lobby.game.title)} остановлена</b>\n"
                                        f"<i>{esc(reason)}</i>")
    except Exception:
        logger.exception("Не удалось отправить сообщение об остановке игры в чат %s", chat_id)


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
    registry[chat_id]["owner"] = lobby
    lobby.players[user.id] = user.first_name or user.username or "Игрок"
    try:
        lobby.msg = await bot.send_message(chat_id, lobby.text(), reply_markup=lobby.markup())
    except Exception:
        finish(lobby)      # не оставляем чат занятым, если сообщение лобби не отправилось
        raise
    asyncio.create_task(_lobby_expire(lobby))
    return None


async def _lobby_expire(lobby: Lobby):
    await asyncio.sleep(900)
    if lobbies.get(lobby.chat_id) is lobby and not (lobby.started or lobby.starting):
        finish(lobby)
        try:
            await lobby.msg.edit_text(f"{e('stop')} <b>Лобби закрыто</b>\n<i>Время ожидания вышло.</i>")
        except Exception:
            logger.debug("Не удалось обновить сообщение закрытого лобби %s", lobby.chat_id, exc_info=True)


@router.callback_query(F.data.startswith("lb:"))
async def lobby_cb(cb: CallbackQuery):
    _, act, gid = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby:
        await cb.answer("Это лобби уже закрыто", show_alert=True)
        return
    if lobby.started or lobby.starting:
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
        # пока шёл await, лобби могли запустить, закрыть или заполнить
        if lobbies.get(lobby.chat_id) is not lobby or lobby.started or lobby.starting:
            await cb.answer("Игра уже началась", show_alert=True)
            return
        if user.id in lobby.players:
            await cb.answer("Ты уже в лобби")
            return
        if len(lobby.players) >= g.max_players:
            await cb.answer("Лобби заполнено", show_alert=True)
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
        if lobby.starting:
            await cb.answer("Игра уже запускается")
            return
        lobby.starting = True                  # до первого await: двойной «Старт» не пройдёт
        try:
            await cb.answer()
            if g.needs_dm:
                bad = []
                for uid, n in list(lobby.players.items()):
                    if not await can_dm(cb.bot, uid):
                        bad.append(n)
                if bad:
                    await cb.message.answer(
                        "❌ Не могу написать в личку: " + ", ".join(f"<b>{esc(n)}</b>" for n in bad) +
                        "\n<i>Откройте бота, нажмите Start и снова нажмите «Старт».</i>")
                    return
            if lobbies.get(lobby.chat_id) is not lobby:   # лобби закрыли за время проверок
                return
            lobby.started = True
        finally:
            lobby.starting = False
        try:
            await lobby.msg.edit_text(f"{e(g.emoji)} <b>{esc(g.title)}</b>\n\n"
                                      f"<i>Игра началась! Игроков: {len(lobby.players)}</i>")
        except TelegramBadRequest:
            pass
        # Пока шло редактирование, /stopgame, отмена или таймаут могли закрыть лобби.
        if lobby.stopped or lobbies.get(lobby.chat_id) is not lobby:
            finish(lobby)
            return
        # Сам запуск — отдельная задача: /stopgame успеет отменить и его, а не только уже созданную игру.
        task = asyncio.create_task(g.start(lobby))
        lobby.task = task
        try:
            await task
        except asyncio.CancelledError:
            finish(lobby)
            return
        except Exception:
            logger.exception("Не удалось запустить игру %s в чате %s", g.title, lobby.chat_id)
            finish(lobby)
            try:
                await cb.message.answer("⚠️ Не удалось запустить игру.")
            except Exception:
                logger.debug("Не удалось сообщить о сбое запуска игры", exc_info=True)


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
                    logger.exception("Не удалось предупредить чат %s о тихом времени", chat_id)


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
        try:
            await add_game_result(uid, game_key, "win" if uid in win else "loss")
        except Exception:
            logger.exception("Не удалось записать результат игры %s для игрока %s", game_key, uid)
