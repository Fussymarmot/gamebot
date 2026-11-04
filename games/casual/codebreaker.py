import asyncio
import logging
import random

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import Message

from games import pause
from games.core import finish, GameDef, InGame, lobbies, Lobby, open_lobby, record_results, register
from utils.emoji import e
from utils.fmt import code, esc

router = Router()
log = logging.getLogger(__name__)
KEY = "code"
ATTEMPTS = 8
TIME_LIMIT = 300


def bulls_cows(secret: str, guess: str):
    bulls = sum(a == b for a, b in zip(secret, guess))
    common = sum(min(secret.count(c), guess.count(c)) for c in set(guess))
    return bulls, common - bulls


async def start_game(lobby: Lobby):
    secret = "".join(random.sample("0123456789", 4))
    lobby.data.update(secret=secret, left={u: ATTEMPTS for u in lobby.players},
                      winner=None, event=asyncio.Event(), tries={u: 0 for u in lobby.players})
    await lobby.bot.send_message(
        lobby.chat_id, f"{e('key')} <b>Взломай код</b>\n\nЯ загадал код из <b>4 разных цифр</b>. "
                       f"У каждого {ATTEMPTS} попыток, время: {TIME_LIMIT // 60} мин.\n"
                       f"Отправляй числа в чат, например {code('1234')}.\n\n"
                       "🐂 <b>бык</b>: цифра на своём месте\n🐄 <b>корова</b>: цифра есть, но не на месте")
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d = lobby.data
    try:
        try:
            await pause.wait_for(d["event"], TIME_LIMIT)
        except asyncio.TimeoutError:
            pass
        w = d["winner"]
        if w:
            text = (f"{e('party')} <b>Код взломан!</b>\n\nПобедитель: <b>{esc(lobby.players[w])}</b> "
                    f"за {d['tries'][w]} поп.\nКод: {code(d['secret'])}")
            await record_results(KEY, lobby.players, [w])
        else:
            text = f"{e('stop')} <b>Код не взломан.</b>\n\nПравильный ответ: {code(d['secret'])}"
        await lobby.bot.send_message(lobby.chat_id, text)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в «Взломай код»")
    finally:
        finish(lobby)


@router.message(InGame(KEY), F.text.regexp(r"^\s*\d{4}\s*$"))
async def guess(message: Message):
    lobby = lobbies.get(message.chat.id)
    d = lobby.data
    uid = message.from_user.id
    if d["winner"] or d["left"].get(uid, 0) <= 0:
        return
    g = message.text.strip()
    d["left"][uid] -= 1
    d["tries"][uid] += 1
    bulls, cows = bulls_cows(d["secret"], g)
    if bulls == 4:
        d["winner"] = uid
        d["event"].set()
        return
    left = d["left"][uid]
    await message.reply(f"{code(g)}: 🐂 {bulls}  🐄 {cows}\n<i>Осталось попыток: {left}</i>")
    if all(v <= 0 for v in d["left"].values()):
        d["event"].set()


@router.message(Command("code"))
async def cmd_code(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, CODE)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


CODE = GameDef(
    key=KEY, title="Взломай код", emoji="key", command="code",
    description="Угадай секретный код из 4 цифр по подсказкам «быки и коровы». Побеждает первый.",
    min_players=1, max_players=10, start=start_game)
register(CODE)
