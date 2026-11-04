from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.core import x
from database.moderation import log_mod
from handlers.admin.common import BACK, CANCEL, panel, parse_when, S, show
from handlers.economy.giveaway import join_kb
from services import giveaways as gw
from services.scheduler import schedule_giveaway, unschedule_giveaway
from utils.fmt import admin_text, code, esc, plain, rich
from utils.timeutils import now, parse_ts, ts
from utils.ui import kb

METRICS = [("messages", "💬 Сообщения"), ("game:all", "🏆 Все игры"), ("game:ttt", "⭕ Крестики-нолики"),
           ("game:rps", "🎲 КНБ"), ("game:duel", "🗡 Дуэль"), ("game:mafia", "🎭 Мафия"),
           ("game:memory", "🎮 Мемори"), ("game:code", "🔑 Взломай код"),
           ("game:assoc", "💬 Ассоциации"), ("game:bunker", "🛡 Бункер")]


async def gw_list(target):
    active = await gw.active_list()
    done = await gw.recent_list(3)
    text = "🎁 <b>Розыгрыши</b>\n\n"
    rows = []
    if active:
        text += "<b>Активные:</b>\n"
        for g in active:
            text += f"• {rich(g['title'])}: до {parse_ts(g['ends_at']).strftime('%d.%m %H:%M')}\n"
            rows.append([(f"🎁 {g['title'][:28]}", f"a:gwv:{g['id']}")])
    else:
        text += "<i>Активных розыгрышей нет.</i>\n"
    if done:
        text += "\n<b>Недавние:</b>\n" + "\n".join(
            f"• {rich(g['title'])} ({'завершён' if g['status'] == 'done' else 'отменён'})" for g in done)
    rows += [[("➕ Создать розыгрыш", "a:gwn")], BACK]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:gw")
async def gw_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await gw_list(cb)


@panel.callback_query(F.data.startswith("a:gwv:"))
async def gw_view(cb: CallbackQuery):
    g = await gw.get(int(cb.data.split(":")[2]))
    if not g:
        await gw_list(cb)
        return
    n = await gw.entries_count(g["id"])
    text = gw.announce_text(g, n if g["kind"] == "draw" else None) + f"\n\nСтатус: <b>{g['status']}</b>"
    rows = []
    if g["status"] == "active":
        rows.append([("🏁 Завершить сейчас", f"a:gwf:{g['id']}"), ("🚫 Отменить", f"a:gwx:{g['id']}")])
    await show(cb, text, kb(rows + [[("◀️ К розыгрышам", "a:gw")]]))


@panel.callback_query(F.data.startswith("a:gwf:"))
async def gw_finish_now(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    unschedule_giveaway(gid)
    await gw.finish(cb.bot, gid)
    await log_mod(ADMIN_ID, 0, "giveaway_finish", str(gid))
    await gw_list(cb)


@panel.callback_query(F.data.startswith("a:gwx:"))
async def gw_cancel(cb: CallbackQuery):
    gid = int(cb.data.split(":")[2])
    unschedule_giveaway(gid)
    await gw.cancel(cb.bot, gid)
    await log_mod(ADMIN_ID, 0, "giveaway_cancel", str(gid))
    await gw_list(cb)


@panel.callback_query(F.data == "a:gwn")
async def gw_new(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.set_state(S.gw_title)
    await show(cb, "🎁 <b>Новый розыгрыш</b>\n\nНазвание розыгрыша:", CANCEL)


@panel.message(S.gw_title)
async def gw_title(message: Message, state: FSMContext):
    t = admin_text(message)
    if not t or len(plain(t)) > 60:
        await message.answer("❌ Название до 60 символов.", reply_markup=CANCEL)
        return
    await state.update_data(title=t)
    await state.set_state(S.gw_prize)
    await message.answer("Приз (текст):", reply_markup=CANCEL)


@panel.message(S.gw_prize)
async def gw_prize(message: Message, state: FSMContext):
    t = admin_text(message)
    if not t or len(plain(t)) > 120:
        await message.answer("❌ Приз до 120 символов.", reply_markup=CANCEL)
        return
    await state.update_data(prize=t)
    await state.set_state(None)
    await step(message, state)


async def step(target, state: FSMContext):
    d = await state.get_data()
    if "kind" not in d:
        await show(target, "Как определяем победителей?", kb([
            [("🎲 Случайно среди участников", "a:gwk:draw")],
            [("🏆 Лучшие по рейтингу (топ)", "a:gwk:top")], [("❌ Отмена", "a:cancel")]]))
        return
    if d["kind"] == "draw" and "cond" not in d:
        await show(target, "Кто может участвовать?", kb([
            [("Любой желающий", "a:gwc:none")],
            [("Только из топ-N рейтинга", "a:gwc:top")],
            [("Только с минимумом очков", "a:gwc:min")], [("❌ Отмена", "a:cancel")]]))
        return
    need_metric = d["kind"] == "top" or d.get("cond", "none") != "none"
    if need_metric and "metric" not in d:
        rows, row = [], []
        for i, (_, title) in enumerate(METRICS):
            row.append((title, f"a:gwm:{i}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        await show(target, "По какому показателю считаем рейтинг?", kb(rows + [[("❌ Отмена", "a:cancel")]]))
        return
    if need_metric and d["metric"] == "messages" and "days" not in d:
        await show(target, "За какой период считаем сообщения?", kb([
            [("Сутки", "a:gwd:1"), ("Неделя", "a:gwd:7"), ("Месяц", "a:gwd:30")], [("❌ Отмена", "a:cancel")]]))
        return
    if d["kind"] == "draw" and d.get("cond", "none") != "none" and "value" not in d:
        await state.set_state(S.gw_value)
        q = ("Сколько мест в топе открывают участие? Число от 1 до 50:" if d["cond"] == "top"
             else "Минимальное значение показателя (число от 1 до 100000):")
        await show(target, q, CANCEL)
        return
    if "winners" not in d:
        await show(target, "Сколько победителей?", kb([
            [(str(n), f"a:gww:{n}") for n in (1, 2, 3, 5, 10)], [("❌ Отмена", "a:cancel")]]))
        return
    if "ends_at" not in d:
        await state.set_state(S.gw_end)
        await show(target, "Когда подвести итоги? Формат: " +
                   f"{code('25.12.2026 18:30')}, {code('25.12 18:30')} или {code('18:30')}:", CANCEL)
        return
    await preview(target, state)


def build(d: dict) -> dict:
    return {"title": d["title"], "prize": d["prize"], "kind": d["kind"], "metric": d.get("metric"),
            "metric_days": d.get("days", 7), "cond_type": d.get("cond", "none"),
            "cond_value": d.get("value", 0), "winners": d["winners"], "ends_at": d["ends_at"]}


async def preview(target, state: FSMContext):
    d = await state.get_data()
    g = build(d)
    await show(target, "👀 <b>Предпросмотр</b>\n\n" + gw.announce_text(g, 0 if g["kind"] == "draw" else None) +
               "\n\nОпубликовать в группе?",
               kb([[("✅ Опубликовать", "a:gwpub"), ("❌ Отмена", "a:cancel")]]))


@panel.callback_query(F.data.startswith("a:gwk:"))
async def gw_kind(cb: CallbackQuery, state: FSMContext):
    await state.update_data(kind=cb.data.split(":")[2])
    await step(cb, state)


@panel.callback_query(F.data.startswith("a:gwc:"))
async def gw_cond(cb: CallbackQuery, state: FSMContext):
    await state.update_data(cond=cb.data.split(":")[2])
    await step(cb, state)


@panel.callback_query(F.data.startswith("a:gwm:"))
async def gw_metric(cb: CallbackQuery, state: FSMContext):
    await state.update_data(metric=METRICS[int(cb.data.split(":")[2])][0])
    await step(cb, state)


@panel.callback_query(F.data.startswith("a:gwd:"))
async def gw_days(cb: CallbackQuery, state: FSMContext):
    await state.update_data(days=int(cb.data.split(":")[2]))
    await step(cb, state)


@panel.message(S.gw_value)
async def gw_value(message: Message, state: FSMContext):
    d = await state.get_data()
    t = (message.text or "").strip()
    limit = 50 if d.get("cond") == "top" else 100000
    if not t.isdigit() or not 1 <= int(t) <= limit:
        await message.answer(f"❌ Нужно число от 1 до {limit}.", reply_markup=CANCEL)
        return
    await state.update_data(value=int(t))
    await state.set_state(None)
    await step(message, state)


@panel.callback_query(F.data.startswith("a:gww:"))
async def gw_winners(cb: CallbackQuery, state: FSMContext):
    await state.update_data(winners=int(cb.data.split(":")[2]))
    await step(cb, state)


@panel.message(S.gw_end)
async def gw_end(message: Message, state: FSMContext):
    d = parse_when(message.text)
    if not d or d <= now():
        await message.answer("❌ Не понял дату или она уже прошла. Пример: " + code("25.12 18:30"),
                             reply_markup=CANCEL)
        return
    await state.update_data(ends_at=ts(d))
    await state.set_state(None)
    await step(message, state)


@panel.callback_query(F.data == "a:gwpub")
async def gw_publish(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if not d.get("ends_at"):
        await cb.answer("Мастер не завершён", show_alert=True)
        return
    gid = await gw.create({"title": d["title"], "prize": d["prize"], "kind": d["kind"],
                           "metric": d.get("metric"), "days": d.get("days", 7), "cond": d.get("cond", "none"),
                           "value": d.get("value", 0), "winners": d["winners"], "ends_at": d["ends_at"]})
    g = await gw.get(gid)
    msg = await cb.bot.send_message(
        GROUP_ID, gw.announce_text(g, 0 if g["kind"] == "draw" else None),
        reply_markup=join_kb(gid) if g["kind"] == "draw" else None)
    await x("UPDATE giveaways SET message_id=? WHERE id=?", (msg.message_id, gid))
    schedule_giveaway(gid, g["ends_at"])
    await log_mod(ADMIN_ID, 0, "giveaway_create", g["title"])
    await state.clear()
    await cb.answer("Опубликовано")
    await gw_list(cb)
