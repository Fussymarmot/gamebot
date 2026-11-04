import asyncio
import logging
from collections import Counter

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from games import ai_content as ai_games, pause
from games.content import pick, who_most_questions
from games.core import finish, GameDef, lobbies, Lobby, open_lobby, register
from services import ai
from utils.emoji import e
from utils.fmt import esc
from utils.ui import btn, name_rows

router = Router()
log = logging.getLogger(__name__)
KEY = "whomost"
ROUNDS = 5
VOTE_SEC = 40


def vote_kb(lobby: Lobby) -> InlineKeyboardMarkup:
    rows = name_rows(lobby.players.items(), lambda uid: f"wm:{lobby.gid}:{uid}")
    rows.append([btn("⏭ Пропустить", f"wm:{lobby.gid}:0", icon=False)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def start_game(lobby: Lobby):
    lobby.data.update(votes={}, event=asyncio.Event(), msg=None, titles=Counter(), open=False)
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    try:
        questions = await ai_games.who_most(ROUNDS) if await ai.enabled() else []
        if len(questions) < ROUNDS:
            questions += pick(who_most_questions(), ROUNDS - len(questions))
        for i, (question, title) in enumerate(questions, 1):
            d["votes"] = {}
            d["event"].clear()
            d["open"] = True
            d["msg"] = await bot.send_message(
                lobby.chat_id, f"{e('chat')} <b>Раунд {i}/{len(questions)}</b>\n\n{esc(question)}\n\n"
                               f"<i>Голосуй кнопкой. Результаты скрыты до конца. Проголосовало: 0/{len(lobby.players)}</i>",
                reply_markup=vote_kb(lobby))
            try:
                await pause.wait_for(d["event"], VOTE_SEC)
            except asyncio.TimeoutError:
                pass
            d["open"] = False
            tally = Counter(t for t in d["votes"].values() if t)
            if tally:
                top = max(tally.values())
                winners = [u for u, c in tally.items() if c == top]
                for u in winners:
                    d["titles"][u] += 1
                lines = "\n".join(f"• {esc(lobby.players[u])}: {c}" for u, c in tally.most_common())
                names = ", ".join(f"<b>{esc(lobby.players[u])}</b>" for u in winners)
                text = (f"{e('chat')} <b>Раунд {i}: итоги</b>\n\n{esc(question)}\n\n{lines}\n\n"
                        f"{e('crown')} <b>{esc(title)}</b>: {names}")
            else:
                text = f"{e('chat')} <b>Раунд {i}</b>\n\n{esc(question)}\n\n<i>Никто не проголосовал.</i>"
            try:
                await d["msg"].edit_text(text)
            except Exception:
                await bot.send_message(lobby.chat_id, text)
            await pause.sleep(2)
        if d["titles"]:
            lines = "\n".join(f"{e('crown')} <b>{esc(lobby.players[u])}</b>: {c}"
                              for u, c in d["titles"].most_common())
            await bot.send_message(lobby.chat_id, f"{e('party')} <b>Итоги игры</b>\n\nЗвания за раунды:\n{lines}")
        else:
            await bot.send_message(lobby.chat_id, f"{e('party')} <b>Игра окончена</b>")
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в «Кто скорее всего»")
    finally:
        finish(lobby)


@router.callback_query(F.data.startswith("wm:"))
async def wm_cb(cb: CallbackQuery):
    _, gid, target = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby or lobby.game.key != KEY or not lobby.data.get("open"):
        await cb.answer("Голосование закончилось", show_alert=True)
        return
    d = lobby.data
    uid = cb.from_user.id
    if uid not in lobby.players:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    d["votes"][uid] = int(target)
    await cb.answer("Голос принят" if int(target) else "Ты пропустил вопрос")
    try:
        base = d["msg"].html_text.rsplit("Проголосовало:", 1)[0]
        await d["msg"].edit_text(f"{base}Проголосовало: {len(d['votes'])}/{len(lobby.players)}</i>",
                                 reply_markup=vote_kb(lobby))
    except (TelegramBadRequest, AttributeError):
        pass
    if len(d["votes"]) >= len(lobby.players):
        d["event"].set()


@router.message(Command("whomost"))
async def cmd_whomost(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, WHOMOST)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


WHOMOST = GameDef(
    key=KEY, title="Кто скорее всего…", emoji="chat", command="whomost",
    description="Шуточные вопросы про участников чата. Голосуйте за друзей, а результат покажем в конце раунда.",
    min_players=3, max_players=20, start=start_game)
register(WHOMOST)
