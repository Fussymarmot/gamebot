"""Виселица на двоих.

/hangman @username (или ответом на сообщение) — бот пишет загадывающему в личку и просит слово.
Когда слово пришло, в группе появляется виселица, а второй игрок угадывает буквы кнопками.
"""
import asyncio
import re
import secrets
import time
from typing import Dict, Optional

from aiogram import F, Router
from aiogram.filters import Command, Filter
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import GROUP_ID
from database.game_stats import add_game_result
from database.moderation import is_user_banned
from games import pause
from games.core import can_dm
from games.invites import acquire, is_busy, release
from utils import richui
from utils.fmt import code, esc
from utils.targets import parse_target
from utils.titles import BAD

router = Router()
KEY = "hangman"
MAX_MISTAKES = 6
WORD_MIN, WORD_MAX = 4, 16
WORD_WAIT_SEC = 180
IDLE_SEC = 600
ALPHABET = "абвгдежзийклмнопрстуфхцчшщъыьэюя"
PER_ROW = 8

games: Dict[str, "Hangman"] = {}
locks: Dict[str, asyncio.Lock] = {}
waiting: Dict[int, str] = {}  # id загадывающего -> id партии, пока он не прислал слово


def clean_word(text: str) -> Optional[str]:
    word = (text or "").strip().lower().replace("ё", "е")
    return word if re.fullmatch(rf"[а-я]{{{WORD_MIN},{WORD_MAX}}}", word) else None


def letters_word(n: int) -> str:
    return f"{n} " + ("буква" if n % 10 == 1 and n != 11 else "буквы" if n % 10 in (2, 3, 4) and n // 10 != 1 else "букв")


def gallows(mistakes: int) -> str:
    """Виселица из палочек: с каждой ошибкой дорисовывается часть человечка."""
    m = mistakes
    head = "O" if m >= 1 else " "
    body = "|" if m >= 2 else " "
    l_arm = "/" if m >= 3 else " "
    r_arm = "\\" if m >= 4 else " "
    l_leg = "/" if m >= 5 else " "
    r_leg = "\\" if m >= 6 else " "
    return "\n".join([
        "  +---+",
        "  |   |",
        f"  {head}   |",
        f" {l_arm}{body}{r_arm}  |",
        f" {l_leg} {r_leg}  |",
        "      |",
        "=========",
    ])


class Hangman:
    def __init__(self, chat_id: int, setter: int, setter_name: str, guesser: int, guesser_name: str):
        self.chat_id = chat_id
        self.setter, self.sn = setter, setter_name
        self.guesser, self.gn = guesser, guesser_name
        self.word = ""
        self.phase = "word"  # word -> play -> over
        self.guessed: set = set()
        self.wrong: list = []
        self.mid: Optional[int] = None
        self.dm_mid: Optional[int] = None
        self.note = ""
        self.last = time.monotonic()

    def masked(self, reveal: bool = False) -> str:
        if not self.word:
            return ""
        return " ".join(c.upper() if (reveal or c in self.guessed) else "_" for c in self.word)

    def solved(self) -> bool:
        return bool(self.word) and all(c in self.guessed for c in self.word)

    def header(self) -> str:
        return f"Загадывает: <b>{esc(self.sn)}</b>\nОтгадывает: <b>{esc(self.gn)}</b>"

    def status(self) -> str:
        if self.phase == "word":
            return f"<i>Ждём слово от {esc(self.sn)} в личке с ботом…</i>"
        if self.phase == "over":
            return self.note
        return f"<i>Буквы выбирает {esc(self.gn)}</i>"

    def text(self) -> str:
        t = f"🪢 <b>Виселица</b>\n\n{self.header()}\n\n"
        if self.phase == "word":
            return t + self.status()
        over = self.phase == "over"
        t += f"<pre>{esc(gallows(len(self.wrong)))}</pre>\n"
        t += f"<b>{self.masked(reveal=over)}</b>  <i>({letters_word(len(self.word))})</i>\n\n"
        t += f"Ошибки ({len(self.wrong)}/{MAX_MISTAKES}): " + (", ".join(c.upper() for c in self.wrong) or "—")
        return t + "\n\n" + self.status()

    def used(self, ch: str) -> bool:
        return ch in self.guessed or ch in self.wrong

    def kb(self, gid: str) -> Optional[InlineKeyboardMarkup]:
        if self.phase != "play":
            return None
        rows, row = [], []
        for ch in ALPHABET:
            if self.used(ch):
                row.append(InlineKeyboardButton(text="•", callback_data=f"hg:{gid}:-"))
            else:
                row.append(InlineKeyboardButton(text=ch.upper(), callback_data=f"hg:{gid}:{ch}"))
            if len(row) == PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        rows.append([InlineKeyboardButton(text="🏳 Сдаться", callback_data=f"hg:{gid}:!")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    def rich_html(self, gid: str) -> str:
        over = self.phase == "over"
        out = f"<h3>Виселица</h3><p>{self.header().replace(chr(10), '<br>')}</p>"
        if self.phase == "word":
            return out + f"<p><i>Ждём слово от {richui.esc(self.sn)} в личке с ботом…</i></p>"
        out += f"<pre>{richui.esc(gallows(len(self.wrong)))}</pre>"
        out += f"<p><b>{richui.esc(self.masked(reveal=over))}</b> <i>({letters_word(len(self.word))})</i></p>"
        out += (f"<p>Ошибки ({len(self.wrong)}/{MAX_MISTAKES}): "
                + richui.esc(", ".join(c.upper() for c in self.wrong) or "—") + "</p>")
        if over:
            return out + f"<p>{self.note}</p>"
        row, rows = [], []
        for ch in ALPHABET:
            row.append(richui.btn("•", f"hg:{gid}:-") if self.used(ch) else richui.btn(ch.upper(), f"hg:{gid}:{ch}"))
            if len(row) == PER_ROW:
                rows.append(richui.row(row))
                row = []
        if row:
            rows.append(richui.row(row))
        rows.append(richui.row([richui.btn("🏳 Сдаться", f"hg:{gid}:!", "danger")]))
        return out + f"<p><i>Буквы выбирает {richui.esc(self.gn)}</i></p>" + "".join(rows)


async def render(bot, gid: str, g: Hangman):
    g.mid, _ = await richui.publish(bot, g.chat_id, g.mid, g.rich_html(gid), g.text(), g.kb(gid))


def drop(gid: str) -> Optional[Hangman]:
    g = games.pop(gid, None)
    locks.pop(gid, None)
    if g:
        waiting.pop(g.setter, None)
        release(g.setter, g.guesser)
    return g


async def _announce_end(bot, gid: str, g: Hangman, note: str):
    g.phase = "over"
    g.note = note
    await render(bot, gid, g)


async def finish(bot, gid: str, g: Hangman, guesser_won: bool, note: str):
    drop(gid)
    await _announce_end(bot, gid, g, note)
    winner, loser = (g.guesser, g.setter) if guesser_won else (g.setter, g.guesser)
    await add_game_result(winner, KEY, "win")
    await add_game_result(loser, KEY, "loss")


async def watch(bot, gid: str):
    while gid in games:
        await asyncio.sleep(15)
        g = games.get(gid)
        if not g:
            return
        idle = time.monotonic() - g.last
        if g.phase == "word" and idle > WORD_WAIT_SEC:
            drop(gid)
            await _announce_end(bot, gid, g, "<i>Слово так и не прислали, игра отменена.</i>")
            if g.dm_mid:
                try:
                    await bot.edit_message_text("Время вышло, игра отменена.", chat_id=g.setter,
                                                message_id=g.dm_mid)
                except Exception:
                    pass
            return
        if g.phase == "play" and idle > IDLE_SEC:
            drop(gid)
            await _announce_end(bot, gid, g, f"<i>Игра закрыта: 10 минут без хода.</i>\nСлово было: "
                                             f"<b>{esc(g.word.upper())}</b>")
            return


@router.message(Command("hangman"))
async def cmd_hangman(message: Message):
    me = message.from_user
    if message.chat.id != GROUP_ID:
        await message.answer("Виселица запускается в группе.")
        return
    if pause.is_paused():
        await message.answer("⏸ Сейчас игры на паузе.")
        return
    if await is_user_banned(me.id):
        await message.answer("❌ Ты в чёрном списке игр")
        return
    if is_busy(me.id):
        await message.answer("⚠️ Ты уже участвуешь в игре")
        return
    t = await parse_target(message)
    if not t:
        await message.answer(f"Используй: {code('/hangman @username')} или {code('/hangman 123456789')}\n"
                             "<i>или ответь командой на сообщение игрока, который будет угадывать</i>")
        return
    if t.id is None:
        await message.answer("❌ Не знаю такого игрока. Ответь командой на его сообщение или укажи ID.")
        return
    if t.id == me.id:
        await message.answer("❌ Нельзя играть с самим собой")
        return
    if t.id == message.bot.id:
        await message.answer("❌ Бот не играет в виселицу")
        return
    if await is_user_banned(t.id):
        await message.answer("❌ Этот пользователь в чёрном списке игр")
        return
    if is_busy(t.id) or not acquire(me.id, t.id, KEY):
        await message.answer("⚠️ Этот игрок уже участвует в игре")
        return
    if not await can_dm(message.bot, me.id):
        release(me.id, t.id)
        bot_me = await message.bot.me()
        await message.answer(f"❌ Не могу написать тебе в личку. Открой @{bot_me.username}, "
                             "нажми Start и повтори команду.")
        return

    gid = secrets.token_hex(3)
    g = Hangman(message.chat.id, me.id, me.first_name or me.username or "Игрок", t.id, t.name)
    games[gid] = g
    locks[gid] = asyncio.Lock()
    waiting[me.id] = gid
    try:
        sent = await message.bot.send_message(
            me.id, f"🪢 <b>Виселица</b>\n\nПришли слово для <b>{esc(g.gn)}</b>: одно существительное, "
                   f"русскими буквами, от {WORD_MIN} до {WORD_MAX} букв.\n<i>Ждём {WORD_WAIT_SEC // 60} мин.</i>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚫 Отмена", callback_data=f"hgx:{gid}")]]))
        g.dm_mid = sent.message_id
    except Exception:
        drop(gid)
        await message.answer("❌ Не получилось написать тебе в личку, игра отменена.")
        return
    await render(message.bot, gid, g)
    asyncio.create_task(watch(message.bot, gid))


class WaitingForWord(Filter):
    async def __call__(self, message: Message) -> bool:
        return bool(message.chat.type == "private" and message.from_user and message.text
                    and not message.text.startswith("/") and message.from_user.id in waiting)


@router.message(WaitingForWord())
async def got_word(message: Message):
    gid = waiting.get(message.from_user.id)
    g = games.get(gid) if gid else None
    if not g or g.phase != "word":
        return
    word = clean_word(message.text)
    if not word:
        await message.answer(f"❌ Нужно одно слово русскими буквами, от {WORD_MIN} до {WORD_MAX} букв.")
        return
    if BAD.search(word):
        await message.answer("❌ Такое слово нельзя, придумай другое.")
        return
    async with locks[gid]:
        if g.phase != "word":
            return
        g.word = word
        g.phase = "play"
        g.last = time.monotonic()
        waiting.pop(g.setter, None)
    await message.answer(f"✅ Слово <b>{esc(word.upper())}</b> принято! Игра идёт в группе.")
    if g.dm_mid:
        try:
            await message.bot.edit_message_reply_markup(chat_id=g.setter, message_id=g.dm_mid, reply_markup=None)
        except Exception:
            pass
    await render(message.bot, gid, g)


@router.callback_query(F.data.startswith("hgx:"))
async def cancel_cb(cb: CallbackQuery):
    gid = cb.data.split(":")[1]
    g = games.get(gid)
    if not g or g.phase != "word" or cb.from_user.id != g.setter:
        await cb.answer("Игра уже не ждёт слово", show_alert=True)
        return
    drop(gid)
    await _announce_end(cb.bot, gid, g, f"<i>{esc(g.sn)} отменил(а) игру.</i>")
    try:
        await cb.message.edit_text("Игра отменена.")
    except Exception:
        pass
    await cb.answer()


@router.callback_query(F.data.startswith("hg:"))
async def guess_cb(cb: CallbackQuery):
    _, gid, ch = cb.data.split(":")
    g = games.get(gid)
    if not g or g.phase != "play":
        await cb.answer("Игра завершена", show_alert=True)
        return
    uid = cb.from_user.id
    if uid != g.guesser:
        who = "Угадывает только " + g.gn if uid != g.setter else "Ты загадал слово, теперь смотри и жди"
        await cb.answer(who, show_alert=True)
        return
    if ch == "-":
        await cb.answer("Эта буква уже была")
        return
    async with locks[gid]:
        if gid not in games or g.phase != "play":
            await cb.answer("Игра завершена", show_alert=True)
            return
        g.last = time.monotonic()
        if ch == "!":
            await finish(cb.bot, gid, g, False,
                         f"🏳 <b>{esc(g.gn)}</b> сдался(ась). Слово было: <b>{esc(g.word.upper())}</b>\n"
                         f"Победил <b>{esc(g.sn)}</b>!")
            await cb.answer()
            return
        if ch not in ALPHABET or g.used(ch):
            await cb.answer("Эта буква уже была")
            return
        if ch in g.word:
            g.guessed.add(ch)
            if g.solved():
                await finish(cb.bot, gid, g, True,
                             f"🎉 <b>{esc(g.gn)}</b> отгадал(а) слово <b>{esc(g.word.upper())}</b>!")
                await cb.answer("Победа!")
                return
            await cb.answer("Есть такая буква!")
        else:
            g.wrong.append(ch)
            if len(g.wrong) >= MAX_MISTAKES:
                await finish(cb.bot, gid, g, False,
                             f"💀 <b>{esc(g.gn)}</b> повешен(а). Слово было: <b>{esc(g.word.upper())}</b>\n"
                             f"Победил <b>{esc(g.sn)}</b>!")
                await cb.answer("Это была последняя ошибка")
                return
            await cb.answer("Нет такой буквы")
        await render(cb.bot, gid, g)
