import asyncio
import logging
import random
import time

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from games import ai_content as ai_games, pause
from games.core import finish, GameDef, InGame, lobbies, Lobby, open_lobby, record_results, register
from services import ai
from utils.emoji import e
from utils.fmt import esc

router = Router()
log = logging.getLogger(__name__)
KEY = "memory"
POOL = list("🍎🚀🐱⭐🎲🍕🎈🔥🌙🍀🎵🐶🍩⚽🎁🦋🍉🚗🎸🍓")
POOL_SET = set(POOL)
ROUNDS = 5
ANSWER_SEC = 25


def extract(text: str):
    return [ch for ch in text if ch in POOL_SET]


async def start_game(lobby: Lobby):
    lobby.data.update(scores={u: 0 for u in lobby.players}, phase="idle", answers={},
                      seq=[], event=asyncio.Event(), msg=None)
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    try:
        for r in range(1, ROUNDS + 1):
            n = r + 2
            seq = random.sample(POOL, n)
            d.update(seq=seq, answers={}, phase="show")
            d["event"].clear()
            show = 2 + n * 0.9
            msg = await bot.send_message(
                lobby.chat_id, f"{e('game')} <b>Мемори: раунд {r}/{ROUNDS}</b>\n\n"
                               f"{'  '.join(seq)}\n\n<i>Запоминай! Скрою через {int(show)} сек.</i>")
            d["msg"] = msg
            await pause.sleep(show)
            d["phase"] = "answer"
            await _edit_status(lobby, r)
            try:
                await pause.wait_for(d["event"], ANSWER_SEC)
            except asyncio.TimeoutError:
                pass
            d["phase"] = "idle"
            answers = d["answers"]
            good = sorted(((t, u) for u, (ex, t) in answers.items() if ex == seq))
            lines = []
            for i, (t, u) in enumerate(good):
                pts = 15 if i == 0 else 10
                d["scores"][u] += pts
                lines.append(f"✅ {esc(lobby.players[u])} +{pts}")
            for u in lobby.players:
                if u in answers and answers[u][0] != seq:
                    lines.append(f"❌ {esc(lobby.players[u])}")
                elif u not in answers:
                    lines.append(f"⌛ {esc(lobby.players[u])}: не ответил(а)")
            await bot.send_message(
                lobby.chat_id, f"<b>Раунд {r}: правильный ответ</b>\n{'  '.join(seq)}\n\n" + "\n".join(lines))
            await pause.sleep(2)
        await end(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в Мемори")
    finally:
        finish(lobby)


async def _edit_status(lobby: Lobby, r: int):
    d = lobby.data
    try:
        await d["msg"].edit_text(
            f"{e('game')} <b>Мемори: раунд {r}/{ROUNDS}</b>\n\n<i>Последовательность скрыта. "
            f"Отправь её эмодзи подряд ({ANSWER_SEC} сек.). Ответили: {len(d['answers'])}/{len(lobby.players)}</i>")
    except Exception:
        pass


async def end(lobby: Lobby):
    d = lobby.data
    ranking = sorted(d["scores"].items(), key=lambda kv: -kv[1])
    top = ranking[0][1] if ranking else 0
    winners = [u for u, s in ranking if s == top and top > 0]
    text = f"{e('party')} <b>Мемори: итоги</b>\n\n"
    medals = ["gold", "silver", "bronze"]
    for i, (u, s) in enumerate(ranking):
        icon = e(medals[i]) if i < 3 else "▫️"
        text += f"{icon} <b>{esc(lobby.players[u])}</b>: {s}\n"
    if await ai.enabled():
        comment = await ai_games.memory_comment([s for _, s in ranking])
        if comment:
            text += f"\n<i>{esc(comment)}</i>"
    await lobby.bot.send_message(lobby.chat_id, text)
    await record_results(KEY, lobby.players, winners)


@router.message(InGame(KEY))
async def memory_answer(message: Message):
    lobby = lobbies.get(message.chat.id)
    d = lobby.data
    if d.get("phase") != "answer":
        return
    seq = extract(message.text)
    uid = message.from_user.id
    if not seq or uid in d["answers"]:
        return
    d["answers"][uid] = (seq, time.monotonic())
    try:
        await message.delete()
    except Exception:
        pass
    if len(d["answers"]) >= len(lobby.players):
        d["event"].set()
    else:
        await _edit_status(lobby, len(d["seq"]) - 2)


@router.message(Command("memory"))
async def cmd_memory(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, MEMORY)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


MEMORY = GameDef(
    key=KEY, title="Мемори", emoji="game", command="memory",
    description="Запомни последовательность эмодзи и отправь её по памяти. 5 раундов, с каждым длиннее.",
    min_players=1, max_players=20, start=start_game)
register(MEMORY)
