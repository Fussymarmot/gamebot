import asyncio
import logging
import random
import time
from collections import Counter

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from games import gamemute, pause
from games.party.bunker.engine import Bunker, DAYS, TAKE_LIMIT
from games.content import load
from games.core import finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from services import ai
from utils.emoji import e
from utils.fmt import esc
from utils.ui import btn, name_rows

router = Router()
log = logging.getLogger(__name__)
KEY = "bunker"
SCAVENGE_SEC = 20
DAY_SEC = 60
EVENT_VOTE_SEC = 40
COUNCIL_SEC = 30
COUNCIL_DAYS = (10, 20)

SKILL_RU = {"doctor": "врач", "cook": "повар", "engineer": "инженер", "radio": "радист", "teacher": "наставник",
            "sport": "спортсмен", "gardener": "садовод", "guard": "охранник", "music": "музыкант",
            "diplomat": "дипломат", "scout": "разведчик", "craft": "мастер", "logic": "логик",
            "funny": "шутник", "leader": "лидер"}
KIT_RU = {"health": "лекарства", "tools": "инструменты", "signal": "связь", "protect": "защита",
          "mood": "развлечения", "water": "вода", "food": "еда"}


def need_label(g: Bunker, need):
    if not need:
        return ""
    kind, name = need.split(":")
    title = SKILL_RU.get(name, name) if kind == "s" else KIT_RU.get(name, name)
    return f" <i>(нужен: {title} {'✅' if g.has(need) else '❌'})</i>"


def bar(value: float) -> str:
    n = max(0, min(10, round(value)))
    return "█" * n + "░" * (10 - n)


def status(g: Bunker) -> str:
    alive = len(g.alive())
    days = g.supply_days()
    return (f"🍖 Еда: <b>{g.food:.0f}</b>   💧 Вода: <b>{g.water:.0f}</b>   <i>(хватит примерно на {days:.0f} дн.)</i>\n"
            f"❤️ Здоровье: {bar(g.hp)} {max(0, g.hp):.0f}/10\n🎭 Настроение: {bar(g.morale)} {g.morale:.0f}/10\n"
            f"👥 В бункере: <b>{alive}</b>")


def day_header(g: Bunker) -> str:
    return f"🛡 <b>Бункер: день {g.day}/{DAYS}</b>\n\n{status(g)}"


async def edit_main(lobby: Lobby, text: str, markup=None):
    d = lobby.data
    try:
        await d["main"].edit_text(text, reply_markup=markup)
    except TelegramBadRequest as ex:
        if "not modified" in str(ex):
            return
        d["main"] = await lobby.bot.send_message(lobby.chat_id, text, reply_markup=markup)


def offer_markup(lobby: Lobby, g: Bunker, uid: int) -> InlineKeyboardMarkup:
    p = g.players[uid]
    rows, row = [], []
    for idx in g.offers[uid]:
        it = g.items[idx]
        mark = "✅ " if idx in p.items else ""
        hint = f" +{it['v']}" if it["cat"] in ("food", "water") else ""
        row.append(btn(f"{mark}{it['name']}{hint}", f"bk:t:{lobby.gid}:{idx}", icon=False))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def offer_text(g: Bunker, uid: int) -> str:
    p = g.players[uid]
    return (f"🏚 <b>Сбор вещей: {SCAVENGE_SEC} секунд!</b>\nВыбери до {TAKE_LIMIT} предметов, они уйдут в бункер.\n"
            f"<i>Нажми ещё раз, чтобы положить обратно. Взято: {len(p.items)}/{TAKE_LIMIT}</i>")


def card_text(g: Bunker, uid: int) -> str:
    p = g.players[uid]
    taken = ", ".join(esc(g.items[i]["name"]) for i in p.items) or "ничего"
    return (f"🛡 <b>Твоя карточка</b>\n\nПрофессия: <b>{esc(p.prof['name'])}</b>\nХобби: <b>{esc(p.hobby['name'])}</b>\n"
            f"Багаж: <b>{esc(p.bag['name'])}</b>\nФакт: <b>{esc(p.fact['name'])}</b>\n\nТы взял в бункер: {taken}\n\n"
            "<i>Карточки открываются в игре по одной. Твои навыки помогают в событиях.</i>")


async def start_game(lobby: Lobby):
    data = load("bunker.json", None)
    g = Bunker(lobby.names(), data)
    lobby.data.update(g=g, main=None, panels={}, event=asyncio.Event(), votes={}, ev=None, phase="scavenge",
                      council=False, events_log=[])
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, g, bot = lobby.data, lobby.data["g"], lobby.bot
    try:
        g.deal_offers()
        for uid in g.players:
            d["panels"][uid] = await bot.send_message(uid, offer_text(g, uid), reply_markup=offer_markup(lobby, g, uid))
        d["main"] = await bot.send_message(
            lobby.chat_id, f"🏚 <b>Бункер: 60 секунд</b>\n\nВ группе {len(g.players)} игроков. Катастрофа! "
                           f"Все хватают вещи в личке, у вас {SCAVENGE_SEC} секунд.")
        left = SCAVENGE_SEC
        while left > 0:
            step = 5 if left > 5 else left
            await pause.sleep(step)
            left -= step
            if left > 0:
                await edit_main(lobby, f"🏚 <b>Бункер: 60 секунд</b>\n\nСбор вещей, осталось <b>{left} сек.</b>")
        d["phase"] = "day"
        g.finish_scavenge()
        for uid, msg in d["panels"].items():
            try:
                await msg.edit_text(card_text(g, uid))
            except Exception:
                pass
        await edit_main(lobby, f"🛡 <b>Двери бункера закрыты!</b>\n\n{status(g)}\n\n<i>Впереди {DAYS} дней, "
                               f"каждый длится {DAY_SEC} секунд.</i>")
        await pause.sleep(6)
        for day in range(1, DAYS + 1):
            g.day = day
            started = time.monotonic()
            if day % 2 == 0:
                await event_day(lobby)
            else:
                await quiet_day(lobby)
            if day in COUNCIL_DAYS and g.supply_days() < 6 and len(g.alive()) > 3:
                await council(lobby)
            await pause.sleep(max(0.0, DAY_SEC - (time.monotonic() - started)))
            notes = g.end_day()
            if notes:
                await edit_main(lobby, day_header(g) + "\n\n" + "\n".join(f"⚠️ {esc(n)}" for n in notes))
                await pause.sleep(3)
            if g.over():
                break
        await end(lobby)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в игре «Бункер»")
        try:
            await bot.send_message(lobby.chat_id, "⚠️ В игре произошла ошибка, игра остановлена.")
        except Exception:
            pass
    finally:
        finish(lobby)


async def quiet_day(lobby: Lobby):
    g = lobby.data["g"]
    text = day_header(g)
    r = g.reveal()
    if r:
        p, label, value = r
        text += f"\n\n🃏 <b>{esc(p.name)}</b> открывает карточку.\n{label}: <b>{esc(value)}</b>"
    else:
        text += "\n\n<i>Спокойный день. Все карточки уже открыты.</i>"
    await edit_main(lobby, text)


def option_markup(lobby: Lobby, ev: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn(f"{i + 1}. {opt['label']}", f"bk:o:{lobby.gid}:{i}", icon=False)] for i, opt in enumerate(ev["options"])])


async def event_day(lobby: Lobby):
    d, g = lobby.data, lobby.data["g"]
    ev = g.next_event()
    d.update(ev=ev, votes={}, phase="event")
    d["event"].clear()
    alive = len(g.alive())

    def view(votes: int) -> str:
        lines = "\n".join(f"{i + 1}. {esc(o['label'])}{need_label(g, o['need'])}" for i, o in enumerate(ev["options"]))
        return (f"{day_header(g)}\n\n⚡ <b>{esc(ev['title'])}</b>\n{esc(ev['text'])}\n\n{lines}\n\n"
                f"<i>Голосуйте кнопками. Проголосовало: {votes}/{alive}</i>")

    d["view"] = view
    await edit_main(lobby, view(0), option_markup(lobby, ev))
    try:
        await pause.wait_for(d["event"], EVENT_VOTE_SEC)
    except asyncio.TimeoutError:
        pass
    d["phase"] = "day"
    tally = Counter(d["votes"].values())
    if tally:
        top = max(tally.values())
        idx = random.choice([i for i, c in tally.items() if c == top])
    else:
        idx = random.randrange(len(ev["options"]))
    opt = ev["options"][idx]
    ok, result = g.resolve(ev, idx)
    d["events_log"].append(f"{ev['title']}: {'удача' if ok else 'неудача'}")
    flavor = await ai.ask(f"Бункер, день {g.day}. Событие: «{ev['title']}». Команда выбрала: «{opt['label']}». "
                          f"Исход: {'удача' if ok else 'неудача'}. Запасы: еда {g.food:.0f}, вода {g.water:.0f}. "
                          "Опиши последствия шутливо.", timeout=6.0)
    text = (f"{day_header(g)}\n\n⚡ <b>{esc(ev['title'])}</b>\nВыбрано: <b>{esc(opt['label'])}</b>"
            f"{' (голосов: ' + str(tally.get(idx, 0)) + ')' if tally else ' (никто не голосовал, выбрано случайно)'}\n\n"
            f"{'✅' if ok else '⚠️'} {esc(result)}")
    if flavor:
        text += f"\n\n<i>{esc(flavor)}</i>"
    await edit_main(lobby, text)


async def council(lobby: Lobby):
    d, g = lobby.data, lobby.data["g"]
    d.update(phase="council", votes={})
    d["event"].clear()
    alive = g.alive()
    rows = name_rows([(p.uid, p.name) for p in alive], lambda uid: f"bk:c:{lobby.gid}:{uid}")
    rows.append([btn("🤝 Никого", f"bk:c:{lobby.gid}:0", icon=False)])
    await edit_main(lobby, f"{day_header(g)}\n\n🗳 <b>Совет бункера</b>\nЗапасов осталось мало. Нужно ли кого-то "
                           f"отправить наружу? Голосуйте ({COUNCIL_SEC} сек.).",
                    InlineKeyboardMarkup(inline_keyboard=rows))
    try:
        await pause.wait_for(d["event"], COUNCIL_SEC)
    except asyncio.TimeoutError:
        pass
    d["phase"] = "day"
    tally = Counter(v for v in d["votes"].values())
    outcome = "Бункер решил: все остаются."
    if tally:
        top = max(tally.values())
        leaders = [u for u, c in tally.items() if c == top]
        if len(leaders) == 1 and leaders[0] != 0:
            p = g.players[leaders[0]]
            g.exclude(p.uid)
            line = await gamemute.mute_dead(lobby.bot, lobby.chat_id, [(p.uid, p.name, p.prof["name"])],
                                            "изгнан из бункера")
            outcome = "\n".join(line)
    await edit_main(lobby, f"{day_header(g)}\n\n🗳 <b>Итоги совета</b>\n{outcome}")
    await pause.sleep(4)


async def end(lobby: Lobby):
    d, g = lobby.data, lobby.data["g"]
    survived = g.over() == "win"
    survivors = g.alive()
    if survived:
        head = f"{e('party')} <b>Бункер выжил!</b>\nВы продержались {DAYS} дней."
        winners = [p.uid for p in survivors]
    else:
        head = "💀 <b>Бункер не выдержал.</b>\nЗапасы и силы закончились."
        winners = []
    story = await ai.ask(f"Бункер {'выжил' if survived else 'не выжил'} за {g.day} дн. Осталось {len(survivors)} из "
                         f"{len(g.players)}. События: {'; '.join(d['events_log'][-5:]) or 'спокойные дни'}. "
                         "Напиши короткий финал истории.", timeout=8.0)
    text = head + f"\n\n{status(g)}\n"
    if story:
        text += f"\n<i>{esc(story)}</i>\n"
    text += "\n<b>Игроки:</b>\n" + "\n".join(
        f"{'🏆' if p.uid in winners else ('🚪' if not p.alive else '▫️')} {esc(p.name)}: {esc(p.prof['name'])}"
        for p in g.players.values())
    await edit_main(lobby, text)
    await record_results(KEY, lobby.players, winners)


@router.callback_query(F.data.startswith("bk:"))
async def bunker_cb(cb: CallbackQuery):
    _, act, gid, arg = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby or lobby.game.key != KEY or "g" not in lobby.data:
        await cb.answer("Эта игра уже закончилась", show_alert=True)
        return
    d, g, uid = lobby.data, lobby.data["g"], cb.from_user.id
    if uid not in g.players:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if act == "t":
        if d["phase"] != "scavenge":
            await cb.answer("Время сбора вышло", show_alert=True)
            return
        ok, msg = g.toggle(uid, int(arg))
        if not ok:
            await cb.answer(msg, show_alert=True)
            return
        try:
            await cb.message.edit_text(offer_text(g, uid), reply_markup=offer_markup(lobby, g, uid))
        except TelegramBadRequest:
            pass
        await cb.answer(msg)
        return
    if not g.players[uid].alive:
        await cb.answer("Ты уже вне бункера", show_alert=True)
        return
    if act == "o" and d["phase"] == "event":
        d["votes"][uid] = int(arg)
        await cb.answer("Голос принят")
        await edit_main(lobby, d["view"](len(d["votes"])), option_markup(lobby, d["ev"]))
        if len(d["votes"]) >= len(g.alive()):
            d["event"].set()
    elif act == "c" and d["phase"] == "council":
        d["votes"][uid] = int(arg)
        await cb.answer("Голос принят")
        if len(d["votes"]) >= len(g.alive()):
            d["event"].set()
    else:
        await cb.answer("Сейчас нельзя", show_alert=True)


@router.message(Command("bunker"))
async def cmd_bunker(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, BUNKER)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


BUNKER = GameDef(
    key=KEY, title="Бункер: 60 секунд", emoji="shield", command="bunker", needs_dm=True,
    description="20 секунд на сбор вещей, потом 30 дней в бункере. Решайте события вместе и не оставайтесь без еды и воды.",
    min_players=4, max_players=12, start=start_game)
register(BUNKER)
