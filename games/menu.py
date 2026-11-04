"""Меню игр: /games и кнопки запуска.

Любую игру можно запустить кнопкой. Для игр со ставкой (игры на двоих и рулетка) после нажатия бот
предлагает выбрать ставку, затем открывает набор. Команды (/ttt 20, /roulette 50, /seabattle bot...)
работают как раньше.
"""
import asyncio
from typing import Dict, List, Optional

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import ADMIN_ID, GROUP_ID
from database.moderation import is_user_banned
from games import core as games_core
from games import invites, pause
from games.core import GAMES, open_lobby
from games.duel import duel, hangman, rps, seabattle, tictactoe  # noqa: F401: регистрируют игры в DUELS
from games.duel.challenge import DUELS, MAX_STAKE, NEED_DM, STAKE_STEPS, create_challenge
from games.party import roulette
from games.party.mafia.handlers import open_mafia_lobby
from services import economy as ec
from services import features
from utils import ui
from utils.emoji import EMOJI, RUBY, e
from utils.fmt import code, esc

router = Router()

# Кастомные эмодзи на кнопках меню. Если Telegram их не принял, на кнопке остаётся обычный смайлик из FALLBACK.
ICONS = {
    "code": "5472308992514464048",       # Взломай код
    "wordseek": "5397575638146110953",   # WordSeek
    "assoc": "5834515786588229330",      # Ассоциации
    "roulette": "5235762367312173706",   # Русская рулетка
    "bunker": "5197253849450042423",     # Бункер
    "seabattle": "5188322825735267247",  # Морской бой
}
RUBY_ID = EMOJI["ruby"][0]
FALLBACK = {
    "mafia": "🎭", "code": "🔑", "wordseek": "🔤", "assoc": "💬", "roulette": "🔫", "bunker": "🛡",
    "seabattle": "🚢", "ttt": "⭕", "rps": "🎲", "duel": "🗡", "hangman": "🪢", "memory": "🎮", "whomost": "💬",
}
# Короткие подписи для кнопок (полные названия остаются в тексте и в лобби)
LABELS = {"bunker": "Бункер", "whomost": "Кто скорее всего", "duel": "Дуэль"}
USAGE = {
    "ttt": "/ttt [ставка]", "rps": "/rps [ставка]", "duel": "/duel [ставка]",
    "seabattle": "/seabattle [ставка] или /seabattle bot", "hangman": "/hangman [ставка]",
}
# Порядок кнопок. Игры, которых тут нет, добавляются в конец.
LOBBY_ORDER = ["bunker", "roulette", "assoc", "code", "wordseek", "memory", "whomost"]
DUEL_ORDER = ["ttt", "duel", "seabattle", "hangman", "rps"]
ONE_LINE = 15  # подпись длиннее этого числа знаков не влезает в половину строки: кнопка занимает всю строку

PICKER_SEC = 60
pickers: Dict[int, Message] = {}  # меню выбора ставки: одно на игрока


def has_stake(key: str) -> bool:
    return key == "roulette" or key in DUELS


def lobby_games():
    order = {k: i for i, k in enumerate(LOBBY_ORDER)}
    games = [(k, g) for k, g in GAMES.items() if features.is_on(g.command or k)]
    return sorted(games, key=lambda kg: order.get(kg[0], len(order)))


def two_player():
    order = {k: i for i, k in enumerate(DUEL_ORDER)}
    duels = [(k, d) for k, d in DUELS.items() if features.is_on(k)]
    return sorted(duels, key=lambda kd: order.get(kd[0], len(order)))


def label_of(key: str, title: str) -> str:
    return LABELS.get(key, title)


def game_button(key: str, label: str, data: str) -> InlineKeyboardButton:
    icon = ICONS.get(key)
    if icon and ui.ICONS_ENABLED:
        return InlineKeyboardButton(text=label, callback_data=data, icon_custom_emoji_id=icon)
    return ui.btn(f"{FALLBACK.get(key, '🎮')} {label}", data)


def ruby_button(text: str, data: str) -> InlineKeyboardButton:
    if ui.ICONS_ENABLED:
        return InlineKeyboardButton(text=text, callback_data=data, icon_custom_emoji_id=RUBY_ID)
    return InlineKeyboardButton(text=f"{RUBY} {text}", callback_data=data)


def pack(buttons: List[tuple]) -> List[List[InlineKeyboardButton]]:
    """Кнопки по две в ряд; длинная подпись занимает ряд целиком (текст на ней по центру)."""
    rows, row = [], []
    for label, btn in buttons:
        if len(label) > ONE_LINE:
            if row:
                rows.append(row)
                row = []
            rows.append([btn])
            continue
        row.append(btn)
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def games_markup() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if features.is_on("mafia"):
        rows.append([game_button("mafia", "Мафия", "gm:play:mafia")])
    rows += pack([(label_of(k, g.title), game_button(k, label_of(k, g.title), f"gm:play:{k}"))
                  for k, g in lobby_games()])
    rows += pack([(label_of(k, d.title), game_button(k, label_of(k, d.title), f"gm:play:{k}"))
                  for k, d in two_player()])
    if features.is_on("top_games"):
        rows.append([ui.btn("🥇 Рейтинг игр", "gm:top")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def games_text() -> str:
    t = f"{e('game')} <b>Игры</b>\n\nНажми на игру, чтобы запустить её. Если в игре есть ставка, бот спросит сумму.\n\n"
    games = lobby_games()
    if games or features.is_on("mafia"):
        t += "<b>На несколько игроков</b> (лобби: «Войти» и «Старт»):\n"
        if features.is_on("mafia"):
            t += "• Мафия: 5+ игроков, роли в личке\n"
        for _, g in games:
            mx = f"–{g.max_players}" if g.max_players != g.min_players else ""
            t += f"• {esc(g.title)} ({g.min_players}{mx} игр.)\n"
    duo = two_player()
    if duo:
        t += "\n<b>На двоих</b> (игра начинается сразу, как только присоединится соперник):\n"
        t += "\n".join(f"• {esc(d.title)}: {code(USAGE.get(k, '/' + k))}" for k, d in duo) + "\n"
    if duo or features.is_on("roulette"):
        t += (f"\n<i>Ставка в рубинах необязательна: победитель забирает её у проигравшего. "
              f"Можно и командой, например {code('/ttt 20')} (до {MAX_STAKE}).</i>")
    if not games and not duo and not features.is_on("mafia"):
        t += "<i>Сейчас все игры отключены.</i>"
    return t


@router.message(Command("games"))
async def cmd_games(message: Message):
    await message.answer(games_text(), reply_markup=games_markup())


def is_enabled(key: str) -> bool:
    if key == "mafia":
        return features.is_on("mafia")
    if key in GAMES:
        return features.is_on(GAMES[key].command or key)
    return key in DUELS and features.is_on(key)


def title_of(key: str) -> str:
    if key == "mafia":
        return "Мафия"
    if key in GAMES:
        return GAMES[key].title
    return DUELS[key].title if key in DUELS else key


async def launch(bot, chat_id: int, user, key: str, stake: int = 0) -> Optional[str]:
    """Запускает игру кнопкой. None, если всё хорошо; иначе текст ошибки (или NEED_DM)."""
    if key == "mafia":
        return await open_mafia_lobby(bot, chat_id, user)
    if key == "roulette":
        return await roulette.open_roulette(bot, chat_id, user, stake)
    if key in DUELS:
        return await create_challenge(bot, chat_id, user, key, stake)
    if key in GAMES:
        return await open_lobby(bot, chat_id, user, GAMES[key])
    return "Игра не найдена."


async def precheck(chat_id: int, user, key: str) -> Optional[str]:
    """Быстрые проверки до выбора ставки, чтобы не заставлять выбирать сумму зря."""
    if chat_id != GROUP_ID:
        return "Игры запускаются в группе."
    if pause.is_paused():
        return "Сейчас игры на паузе."
    if await is_user_banned(user.id):
        return "Ты в чёрном списке игр."
    if key == "roulette":
        info = games_core.active(chat_id)
        if info:
            return f"Сейчас уже идёт игра: {info['title']}. Дождись окончания."
    elif key in DUELS and invites.is_busy(user.id):
        return "Ты уже участвуешь в игре."
    return None


def picker_markup(uid: int, key: str, balance: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Без ставки", callback_data=f"gm:st:{uid}:{key}:0")]]
    steps = [s for s in STAKE_STEPS if s <= balance]
    row = []
    for s in steps:
        row.append(ruby_button(str(s), f"gm:st:{uid}:{key}:{s}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if key == "seabattle":
        rows.append([InlineKeyboardButton(text="🤖 Играть против бота", callback_data=f"gm:bot:{uid}")])
    rows.append([ui.btn("🚫 Отмена", f"gm:sx:{uid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def picker_text(key: str, name: str, balance: int) -> str:
    t = (f"{e('game')} <b>{esc(title_of(key))}</b>\n"
         f"Игрок: <b>{esc(name)}</b>\n\n"
         f"Выбери ставку. Победитель забирает её у проигравшего.\n"
         f"У тебя: <b>{balance}</b> {RUBY}")
    if balance < STAKE_STEPS[0]:
        t += f"\n<i>Ставки от {STAKE_STEPS[0]} {RUBY} станут доступны, когда накопишь рубины.</i>"
    return t


async def _drop(msg: Message):
    try:
        await msg.delete()
    except TelegramAPIError:
        pass


async def _close_picker(uid: int, msg: Message):
    if pickers.get(uid) is msg:
        pickers.pop(uid, None)
    await _drop(msg)


async def _drop_later(uid: int, msg: Message):
    await asyncio.sleep(PICKER_SEC)
    await _close_picker(uid, msg)


async def _open_picker(cb: CallbackQuery, key: str):
    user = cb.from_user
    old = pickers.pop(user.id, None)
    if old:
        await _drop(old)
    balance = (await ec.wallet(user.id))[0]
    msg = await cb.message.answer(picker_text(key, user.first_name or user.username or "Игрок", balance),
                                  reply_markup=picker_markup(user.id, key, balance))
    pickers[user.id] = msg
    asyncio.create_task(_drop_later(user.id, msg))


async def _answer_error(cb: CallbackQuery, err: str):
    if err == NEED_DM:
        from utils.startlink import prompt_start
        await prompt_start(cb, "game")
    else:
        await cb.answer(err, show_alert=True)


@router.callback_query(F.data.startswith("gm:"))
async def games_cb(cb: CallbackQuery):
    parts = cb.data.split(":")
    act = parts[1]
    user = cb.from_user

    if act == "top":
        from games.ratings import rating_kb, rating_text
        await cb.message.answer(await rating_text("all"), reply_markup=rating_kb("all"))
        await cb.answer()
        return

    # Меню выбора ставки принадлежит тому, кто его открыл
    if act in ("st", "sx", "bot"):
        if user.id != int(parts[2]):
            await cb.answer("Это меню открыл другой игрок", show_alert=True)
            return
        if act == "sx":
            await _close_picker(user.id, cb.message)
            await cb.answer()
            return

    from middlewares.ratelimit import allow_lobby
    if act == "play":
        key = parts[2]
        if key != "mafia" and key not in GAMES and key not in DUELS:
            await cb.answer("Игра не найдена", show_alert=True)
            return
        if not is_enabled(key):
            await cb.answer("Эта игра отключена", show_alert=True)
            return
        if not has_stake(key):
            if not allow_lobby(user.id):
                await cb.answer("⏳ Слишком много действий. Попробуй немного позже.", show_alert=True)
                return
            err = await launch(cb.bot, cb.message.chat.id, user, key)
            if err:
                await _answer_error(cb, err)
            else:
                await cb.answer("Лобби открыто!")
            return
        err = await precheck(cb.message.chat.id, user, key)
        if err:
            await cb.answer(err, show_alert=True)
            return
        await _open_picker(cb, key)
        await cb.answer()
        return

    if act == "st":
        key, stake = parts[3], int(parts[4])
        if not has_stake(key) or not is_enabled(key) or not 0 <= stake <= MAX_STAKE:
            await cb.answer("Эта игра отключена", show_alert=True)
            return
        if not allow_lobby(user.id):
            await cb.answer("⏳ Слишком много действий. Попробуй немного позже.", show_alert=True)
            return
        err = await launch(cb.bot, cb.message.chat.id, user, key, stake)
        if err:
            if err != NEED_DM:
                await _close_picker(user.id, cb.message)
            await _answer_error(cb, err)
            return
        await _close_picker(user.id, cb.message)
        await cb.answer("Набор открыт!")
        return

    if act == "bot":
        if not is_enabled("seabattle"):
            await cb.answer("Эта игра отключена", show_alert=True)
            return
        err = await seabattle.start_vs_bot(cb.bot, cb.message.chat.id, user)
        if err:
            if err != NEED_DM:
                await _close_picker(user.id, cb.message)
            await _answer_error(cb, err)
            return
        await _close_picker(user.id, cb.message)
        await cb.answer("Партия против бота началась, смотри личку")
        return

    await cb.answer()


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
