import asyncio
import logging
import random

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from database.core import x
from games import pause
from games.core import finish, GameDef, lobbies, Lobby, open_lobby, record_results, register
from services import economy as ec
from utils.fmt import esc
from utils.ui import kb

router = Router()
log = logging.getLogger(__name__)
KEY = "roulette"
CHAMBERS = 6
TURN_SEC = 60
MAX_STAKE = 200


def panel(lobby: Lobby, note: str = "") -> str:
    d = lobby.data
    alive = "\n".join(f"{'🔫' if u == d['turn'] else '🙂'} {esc(lobby.players[u])}" for u in d["alive"])
    dead = ", ".join(esc(lobby.players[u]) for u in lobby.players if u not in d["alive"])
    stake = f"\nСтавка: <b>{d['stake']}</b> 🪙" if d.get("stake") else ""
    t = f"🔫 <b>Русская рулетка</b>{stake}\n\n{alive}"
    if dead:
        t += f"\n\n💀 {dead}"
    if note:
        t += f"\n\n{note}"
    return t


def markup(lobby: Lobby):
    d = lobby.data
    cur = d["turn"]
    rows = [[("🔫 Выстрел", f"ru:{lobby.gid}:fire")]]
    if d["spins"].get(cur):
        rows.append([("🌀 Крутить барабан и выстрелить", f"ru:{lobby.gid}:spin")])
    return kb(rows)


async def start_game(lobby: Lobby):
    stake = lobby.data.get("stake", 0)
    if stake:
        poor = [u for u in lobby.players if (await ec.wallet(u))[0] < stake]
        for u in poor:
            lobby.players.pop(u, None)
        if poor and len(lobby.players) < 2:
            await lobby.bot.send_message(lobby.chat_id, "❌ Не у всех хватило монет на ставку, игра отменена.")
            finish(lobby)
            return
    order = list(lobby.players)
    random.shuffle(order)
    lobby.data.update(alive=order, turn=order[0], bullet=random.randrange(CHAMBERS), pos=0,
                      spins={u: True for u in order}, event=asyncio.Event(), action=None, msg=None)
    lobby.task = asyncio.create_task(run(lobby))


async def run(lobby: Lobby):
    d, bot = lobby.data, lobby.bot
    try:
        d["msg"] = await bot.send_message(lobby.chat_id, panel(lobby, "Ход открыт."), reply_markup=markup(lobby))
        while len(d["alive"]) > 1:
            cur = d["turn"]
            d["event"].clear()
            d["action"] = None
            try:
                await pause.wait_for(d["event"], TURN_SEC)
            except asyncio.TimeoutError:
                d["action"] = "fire"
            if d["action"] == "spin":
                d["spins"][cur] = False
                d["bullet"], d["pos"] = random.randrange(CHAMBERS), 0
            name = esc(lobby.players[cur])
            i = d["alive"].index(cur)
            died = d["pos"] == d["bullet"]
            if died:
                d["alive"].remove(cur)
                d["bullet"], d["pos"] = random.randrange(CHAMBERS), 0
                note = f"💥 <b>{name}</b> выбывает!"
            else:
                d["pos"] += 1
                note = f"😮‍💨 <b>{name}</b> выжил(а): щелчок."
            if len(d["alive"]) > 1:
                d["turn"] = d["alive"][(i if died else i + 1) % len(d["alive"])]
                try:
                    await d["msg"].edit_text(panel(lobby, note), reply_markup=markup(lobby))
                except Exception:
                    d["msg"] = await bot.send_message(lobby.chat_id, panel(lobby, note), reply_markup=markup(lobby))
        winner = d["alive"][0]
        pot = await settle(lobby, winner)
        text = f"🏆 <b>{esc(lobby.players[winner])}</b> остаётся в живых!"
        if pot:
            text += f"\nВыигрыш: <b>{pot}</b> 🪙"
        try:
            await d["msg"].edit_text(panel(lobby, text))
        except Exception:
            await bot.send_message(lobby.chat_id, text)
        await record_results(KEY, lobby.players, [winner])
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Ошибка в рулетке")
    finally:
        finish(lobby)


async def settle(lobby: Lobby, winner: int) -> int:
    stake = lobby.data.get("stake", 0)
    if not stake:
        return 0
    pot = 0
    for u in lobby.players:
        if u == winner:
            continue
        have = (await ec.wallet(u))[0]
        pay = min(stake, have)
        if pay:
            await x("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (pay, u))
            pot += pay
    if pot:
        await ec.ensure(winner)
        await x("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (pot, winner))
    return pot


@router.callback_query(F.data.startswith("ru:"))
async def ru_cb(cb: CallbackQuery):
    _, gid, act = cb.data.split(":")
    lobby = next((l for l in lobbies.values() if l.gid == int(gid)), None)
    if not lobby or lobby.game.key != KEY or not lobby.started or "turn" not in lobby.data:
        await cb.answer("Игра завершена", show_alert=True)
        return
    d = lobby.data
    if cb.from_user.id not in lobby.players:
        await cb.answer("Ты не участвуешь в этой игре", show_alert=True)
        return
    if d["turn"] != cb.from_user.id:
        await cb.answer("Сейчас не твой ход", show_alert=True)
        return
    if act == "spin" and not d["spins"].get(cb.from_user.id):
        await cb.answer("Ты уже крутил(а) барабан", show_alert=True)
        return
    if d["event"].is_set():
        await cb.answer()
        return
    d["action"] = act
    d["event"].set()
    await cb.answer("Щёлк...")


@router.message(Command("roulette"))
async def cmd_roulette(message: Message):
    parts = (message.text or "").split()
    stake = 0
    if len(parts) > 1:
        if not parts[1].isdigit() or not 1 <= int(parts[1]) <= MAX_STAKE:
            await message.answer(f"❌ Ставка: число монет от 1 до {MAX_STAKE}. Пример: <code>/roulette 20</code>")
            return
        stake = int(parts[1])
        if (await ec.wallet(message.from_user.id))[0] < stake:
            await message.answer("❌ У тебя не хватает монет на такую ставку.")
            return
    err = await open_lobby(message.bot, message.chat.id, message.from_user, ROULETTE)
    if err:
        await message.answer(f"⚠️ {esc(err)}")
        return
    lobby = lobbies.get(message.chat.id)
    if lobby and stake:
        lobby.data["stake"] = stake
        lobby.data["note"] = f"Ставка: {stake} 🪙. Проигравшие отдают её победителю (сколько есть на балансе)."
        await lobby.refresh()


ROULETTE = GameDef(
    key=KEY, title="Русская рулетка", emoji="skull", command="roulette",
    description="Револьвер на 6 патронов, один заряжен. По очереди жми «Выстрел», выживший побеждает.",
    min_players=2, max_players=6, start=start_game)
register(ROULETTE)
