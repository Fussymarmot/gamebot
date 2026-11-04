import logging

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.core import batch, q
from database.moderation import log_mod
from database.users import get_user_name
from games.ratings import RATING_GAMES
from handlers.admin.common import BACK, CANCEL, panel, S, show
from services import backup
from utils.emoji import e
from utils.fmt import esc
from utils.targets import parse_free
from utils.ui import kb

log = logging.getLogger(__name__)

ACTIONS = {
    "coins_all": ("рубины всех игроков", [
        ("UPDATE wallets SET coins=0, total_earned=0", ()), ("DELETE FROM daily_tasks WHERE task='_earned'", ())]),
    "ratings_all": ("рейтинги всех игр", [("DELETE FROM game_stats", ()), ("DELETE FROM game_log", ())]),
    "ach_tasks": ("достижения и ежедневные задания", [("DELETE FROM achievements", ()), ("DELETE FROM daily_tasks", ())]),
    "season": ("всё за сезон: рубины, рейтинги игр, достижения и задания", [
        ("UPDATE wallets SET coins=0, total_earned=0", ()), ("DELETE FROM game_stats", ()), ("DELETE FROM game_log", ()),
        ("DELETE FROM achievements", ()), ("DELETE FROM daily_tasks", ())]),
}


async def cleanup_menu(target):
    rows = [[("♦️ Рубины одного игрока", "a:clu"), ("♦️ Рубины всех", "a:cla:coins_all")],
            [("🏆 Рейтинг одной игры", "a:clg"), ("🏆 Все рейтинги", "a:cla:ratings_all")],
            [("🎖 Достижения и задания", "a:cla:ach_tasks")],
            [("🏁 Новый сезон", "a:cla:season")]]
    await show(target, "🧹 <b>Очистка</b>\n\n<i>Перед каждой очисткой бот присылает тебе бэкап базы, "
                       "а действие записывается в журнал.</i>", kb(rows + [BACK]))


@panel.callback_query(F.data == "a:cl")
async def cl_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cleanup_menu(cb)


@panel.callback_query(F.data.startswith("a:cla:"))
async def cl_ask(cb: CallbackQuery):
    what = cb.data.split(":")[2]
    title = ACTIONS[what][0]
    extra = "\nВ группу уйдёт объявление победителей." if what == "season" else ""
    await show(cb, f"Точно очистить: <b>{esc(title)}</b>?{extra}\nЭто нельзя отменить (бэкап останется у тебя).",
               kb([[("✅ Да, очистить", f"a:cld:{what}"), ("❌ Нет", "a:cl")]]))


async def season_summary() -> str:
    wins = await q("""SELECT g.user_id, SUM(g.wins) w FROM game_stats g GROUP BY g.user_id HAVING w > 0
                      ORDER BY w DESC LIMIT 3""")
    coins = await q("SELECT user_id, coins FROM wallets WHERE coins > 0 ORDER BY coins DESC LIMIT 3")
    medals = ["🥇", "🥈", "🥉"]
    text = f"{e('party')} <b>Сезон завершён!</b>\n\n"
    if wins:
        lines = [f"{medals[i]} {esc(await get_user_name(u))}: {w}" for i, (u, w) in enumerate(wins)]
        text += "<b>Больше всего побед:</b>\n" + "\n".join(lines) + "\n\n"
    if coins:
        lines = [f"{medals[i]} {esc(await get_user_name(u))}: {c}" for i, (u, c) in enumerate(coins)]
        text += "<b>Больше всего рубинов:</b>\n" + "\n".join(lines) + "\n\n"
    return text + "<i>Новый сезон начинается: рейтинги и рубины обнулены.</i>"


@panel.callback_query(F.data.startswith("a:cld:"))
async def cl_do(cb: CallbackQuery):
    what = cb.data.split(":")[2]
    await cb.answer("Делаю бэкап...")
    await backup.send_backup(cb.bot, "перед очисткой")
    summary = None
    if what == "season":
        try:
            summary = await season_summary()
        except Exception:
            log.exception("Не удалось собрать итоги сезона")
    try:
        await batch(ACTIONS[what][1])
    except Exception:
        log.exception("Очистка не удалась")
        await cb.message.answer("❌ Очистка не удалась, подробности в логе. Бэкап уже у тебя.")
        return
    await log_mod(ADMIN_ID, 0, "cleanup", what)
    if summary:
        try:
            await cb.bot.send_message(GROUP_ID, summary)
        except Exception:
            log.exception("Не удалось объявить итоги сезона")
    await cb.message.answer(f"{e('check')} Готово: {esc(ACTIONS[what][0])}.")
    await cleanup_menu(cb)


@panel.callback_query(F.data == "a:clg")
async def cl_game(cb: CallbackQuery):
    rows = [[(title, f"a:clga:{key}")] for key, title, _ in RATING_GAMES if key != "all"]
    await show(cb, "Рейтинг какой игры очистить?", kb(rows + [[("◀️ Назад", "a:cl")]]))


@panel.callback_query(F.data.startswith("a:clga:"))
async def cl_game_ask(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    title = next(t for k, t, _ in RATING_GAMES if k == key)
    await show(cb, f"Очистить рейтинг игры «{esc(title)}»?",
               kb([[("✅ Да", f"a:clgd:{key}"), ("❌ Нет", "a:cl")]]))


@panel.callback_query(F.data.startswith("a:clgd:"))
async def cl_game_do(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    await cb.answer("Делаю бэкап...")
    await backup.send_backup(cb.bot, "перед очисткой")
    await batch([("DELETE FROM game_stats WHERE game_type=?", (key,)), ("DELETE FROM game_log WHERE game_key=?", (key,))])
    await log_mod(ADMIN_ID, 0, "cleanup", f"rating {key}")
    await cleanup_menu(cb)


@panel.callback_query(F.data == "a:clu")
async def cl_user(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.clean_user)
    await show(cb, "Пришли @username или ID игрока, у которого обнулить рубины (можно переслать его сообщение):", CANCEL)


@panel.message(S.clean_user)
async def cl_user_do(message: Message, state: FSMContext):
    t = await parse_free(message)
    uid = t.id if t else None
    if not uid:
        await message.answer("❌ Не нашёл такого игрока.", reply_markup=CANCEL)
        return
    await backup.send_backup(message.bot, "перед очисткой")
    await batch([("UPDATE wallets SET coins=0, total_earned=0 WHERE user_id=?", (uid,))])
    await log_mod(ADMIN_ID, uid, "cleanup", "coins one")
    await state.clear()
    await message.answer(f"{e('check')} Рубины игрока {esc(await get_user_name(uid))} обнулены.")
    await cleanup_menu(message)
