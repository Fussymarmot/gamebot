from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID
from database.moderation import log_mod
from database.settings import get_setting, set_setting
from handlers.admin.common import BACK, CANCEL, panel, S, show
from services import mods
from services.modtools import name_of
from utils.fmt import esc
from utils.targets import parse_free
from utils.ui import kb

HOURS = (24, 48, 72, 168)


async def view(target):
    rows_db = await mods.listing()
    auto = (await get_setting("automod_on", "1")) == "1"
    hours = int(await get_setting("automod_hours", "48"))
    weekly = (await get_setting("weekly_report", "1")) == "1"
    text = "🛡 <b>Модераторы</b>\n\n"
    rows = []
    for uid, perms, until in rows_db:
        n = await name_of(uid)
        text += f"• {esc(n)}: {mods.until_text(until)}\n"
        rows.append([(f"🗑 Снять: {n[:20]}", f"a:mdr:{uid}")])
    if not rows_db:
        text += "<i>Модераторов нет. Права: /mute, /unmute, /warn.</i>\n"
    text += (f"\nАвтомодерация ссылок от новичков: <b>{'вкл' if auto else 'выкл'}</b> (новичок: первые {hours} ч)\n"
             f"Недельный отчёт (воскресенье, 19:00): <b>{'вкл' if weekly else 'выкл'}</b>")
    rows += [[("➕ Назначить модератора", "a:mda")],
             [(f"{'✅' if auto else '❌'} Автомодерация", "a:mdau"), (f"⏱ Новичок: {hours} ч", "a:mdh")],
             [(f"{'✅' if weekly else '❌'} Недельный отчёт", "a:mdw"), ("📋 Отчёт сейчас", "a:mdn")], BACK]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:mods")
async def mods_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await view(cb)


@panel.callback_query(F.data.startswith("a:mdr:"))
async def mod_remove(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    await mods.remove(uid)
    await log_mod(ADMIN_ID, uid, "delmod", "панель")
    await view(cb)


@panel.callback_query(F.data == "a:mda")
async def mod_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.mod_user)
    await show(cb, "Пришли @username или ID будущего модератора. Если username нет, перешли его сообщение "
                   "или выбери его через упоминание.", CANCEL)


@panel.message(S.mod_user)
async def mod_user(message: Message, state: FSMContext):
    t = await parse_free(message)
    if not t or not t.id or t.id == ADMIN_ID:
        await message.answer("❌ Не знаю такого пользователя. Пришли его ID.", reply_markup=CANCEL)
        return
    await state.clear()
    await message.answer(f"На какой срок сделать <b>{esc(t.name)}</b> модератором?",
                         reply_markup=kb([[("Без срока", f"a:mdd:{t.id}:0"), ("1 день", f"a:mdd:{t.id}:1")],
                                          [("7 дней", f"a:mdd:{t.id}:7"), ("30 дней", f"a:mdd:{t.id}:30")],
                                          [("❌ Отмена", "a:cancel")]]))


@panel.callback_query(F.data.startswith("a:mdd:"))
async def mod_days(cb: CallbackQuery):
    _, _, uid, days = cb.data.split(":")
    await mods.add(int(uid), int(days) or None, ADMIN_ID)
    await log_mod(ADMIN_ID, int(uid), "addmod", f"{days} дн." if int(days) else "без срока")
    await view(cb)


@panel.callback_query(F.data == "a:mdau")
async def toggle_auto(cb: CallbackQuery):
    await set_setting("automod_on", "0" if (await get_setting("automod_on", "1")) == "1" else "1")
    await view(cb)


@panel.callback_query(F.data == "a:mdh")
async def cycle_hours(cb: CallbackQuery):
    cur = int(await get_setting("automod_hours", "48"))
    await set_setting("automod_hours", str(HOURS[(HOURS.index(cur) + 1) % len(HOURS)] if cur in HOURS else 48))
    await view(cb)


@panel.callback_query(F.data == "a:mdw")
async def toggle_weekly(cb: CallbackQuery):
    await set_setting("weekly_report", "0" if (await get_setting("weekly_report", "1")) == "1" else "1")
    await view(cb)


@panel.callback_query(F.data == "a:mdn")
async def report_now(cb: CallbackQuery):
    from services import report_week
    await cb.message.answer(await report_week.build())
    await cb.answer()
