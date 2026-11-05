import asyncio
import random
import re
from collections import Counter

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.settings import get_setting
from games import ai_content as ai_games, pause
from games.content import assoc_words
from games.core import finish, GameDef, InGame, lobbies, Lobby, open_lobby, record_results, register
from services import ai
from utils.emoji import e
from utils.fmt import esc
from utils.logs import logger

router = Router()
KEY = "assoc"
MAX_WORDS = 60
WORD_RE = re.compile(r"[а-яёa-z][а-яёa-z\-]{1,24}")


def norm(w: str) -> str:
    return w.strip().lower().replace("ё", "е")


def stem(w: str) -> str:
    return w[:5] if len(w) >= 6 else w


async def start_game(lobby: Lobby):
    sec = int(await get_setting("assoc_sec", "15"))
    lives = int(await get_setting("assoc_lives", "2"))
    start = (await ai_games.assoc_start() if await ai.enabled() else None) or random.choice(assoc_words())
    order = list(lobby.players)
    random.shuffle(order)
    lobby.data.update(order=order, strikes=Counter(), used={stem(norm(start))}, last=start, sec=sec,
                      lives=lives, expect=None, word=None, event=asyncio.Event(), hinted=False, count=0)
    await lobby.bot.send_message(
        lobby.chat_id, f"{e('speech')} <b>Ассоциации</b>\n\nПишу слово, а вы по очереди придумываете "
                       f"ассоциацию к <b>последнему</b> слову. На ответ {sec} сек. Повторы и молчание "
                       f"дают штраф, после {lives} штрафов игрок выбывает.\n\n"
                       f"Начинаем со слова: <b>{esc(start)}</b>")
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    idx = 0
    try:
        while True:
            order = d["order"]
            idx %= len(order)
            cur = order[idx]
            d.update(expect=cur, word=None, hinted=False)
            d["event"].clear()
            await bot.send_message(
                lobby.chat_id, f"{e('hourglass')} Слово: <b>{esc(d['last'])}</b>\n"
                               f"Ход: <b>{esc(lobby.players[cur])}</b> ({d['sec']} сек.)")
            reason = None
            try:
                await pause.wait_for(d["event"], d["sec"])
                w = norm(d["word"])
                if stem(w) in d["used"]:
                    reason = f"«{esc(w)}» уже было"
                else:
                    d["used"].add(stem(w))
                    d["last"] = w
                    d["count"] += 1
            except asyncio.TimeoutError:
                reason = "время вышло"
            d["expect"] = None
            advance = True
            if reason:
                d["strikes"][cur] += 1
                left = d["lives"] - d["strikes"][cur]
                if left <= 0:
                    order.remove(cur)
                    advance = False
                    await bot.send_message(lobby.chat_id, f"💥 <b>{esc(lobby.players[cur])}</b> выбывает ({reason}).")
                else:
                    await bot.send_message(lobby.chat_id, f"⚠️ {esc(lobby.players[cur])}: {reason}. "
                                                          f"Штрафов: {d['strikes'][cur]}/{d['lives']}")
            if len(order) <= 1:
                break
            if d["count"] >= MAX_WORDS:
                break
            if advance:
                idx += 1
        await end(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Ошибка в игре «Ассоциации» в чате %s", lobby.chat_id)
    finally:
        finish(lobby)


async def end(lobby: Lobby):
    d = lobby.data
    order = d["order"]
    if len(order) == 1:
        winners = list(order)
    else:
        best = min(d["strikes"][u] for u in order)
        winners = [u for u in order if d["strikes"][u] == best]
    names = ", ".join(f"<b>{esc(lobby.players[u])}</b>" for u in winners)
    await lobby.bot.send_message(
        lobby.chat_id, f"{e('party')} <b>Ассоциации: игра окончена</b>\n\nСлов в цепочке: {d['count']}\n"
                       f"Победитель: {names}")
    await record_results(KEY, lobby.players, winners)


@router.message(InGame(KEY))
async def assoc_word(message: Message):
    lobby = lobbies.get(message.chat.id)
    d = lobby.data
    if d.get("expect") != message.from_user.id or d.get("word"):
        return
    text = norm(message.text)
    if not WORD_RE.fullmatch(text):
        if not d["hinted"]:
            d["hinted"] = True
            await message.reply("<i>Нужно одно слово (буквы и дефис).</i>")
        return
    d["word"] = text
    d["event"].set()


@router.message(Command("assoc"))
async def cmd_assoc(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, ASSOC)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


ASSOC = GameDef(
    key=KEY, title="Ассоциации", emoji="speech", command="assoc",
    description="Цепочка слов: придумывай ассоциацию к последнему слову, не повторяйся и успей вовремя.",
    min_players=3, max_players=20, start=start_game)
register(ASSOC)
