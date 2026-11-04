from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID
from games import core as games_core
from games.core import GAMES, open_lobby
from games.party.mafia.handlers import open_mafia_lobby
from services import features
from utils.emoji import e
from utils.fmt import code, esc
from utils.ui import kb

router = Router()

TWO_PLAYER = {
    "ttt": ("Крестики-нолики", "/ttt @username", "⭕"),
    "rps": ("Камень-ножницы-бумага", "/rps @username", "🎲"),
    "duel": ("Дуэль на скорость", "/duel @username", "🗡"),
    "checkers": ("Шашки", "/checkers @username", "♟"),
    "seabattle": ("Морской бой", "/seabattle @username или /seabattle bot", "🚢"),
    "hangman": ("Виселица", "/hangman @username", "🪢"),
}


def lobby_games():
    return [(k, g) for k, g in GAMES.items() if features.is_on(g.command or k)]


def two_player():
    return [(k, v) for k, v in TWO_PLAYER.items() if features.is_on(k)]


def games_markup():
    rows = []
    if features.is_on("mafia"):
        rows.append([("🎭 Мафия", "gm:play:mafia")])
    row = []
    for key, g in lobby_games():
        row.append((f"🎮 {g.title}", f"gm:play:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    row = []
    for key, (title, usage, icon) in two_player():
        row.append((f"{icon} {title}", f"gm:info:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if features.is_on("top_games"):
        rows.append([("🥇 Рейтинг игр", "gm:top")])
    return kb(rows)


def games_text() -> str:
    t = f"{e('game')} <b>Игры</b>\n\n"
    games = lobby_games()
    if games or features.is_on("mafia"):
        t += "<b>На несколько игроков</b> (лобби, нажми кнопку ниже):\n"
        if features.is_on("mafia"):
            t += "• Мафия: 5+ игроков, роли в личке\n"
        for _, g in games:
            mx = f"–{g.max_players}" if g.max_players != g.min_players else ""
            t += f"• {esc(g.title)}: {g.min_players}{mx} игр.\n"
    duo = two_player()
    if duo:
        t += "\n<b>На двоих:</b> " + ", ".join(code(u.split(" или ")[0]) for _, (_, u, _) in duo) + "\n"
        t += "<i>Вместо @username можно ID или ответ командой на сообщение соперника.</i>"
    if not games and not duo and not features.is_on("mafia"):
        t += "<i>Сейчас все игры отключены.</i>"
    return t


@router.message(Command("games"))
async def cmd_games(message: Message):
    await message.answer(games_text(), reply_markup=games_markup())


@router.callback_query(F.data.startswith("gm:"))
async def games_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    act = parts[1]
    if act == "info":
        if parts[2] not in TWO_PLAYER or not features.is_on(parts[2]):
            await cb.answer("Эта игра отключена", show_alert=True)
            return
        title, usage, _ = TWO_PLAYER[parts[2]]
        await cb.answer(f"{title}: напиши {usage} или ответь командой на сообщение соперника.", show_alert=True)
        return
    if act == "top":
        from games.ratings import rating_kb, rating_text
        await cb.message.answer(await rating_text("all"), reply_markup=rating_kb("all"))
        await cb.answer()
        return
    key = parts[2]
    command = GAMES[key].command or key if key in GAMES else key
    if not features.is_on(command):
        await cb.answer("Эта игра отключена", show_alert=True)
        return
    from middlewares.ratelimit import allow_lobby
    if not allow_lobby(cb.from_user.id):
        await cb.answer("⏳ Слишком много действий. Попробуй немного позже.", show_alert=True)
        return
    if key == "mafia":
        err = await open_mafia_lobby(cb.bot, cb.message.chat.id, cb.from_user)
    elif key in GAMES:
        err = await open_lobby(cb.bot, cb.message.chat.id, cb.from_user, GAMES[key])
    else:
        err = "Игра не найдена."
    await cb.answer(err or "Лобби открыто!", show_alert=bool(err))


def creator_of(chat_id: int):
    lobby = games_core.lobbies.get(chat_id)
    if lobby:
        return lobby.creator_id
    from games.party.mafia.handlers import sessions
    sess = sessions.get(chat_id)
    return sess.game.creator_id if sess else None


@router.message(Command("stopgame", "stopmafia"))
async def cmd_stopgame(message: Message):
    uid = message.from_user.id
    owner = creator_of(message.chat.id)
    if uid != ADMIN_ID and uid != owner:
        await message.answer("❌ Остановить игру может только её создатель или админ.")
        return
    by = "админом" if uid == ADMIN_ID else "создателем"
    if await games_core.stop_active(message.bot, message.chat.id, f"Игра остановлена {by}."):
        from database.moderation import log_mod
        await log_mod(uid, 0, "game_stop", f"чат {message.chat.id}")
        return
    await message.answer("Сейчас нет активной игры.")
