"""Рейтинги игр: /top_games и кнопка «Рейтинг игр» в меню."""
from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database.game_stats import get_game_top
from database.settings import get_setting
from services import features
from utils.emoji import e
from utils.fmt import esc
from utils.ui import kb

router = Router()
MEDALS = ["gold", "silver", "bronze"]

RATING_GAMES = [
    ("all", "Общий", "Игрок №1"),
    ("ttt", "Крестики-нолики", "Мастер крестиков"),
    ("rps", "Камень-ножницы-бумага", "Повелитель КНБ"),
    ("duel", "Дуэль", "Король дуэли"),
    ("mafia", "Мафия", "Главарь мафии"),
    ("memory", "Мемори", "Феноменальная память"),
    ("code", "Взломай код", "Взломщик"),
    ("assoc", "Ассоциации", "Мастер ассоциаций"),
    ("bunker", "Бункер", "Хранитель бункера"),
    ("roulette", "Русская рулетка", "Везунчик"),
    ("poker", "Покер", "Покерный шулер"),
    ("wordseek", "WordSeek", "Словесный гений"),
    ("seabattle", "Морской бой", "Адмирал"),
    ("hangman", "Виселица", "Мастер виселицы"),
    ("coin", "Орёл и решка", "Счастливчик"),
]


async def top_size() -> int:
    try:
        return max(3, min(10, int(await get_setting("top_size", "5"))))
    except ValueError:
        return 5


def rating_kb(current: str):
    rows, row = [], []
    for key, title, _ in RATING_GAMES:
        if key != "all" and not features.is_on(key):
            continue
        row.append((("• " if key == current else "") + title, f"tg:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return kb(rows)


async def rating_text(key: str) -> str:
    title, champion = next((t, c) for k, t, c in RATING_GAMES if k == key)
    n = await top_size()
    rows = await get_game_top(None if key == "all" else key, n)
    text = f"{e('gold')} <b>Рейтинг: {esc(title)}</b>\n<i>Топ-{n} по победам</i>\n\n"
    if not rows:
        return text + "<i>Здесь пока никого, сыграйте первыми!</i>"
    for i, (fn, un, w, l, d) in enumerate(rows):
        name = fn or (f"@{un}" if un else "Игрок")
        total = w + l + d
        rate = round(w * 100 / total) if total else 0
        mark = e(MEDALS[i]) if i < 3 else f"{i + 1}."
        text += f"{mark} <b>{esc(name)}</b> — {w} п. / {l} пор. <i>({rate}%)</i>\n"
    fn, un = rows[0][0], rows[0][1]
    text += f"\n{e('crown')} <b>{esc(champion)}</b>: {esc(fn or (f'@{un}' if un else 'Игрок'))}"
    return text


@router.message(Command("top_games"))
async def cmd_top_games(message: Message):
    await message.answer(await rating_text("all"), reply_markup=rating_kb("all"))


@router.callback_query(F.data.startswith("tg:"))
async def top_games_cb(cb: CallbackQuery):
    key = cb.data.split(":")[1]
    if any(k == key for k, _, _ in RATING_GAMES):
        try:
            await cb.message.edit_text(await rating_text(key), reply_markup=rating_kb(key))
        except Exception:
            pass
    await cb.answer()
