import asyncio

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database.settings import get_setting
from games import gamemute, pause
from games.core import dm, finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from games.party.werewolf.engine import ROLE_DESC, ROLE_EMOJI, ROLE_NAME, WerewolfGame
from utils.emoji import e
from utils.fmt import esc
from utils.ui import btn, name_rows
from aiogram.types import InlineKeyboardMarkup

router = Router()
KEY = "werewolf"


def targets_kb(gid: int, prefix: str, targets, skip: bool = True) -> InlineKeyboardMarkup:
    rows = name_rows(targets, lambda uid: f"ww:{prefix}:{gid}:{uid}")
    if skip:
        rows.append([btn("⏭ Пропустить" if prefix == "n" else "🤐 Воздержаться",
                         f"ww:{prefix}:{gid}:0", icon=False)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def cfg_int(key: str, default: int) -> int:
    return int(await get_setting(key, str(default)))


async def start_game(lobby: Lobby):
    use_doc = (await get_setting("ww_doctor", "1")) == "1"
    use_det = (await get_setting("ww_detective", "1")) == "1"
    g = WerewolfGame(lobby.names(), use_doc, use_det)
    g.assign()
    lobby.data.update(g=g, night_event=asyncio.Event(), vote_event=asyncio.Event(), dm={}, dm_text={},
                      night_msg=None, muted=set())
    wolves = [p for p in g.players.values() if p.role == "werewolf"]
    for p in g.players.values():
        t = f"{ROLE_EMOJI[p.role]} <b>Твоя роль: {ROLE_NAME[p.role]}</b>\n\n{esc(ROLE_DESC[p.role])}"
        if p.role == "werewolf" and len(wolves) > 1:
            t += "\n\n<b>Другие оборотни:</b>\n" + "\n".join(
                f"• {esc(w.name)}" for w in wolves if w.uid != p.uid)
        await dm(lobby.bot, p.uid, t)
    await lobby.bot.send_message(
        lobby.chat_id, f"{e('moon')} <b>Оборотни: игра началась!</b>\n\nИгроков: <b>{len(g.players)}</b>, "
                       f"оборотней среди них: <b>{len(wolves)}</b>.\n<i>Роли разосланы в личку.</i>")
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    g: WerewolfGame = lobby.data["g"]
    try:
        for _ in range(30):
            morning = await night(lobby)
            w = g.check_win()
            if w:
                await _clear_night_msg(lobby)
                await lobby.bot.send_message(lobby.chat_id, morning)
                return await end(lobby, w)
            await day(lobby, morning)
            w = g.check_win()
            if w:
                return await end(lobby, w)
        await end(lobby, "wolves")
    except asyncio.CancelledError:
        raise
    except Exception:
        try:
            await lobby.bot.send_message(lobby.chat_id, "⚠️ В игре произошла ошибка, игра остановлена.")
        except Exception:
            pass
    finally:
        finish(lobby)


async def _clear_night_msg(lobby: Lobby):
    m = lobby.data.get("night_msg")
    if m:
        try:
            await m.delete()
        except Exception:
            pass
        lobby.data["night_msg"] = None


async def night(lobby: Lobby) -> str:
    g: WerewolfGame = lobby.data["g"]
    bot, d = lobby.bot, lobby.data
    g.begin_night()
    d["night_event"].clear()
    d["dm"], d["dm_text"] = {}, {}
    d["night_msg"] = await bot.send_message(
        lobby.chat_id, f"{e('moon')} <b>Ночь {g.day}</b>\n\n<i>Город засыпает. Кто-то делает свой ход в личке.</i>")
    prompts = {"werewolf": "Выбери жертву оборотней:", "doctor": "Кого спасёшь этой ночью?",
               "detective": "Кого проверишь этой ночью?"}
    for p in g.alive():
        opts = g.options(p.uid)
        if opts is not None:
            m = await dm(bot, p.uid, f"{ROLE_EMOJI[p.role]} <b>Ночь {g.day}</b>\n\n{prompts[p.role]}",
                         targets_kb(lobby.gid, "n", opts))
            if m:
                d["dm"][p.uid] = m
    if not g.pending():
        await pause.sleep(4)
    else:
        try:
            await pause.wait_for(d["night_event"], await cfg_int("ww_night_sec", 60))
        except asyncio.TimeoutError:
            pass
    for uid, m in d["dm"].items():
        if uid not in g.actions:
            d["dm_text"][uid] = "⌛ Время вышло, ход пропущен."
            try:
                await m.edit_text(d["dm_text"][uid])
            except Exception:
                pass
    res = g.resolve_night()
    for uid, (t, is_wolf) in res["checks"].items():
        body = f"🕵 {esc(g.players[t].name)}: <b>{'ОБОРОТЕНЬ' if is_wolf else 'мирный житель'}</b>."
        m = d["dm"].get(uid)
        try:
            base = d["dm_text"].get(uid, "")
            await m.edit_text((base + "\n\n" if base else "") + body)
        except Exception:
            await dm(bot, uid, body)
    text = f"{e('sparkles')} <b>Наступило утро</b>\n\n"
    if res["died"] is not None:
        p = g.players[res["died"]]
        lines = await gamemute.mute_dead(bot, lobby.chat_id, [(p.uid, p.name, ROLE_NAME[p.role])], "убит")
        d["muted"].add(p.uid)
        text += "\n".join(lines)
    elif res["saved"]:
        text += "Оборотни напали, но жертву спас доктор. Никто не погиб."
    else:
        text += "<i>Этой ночью никто не погиб.</i>"
    return text


async def day(lobby: Lobby, morning: str):
    g: WerewolfGame = lobby.data["g"]
    bot, d = lobby.bot, lobby.data
    sec = await cfg_int("ww_day_sec", 90)
    alive = "\n".join(f"• {esc(p.name)}" for p in g.alive())
    await bot.send_message(lobby.chat_id, f"{morning}\n\n☀️ <b>День {g.day}</b>\n<i>Обсудите, кто оборотень. "
                                          f"Голосование через {sec} сек.</i>\n\n<b>Живые:</b>\n{alive}")
    await _clear_night_msg(lobby)
    await pause.sleep(sec)
    candidates = None
    for round_no in (1, 2):
        g.begin_vote(candidates)
        d["vote_event"].clear()
        targets = [(p.uid, p.name) for p in g.alive() if candidates is None or p.uid in candidates]
        title = "🗳 <b>Голосование</b>" if round_no == 1 else "🗳 <b>Повторное голосование</b> (ничья)"
        d["vote_msg"] = await bot.send_message(
            lobby.chat_id, f"{title}\n\n<i>Проголосовало: 0/{len(g.alive())}</i>",
            reply_markup=targets_kb(lobby.gid, "v", targets))
        try:
            await pause.wait_for(d["vote_event"], await cfg_int("ww_vote_sec", 45))
        except asyncio.TimeoutError:
            pass
        tally = g.tally()
        out, tied = g.resolve_vote()
        lines = "\n".join(f"• {esc(g.players[t].name)}: {c}" for t, c in tally.most_common())
        if out is not None:
            p = g.players[out]
            mute = await gamemute.mute_dead(bot, lobby.chat_id, [(p.uid, p.name, f"{ROLE_EMOJI[p.role]} {ROLE_NAME[p.role]}")],
                                            "казнён")
            d["muted"].add(p.uid)
            text = f"🗳 <b>Итоги голосования</b>\n\n{lines}\n\n" + "\n".join(mute)
        elif tied and round_no == 1:
            text = f"🗳 <b>Итоги голосования</b>\n\n{lines}\n\n<i>Ничья! Повторное голосование.</i>"
        else:
            text = (f"🗳 <b>Итоги голосования</b>\n\n{lines}\n\n" if lines else "🗳 ") + "<i>Никого не изгнали.</i>"
        try:
            await d["vote_msg"].edit_text(text)
        except Exception:
            await bot.send_message(lobby.chat_id, text)
        if out is not None or not tied or round_no == 2:
            break
        candidates = tied


async def end(lobby: Lobby, key: str):
    g: WerewolfGame = lobby.data["g"]
    if key == "villagers":
        head = f"{e('party')} <b>Победа мирных жителей!</b>\nВсе оборотни найдены."
        winners = [p.uid for p in g.players.values() if p.role != "werewolf"]
    else:
        head = "🐺 <b>Победа оборотней!</b>\nОни захватили деревню."
        winners = [p.uid for p in g.players.values() if p.role == "werewolf"]
    text = head + "\n\n<b>Роли:</b>\n"
    for p in g.players.values():
        mark = "🏆" if p.uid in winners else ("☠️" if not p.alive else "▫️")
        text += f"{mark} {esc(p.name)} — {ROLE_EMOJI[p.role]} {ROLE_NAME[p.role]}\n"
    await lobby.bot.send_message(lobby.chat_id, text)
    await record_results(KEY, g.players, winners)


@router.callback_query(F.data.startswith("ww:"))
async def ww_cb(cb: CallbackQuery):
    _, act, gid, target = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby or lobby.game.key != KEY or "g" not in lobby.data:
        await cb.answer("Эта игра уже закончилась", show_alert=True)
        return
    g: WerewolfGame = lobby.data["g"]
    user = cb.from_user
    target = int(target) or None
    if act == "n":
        if g.phase != "night" or not g.submit(user.id, target):
            await cb.answer("Сейчас нельзя", show_alert=True)
            return
        shown = "Ход пропущен." if target is None else f"Ты выбрал: <b>{esc(g.players[target].name)}</b>"
        lobby.data["dm_text"][user.id] = shown
        await cb.message.edit_text(shown)
        await cb.answer()
        if g.players[user.id].role == "werewolf" and target:
            for w in g.wolves():
                if w.uid != user.id:
                    await dm(cb.bot, w.uid, f"🐺 {esc(g.players[user.id].name)} выбрал жертвой "
                                            f"<b>{esc(g.players[target].name)}</b>")
        if not g.pending():
            lobby.data["night_event"].set()
    elif act == "v":
        if g.phase != "vote" or not g.submit_vote(user.id, target):
            await cb.answer("Ты не можешь голосовать", show_alert=True)
            return
        await cb.answer("Голос принят" if target else "Ты воздержался")
        try:
            await lobby.data["vote_msg"].edit_text(
                f"🗳 <b>Голосование</b>\n\n<i>Проголосовало: {len(g.votes)}/{len(g.alive())}</i>",
                reply_markup=cb.message.reply_markup)
        except TelegramBadRequest:
            pass
        if g.all_voted():
            lobby.data["vote_event"].set()


@router.message(Command("werewolf"))
async def cmd_werewolf(message: Message):
    err = await open_lobby(message.bot, message.chat.id, message.from_user, WEREWOLF)
    if err:
        await message.answer(f"⚠️ {esc(err)}")


WEREWOLF = GameDef(
    key=KEY, title="Оборотни", emoji="moon", command="werewolf", needs_dm=True,
    description="Мирные жители ищут оборотней, оборотни охотятся по ночам. Есть доктор и детектив.",
    min_players=6, max_players=15, start=start_game)
register(WEREWOLF)
