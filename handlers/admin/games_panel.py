from aiogram import F
from aiogram.types import CallbackQuery

from config import ADMIN_ID
from database.moderation import log_mod
from database.settings import get_setting, set_setting
from games import core as games_core, pause
from games.party.mafia.roles import NAME, REQUIRED_ROLES, ROLES
from handlers.admin.common import BACK, panel, show
from utils.emoji import e
from utils.fmt import code
from utils.ui import kb

GROUPS = {
    "mf": ("🎭 Мафия", [
        ("mafia_night_sec", "Ночь, сек", "60", [30, 45, 60, 90, 120]),
        ("mafia_day_sec", "Обсуждение, сек", "90", [30, 60, 90, 120, 180]),
        ("mafia_vote_sec", "Голосование, сек", "45", [30, 45, 60, 90]),
        ("mafia_min_players", "Мин. игроков", "5", [3, 4, 5, 6, 7, 8])], []),
    "ww": ("🌙 Оборотни", [
        ("ww_night_sec", "Ночь, сек", "60", [30, 45, 60, 90, 120]),
        ("ww_day_sec", "Обсуждение, сек", "90", [30, 60, 90, 120, 180]),
        ("ww_vote_sec", "Голосование, сек", "45", [30, 45, 60, 90])],
        [("ww_doctor", "Доктор"), ("ww_detective", "Детектив")]),
    "as": ("💬 Ассоциации", [
        ("assoc_sec", "Время на слово, сек", "15", [10, 15, 20, 30]),
        ("assoc_lives", "Штрафов до вылета", "2", [1, 2, 3])], []),
}


async def top_size() -> int:
    try:
        return max(3, min(10, int(await get_setting("top_size", "5"))))
    except ValueError:
        return 5


async def coins_size() -> int:
    try:
        return max(5, min(20, int(await get_setting("topcoins_size", "10"))))
    except ValueError:
        return 10


@panel.callback_query(F.data == "a:gcoin")
async def gcoin(cb: CallbackQuery):
    cur = await coins_size()
    await set_setting("topcoins_size", str({10: 15, 15: 20}.get(cur, 10)))
    await games_view(cb)


async def games_view(cb: CallbackQuery):
    n = await top_size()
    active = ", ".join(i["title"] for i in games_core.registry.values()) or "нет"
    text = (f"{e('game')} <b>Игры и топы</b>\n\nРазмер рейтингов игр: {code(f'топ-{n}')} (от 3 до 10)\n"
            f"Идёт сейчас: <b>{active}</b>")
    rows = [[("🎭 Мафия", "a:gs:mf"), ("🌙 Оборотни", "a:gs:ww")],
            [("💬 Ассоциации", "a:gs:as")],
            [("➖", "a:gtop:-1"), (f"🏆 Топ-{n}", "a:gtop:0"), ("➕", "a:gtop:1")],
            [(f"🪙 Топ монет: {await coins_size()} ➜", "a:gcoin")],
            [("🤖 ИИ-рассказчик", "a:ai")]]
    paused = pause.is_paused()
    rows.append([("▶️ Продолжить игры" if paused else "⏸ Пауза для игр", "a:gpause")])
    if games_core.registry:
        rows.append([("⛔ Остановить игру", "a:gstop")])
    await show(cb, text, kb(rows + [BACK]))


@panel.callback_query(F.data == "a:gm")
async def gm_cb(cb: CallbackQuery):
    await games_view(cb)


@panel.callback_query(F.data.startswith("a:gtop:"))
async def gtop(cb: CallbackQuery):
    delta = int(cb.data.split(":")[2])
    n = max(3, min(10, await top_size() + delta))
    await set_setting("top_size", str(n))
    await games_view(cb)


async def ai_view(cb: CallbackQuery):
    from services import ai
    from config import AI_KEY, AI_MODEL
    on = await ai.enabled()
    text = (f"🤖 <b>ИИ-рассказчик</b>\n\nСтатус: <b>{'включён' if on else 'выключен'}</b>\n"
            f"Ключ в .env: <b>{'есть' if AI_KEY else 'не найден'}</b>\nМодель: {code(AI_MODEL)}\n"
            f"Запросов сегодня: {code(str(await ai.used_today()))} из {code(str(await ai.limit()))}\n\n"
            "<i>ИИ пересказывает события «Бункера» и пишет финал. В запросы не уходят имена и сообщения чата. "
            "Без ключа или при выключенном ИИ игры работают на готовых текстах.</i>")
    rows = [[("Выключить" if on else "Включить", "a:ait")], [("📊 Лимит в сутки ➜", "a:ail")],
            [("◀️ К играм", "a:gm")]]
    await show(cb, text, kb(rows))


@panel.callback_query(F.data == "a:ai")
async def ai_cb(cb: CallbackQuery):
    await ai_view(cb)


@panel.callback_query(F.data == "a:ait")
async def ai_toggle(cb: CallbackQuery):
    on = await get_setting("ai_enabled", "0") == "1"
    await set_setting("ai_enabled", "0" if on else "1")
    await ai_view(cb)


@panel.callback_query(F.data == "a:ail")
async def ai_limit(cb: CallbackQuery):
    cur = int(await get_setting("ai_daily_limit", "50"))
    order = [20, 50, 100, 200]
    await set_setting("ai_daily_limit", str(order[(order.index(cur) + 1) % len(order)] if cur in order else 50))
    await ai_view(cb)


@panel.callback_query(F.data == "a:gpause")
async def gpause(cb: CallbackQuery):
    from handlers.system.pause_cmds import do_pause, do_resume
    if pause.is_paused():
        await do_resume(cb.bot)
    else:
        await do_pause(cb.bot)
    await games_view(cb)


@panel.callback_query(F.data == "a:gstop")
async def gstop(cb: CallbackQuery):
    for chat_id in list(games_core.registry):
        await games_core.stop_active(cb.bot, chat_id, "Игра остановлена админом.")
    await log_mod(ADMIN_ID, 0, "game_stop", "панель")
    await games_view(cb)


async def group_view(cb: CallbackQuery, grp: str):
    title, cycles, toggles = GROUPS[grp]
    text = f"<b>{title}</b>\n\n"
    rows = []
    for i, (key, label, default, _) in enumerate(cycles):
        v = await get_setting(key, default)
        text += f"{label}: {code(v)}\n"
        rows.append([(f"{label}: {v} ➜", f"a:gc:{grp}:{i}")])
    for key, label in toggles:
        on = await get_setting(key, "1") == "1"
        text += f"{label}: <b>{'вкл' if on else 'выкл'}</b>\n"
        rows.append([(f"{'✅' if on else '❌'} {label}", f"a:gt:{grp}:{key}")])
    if grp == "mf":
        dis = [d for d in (await get_setting("mafia_disabled", "")).split(",") if d]
        text += f"\nОтключено ролей: {code(str(len(dis)))}"
        rows.append([("🎭 Включить/выключить роли", "a:mfr")])
    await show(cb, text, kb(rows + [[("◀️ К играм", "a:gm")]]))


@panel.callback_query(F.data.startswith("a:gs:"))
async def gs_cb(cb: CallbackQuery):
    await group_view(cb, cb.data.split(":")[2])


@panel.callback_query(F.data.startswith("a:gc:"))
async def gc_cb(cb: CallbackQuery):
    _, _, grp, idx = cb.data.split(":")
    key, _, default, opts = GROUPS[grp][1][int(idx)]
    cur = int(await get_setting(key, default))
    nxt = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else opts[0]
    await set_setting(key, str(nxt))
    await group_view(cb, grp)


@panel.callback_query(F.data.startswith("a:gt:"))
async def gt_cb(cb: CallbackQuery):
    _, _, grp, key = cb.data.split(":")
    on = await get_setting(key, "1") == "1"
    await set_setting(key, "0" if on else "1")
    await group_view(cb, grp)


@panel.callback_query(F.data == "a:mfr")
async def mf_roles(cb: CallbackQuery):
    dis = (await get_setting("mafia_disabled", "")).split(",")
    rows, row = [], []
    for k in ROLES:
        if k in REQUIRED_ROLES:
            continue
        row.append((("❌ " if k in dis else "✅ ") + NAME[k], f"a:mft:{k}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    await show(cb, "<b>Роли в мафии</b>\n\n<i>Нажми на роль, чтобы включить или выключить.</i>",
               kb(rows + [[("◀️ Назад", "a:gs:mf")]]))


@panel.callback_query(F.data.startswith("a:mft:"))
async def mf_toggle(cb: CallbackQuery):
    k = cb.data.split(":")[2]
    dis = [d for d in (await get_setting("mafia_disabled", "")).split(",") if d]
    dis = [d for d in dis if d != k] if k in dis else dis + [k]
    await set_setting("mafia_disabled", ",".join(dis))
    await mf_roles(cb)
