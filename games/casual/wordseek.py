import asyncio
import random
import re
from collections import Counter

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message

from database.game_stats import add_game_result
from database.moderation import is_user_banned
from database.settings import get_setting, set_setting
from games import ai_content as ai_games, pause
from games.content import wordseek_words
from games.core import finish, GameDef, InGame, lobbies, Lobby, open_lobby, record_results, register
from services import ai
from utils.fmt import esc
from utils.logs import logger
from utils.timeutils import now

router = Router()
KEY = "wordseek"
ATTEMPTS = 6
TIME_LIMIT = 300
WORD_RE = re.compile(r"[а-я]{5}")
solo = {}


def norm(w: str) -> str:
    return (w or "").strip().lower().replace("ё", "е")


_words_cache: dict = {"src": None, "set": frozenset(), "sorted": []}


def local_words():
    """Множество 5-буквенных слов. Пересобирается только когда сменился список из data/wordseek_words.json
    (раньше строилось заново на каждую догадку каждого игрока)."""
    src = wordseek_words()
    if _words_cache["src"] is not src:
        words = {norm(w) for w in src if len(norm(w)) == 5}
        _words_cache.update(src=src, set=frozenset(words), sorted=sorted(words))
    return _words_cache["set"]


def evaluate(secret: str, guess: str) -> str:
    res = ["⬜"] * 5
    left = Counter(s for s, g in zip(secret, guess) if s != g)
    for i, (s, g) in enumerate(zip(secret, guess)):
        if s == g:
            res[i] = "🟩"
    for i, g in enumerate(guess):
        if res[i] != "🟩" and left[g] > 0:
            res[i] = "🟨"
            left[g] -= 1
    return "".join(res)


async def is_word(w: str) -> bool:
    if w in local_words():
        return True
    known = await ai_games.word_known(w)
    return True if known is None else known


async def pick_secret(daily: bool) -> str:
    local_words()
    words = _words_cache["sorted"]
    if daily:
        return random.Random(now().strftime("%Y-%m-%d")).choice(words)
    if await ai.enabled() and random.random() < 0.5:
        word = await ai_games.secret_word()
        if word:
            return word
    return random.choice(words)


async def start_game(lobby: Lobby):
    secret = await pick_secret(False)
    lobby.data.update(secret=secret, tries={u: [] for u in lobby.players}, winner=None, hinted=False,
                      event=asyncio.Event(), guesses=0, busy=set())
    await lobby.bot.send_message(
        lobby.chat_id, f"🔤 <b>WordSeek</b>\n\nЗагадано слово из <b>5 букв</b>. У каждого {ATTEMPTS} попыток, "
                       f"время: {TIME_LIMIT // 60} мин.\nПиши слова прямо в чат.\n\n"
                       "🟩 буква на своём месте\n🟨 буква есть, но в другом месте\n⬜ такой буквы нет")
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    try:
        try:
            await pause.wait_for(d["event"], TIME_LIMIT)
        except asyncio.TimeoutError:
            pass
        if d["winner"]:
            text = f"{'🏆'} <b>{esc(lobby.players[d['winner']])}</b> угадал(а) слово: <b>{d['secret'].upper()}</b>!"
        else:
            text = f"⌛ Никто не угадал. Слово было: <b>{d['secret'].upper()}</b>"
        await bot.send_message(lobby.chat_id, text)
        await record_results(KEY, lobby.players, [d["winner"]] if d["winner"] else [])
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Ошибка в игре WordSeek в чате %s", lobby.chat_id)
    finally:
        finish(lobby)


@router.message(InGame(KEY))
async def group_guess(message: Message):
    lobby = lobbies.get(message.chat.id)
    d = lobby.data
    uid = message.from_user.id
    word = norm(message.text)
    if d.get("winner") or not WORD_RE.fullmatch(word) or uid in d["busy"]:
        return
    tries = d["tries"][uid]
    if len(tries) >= ATTEMPTS or word in tries:
        return
    d["busy"].add(uid)
    try:
        if not await is_word(word):
            await message.reply("<i>Такого слова нет в словаре.</i>")
            return
        tries.append(word)
        d["guesses"] += 1
        await message.reply(f"{evaluate(d['secret'], word)} <b>{word.upper()}</b> ({len(tries)}/{ATTEMPTS})")
        if word == d["secret"]:
            d["winner"] = uid
            d["event"].set()
            return
        if all(len(t) >= ATTEMPTS for t in d["tries"].values()):
            d["event"].set()
            return
        if d["guesses"] >= 6 and not d["hinted"]:
            d["hinted"] = True
            hint = await ai_games.hint_for(d["secret"]) if await ai.enabled() else None
            if hint:
                await message.answer(f"💡 <b>Подсказка:</b> {esc(hint)}")
    finally:
        d["busy"].discard(uid)


class SoloActive(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.chat.type == "private" and message.from_user and message.text
                    and not message.text.startswith("/") and message.from_user.id in solo)


def board(g) -> str:
    lines = [f"{evaluate(g['secret'], w)} <b>{w.upper()}</b>" for w in g["tries"]]
    return "🔤 <b>WordSeek: слово дня</b>\n\n" + "\n".join(lines) + f"\n\nПопыток: {len(g['tries'])}/{ATTEMPTS}"


async def solo_start(message: Message):
    uid = message.from_user.id
    today = now().strftime("%Y-%m-%d")
    if await is_user_banned(uid):
        await message.answer("⚠️ Ты в чёрном списке игр.")
        return
    if (await get_setting(f"ws_{uid}", "")) == today:
        await message.answer("🔤 Сегодняшнее слово ты уже играл(а). Новое будет завтра, а с друзьями можно "
                             "сыграть в группе: /wordseek")
        return
    solo[uid] = {"secret": await pick_secret(True), "tries": [], "hint": False}
    await message.answer("🔤 <b>WordSeek: слово дня</b>\n\nЗагадано слово из <b>5 букв</b>, 6 попыток. "
                         "Пиши слова сюда.\n🟩 на месте, 🟨 есть, но не там, ⬜ нет такой буквы.")


@router.message(SoloActive())
async def solo_guess(message: Message):
    uid = message.from_user.id
    g = solo[uid]
    word = norm(message.text)
    if not WORD_RE.fullmatch(word):
        await message.answer("<i>Нужно слово из 5 русских букв.</i>")
        return
    if word in g["tries"]:
        await message.answer("<i>Это слово уже было.</i>")
        return
    if not await is_word(word):
        await message.answer("<i>Такого слова нет в словаре.</i>")
        return
    g["tries"].append(word)
    won = word == g["secret"]
    over = won or len(g["tries"]) >= ATTEMPTS
    text = board(g)
    if won:
        text += "\n\n🏆 <b>Угадал(а)!</b>"
    elif over:
        text += f"\n\n⌛ Попытки закончились. Слово: <b>{g['secret'].upper()}</b>"
    elif len(g["tries"]) >= 3 and not g["hint"] and await ai.enabled():
        g["hint"] = True
        hint = await ai_games.hint_for(g["secret"])
        if hint:
            text += f"\n\n💡 <b>Подсказка:</b> {esc(hint)}"
    await message.answer(text)
    if over:
        solo.pop(uid, None)
        await set_setting(f"ws_{uid}", now().strftime("%Y-%m-%d"))
        await add_game_result(uid, KEY, "win" if won else "loss")


@router.message(Command("wordseek"))
async def cmd_wordseek(message: Message):
    if message.chat.type == "private":
        await solo_start(message)
        return
    err = await open_lobby(message.bot, message.chat.id, message.from_user, WORDSEEK)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


WORDSEEK = GameDef(
    key=KEY, title="WordSeek", emoji="game", command="wordseek",
    description="Угадай слово из 5 букв за 6 попыток. Подсказки: 🟩 на месте, 🟨 не на месте. Соло: /wordseek в личке.",
    min_players=1, max_players=20, start=start_game)
register(WORDSEEK)
