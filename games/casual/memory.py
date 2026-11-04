import asyncio
import random
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from games import ai_content as ai_games, pause
from games.core import finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from services import ai
from utils.fmt import esc
from utils.premium import raw

router = Router()
KEY = "memory"
POOL = list("🍎🚀🐱⭐🎲🍕🎈🔥🌙🍀🎵🐶🍩⚽🎁🦋🍉🚗🎸🍓")
ROUNDS = 5
ANSWER_SEC = 25
PER_ROW = 5


def keyboard(r: int) -> InlineKeyboardMarkup:
    """Кнопки с эмодзи: ответ набирается нажатиями, печатать (и иметь Premium) не нужно."""
    rows = [[InlineKeyboardButton(text=POOL[i], callback_data=f"mem:{r}:{i}")
             for i in range(start, min(start + PER_ROW, len(POOL)))]
            for start in range(0, len(POOL), PER_ROW)]
    rows.append([InlineKeyboardButton(text="⌫ Стереть", callback_data=f"mem:{r}:x")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_game(lobby: Lobby):
    lobby.data.update(scores={u: 0 for u in lobby.players}, phase="idle", answers={}, progress={},
                      seq=[], round=0, event=asyncio.Event(), msg=None)
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    try:
        for r in range(1, ROUNDS + 1):
            n = r + 2
            seq = random.sample(POOL, n)
            d.update(seq=seq, answers={}, progress={}, round=r, phase="show")
            d["event"].clear()
            show = 2 + n * 0.9
            msg = await bot.send_message(
                lobby.chat_id, raw(f"🎮 <b>Мемори: раунд {r}/{ROUNDS}</b>\n\n"
                                   f"{'  '.join(seq)}\n\n<i>Запоминай! Скрою через {int(show)} сек.</i>"))
            d["msg"] = msg
            await pause.sleep(show)
            d["phase"] = "answer"
            await _edit_status(lobby, r)
            try:
                await pause.wait_for(d["event"], ANSWER_SEC)
            except asyncio.TimeoutError:
                pass
            d["phase"] = "idle"
            try:
                await d["msg"].edit_reply_markup(reply_markup=None)
            except Exception:
                pass
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
                lobby.chat_id, raw(f"<b>Раунд {r}: правильный ответ</b>\n{'  '.join(seq)}\n\n" + "\n".join(lines)))
            await pause.sleep(2)
        await end(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        finish(lobby)


async def _edit_status(lobby: Lobby, r: int):
    d = lobby.data
    try:
        await d["msg"].edit_text(
            raw(f"🎮 <b>Мемори: раунд {r}/{ROUNDS}</b>\n\n<i>Последовательность скрыта. "
                f"Нажимай эмодзи кнопками в том же порядке ({ANSWER_SEC} сек.). "
                f"Ответили: {len(d['answers'])}/{len(lobby.players)}</i>"),
            reply_markup=keyboard(r))
    except Exception:
        pass


async def end(lobby: Lobby):
    d = lobby.data
    ranking = sorted(d["scores"].items(), key=lambda kv: -kv[1])
    top = ranking[0][1] if ranking else 0
    winners = [u for u, s in ranking if s == top and top > 0]
    text = "🎉 <b>Мемори: итоги</b>\n\n"
    medals = ["🥇", "🥈", "🥉"]
    for i, (u, s) in enumerate(ranking):
        icon = medals[i] if i < 3 else "▫️"
        text += f"{icon} <b>{esc(lobby.players[u])}</b>: {s}\n"
    if await ai.enabled():
        comment = await ai_games.memory_comment([s for _, s in ranking])
        if comment:
            text += f"\n<i>{esc(comment)}</i>"
    await lobby.bot.send_message(lobby.chat_id, raw(text))
    await record_results(KEY, lobby.players, winners)


@router.callback_query(F.data.startswith("mem:"))
async def memory_tap(cb: CallbackQuery):
    _, r, act = cb.data.split(":")
    lobby = lobbies.get(cb.message.chat.id) if cb.message else None
    if not lobby or not lobby.started or lobby.game.key != KEY:
        await cb.answer("Игра уже закончилась", show_alert=True)
        return
    d = lobby.data
    uid = cb.from_user.id
    if uid not in lobby.players:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if d.get("phase") != "answer" or int(r) != d.get("round"):
        await cb.answer("Раунд уже закончился")
        return
    if uid in d["answers"]:
        await cb.answer("Ответ уже принят")
        return
    n = len(d["seq"])
    cur = d["progress"].setdefault(uid, [])
    if act == "x":
        if cur:
            cur.pop()
    else:
        cur.append(POOL[int(act)])
    if len(cur) < n:
        await cb.answer(f"{' '.join(cur) or '—'}  ({len(cur)}/{n})")
        return
    d["answers"][uid] = (list(cur), time.monotonic())
    await cb.answer(f"Ответ принят: {' '.join(cur)}")
    if len(d["answers"]) >= len(lobby.players):
        d["event"].set()
    else:
        await _edit_status(lobby, d["round"])


@router.message(Command("memory"))
async def cmd_memory(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, MEMORY)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


MEMORY = GameDef(
    key=KEY, title="Мемори", emoji="game", command="memory",
    description="Запомни последовательность эмодзи и повтори её кнопками. 5 раундов, с каждым длиннее.",
    min_players=1, max_players=20, start=start_game)
register(MEMORY)
