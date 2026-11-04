import secrets

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID
from database.core import q, x
from database.moderation import log_mod
from database.settings import set_setting
from handlers.admin.common import BACK, CANCEL, panel, S, show
from services import economy as ec
from utils import titles
from utils.fmt import admin_text, code, esc, plain, rich
from utils.ui import kb


async def shop_view(target):
    level = ec.LEVELS[ec.level_key][0]
    premod = await titles.premoderation()
    pending = (await q("SELECT COUNT(*) FROM orders WHERE status='new'", one=True))[0]
    items = (await q("SELECT COUNT(*) FROM shop_items WHERE active=1", one=True))[0]
    text = (f"🛍 <b>Магазин и звания</b>\n\nЭкономика: <b>{level}</b> (награды ×{ec.mult()}, лимит {ec.daily_cap()} рубинов в сутки)\n"
            f"Призов в магазине: {code(str(items))}\nЗаказов ждёт решения: {code(str(pending))}\n"
            f"Проверка своих званий: <b>{'включена' if premod else 'выключена'}</b>")
    rows = [[(f"⚖️ Экономика: {level} ➜", "a:shlvl")],
            [("🎁 Призы магазина", "a:shpz"), ("🏷 Готовые звания", "a:shtl")],
            [("🏆 Титулы магазина", "a:ttl")],
            [(f"{'✅' if premod else '❌'} Проверять свои звания", "a:shpm")]]
    if pending:
        rows.append([("📋 Заказы на решении", "a:shord")])
    await show(target, text, kb(rows + [BACK]))


@panel.callback_query(F.data == "a:shop")
async def shop_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await shop_view(cb)


@panel.callback_query(F.data == "a:shlvl")
async def shop_level(cb: CallbackQuery):
    order = list(ec.LEVELS)
    await ec.set_level(order[(order.index(ec.level_key) + 1) % len(order)])
    await log_mod(ADMIN_ID, 0, "economy_level", ec.level_key)
    await shop_view(cb)


@panel.callback_query(F.data == "a:shpm")
async def shop_premod(cb: CallbackQuery):
    await set_setting("title_premod", "0" if await titles.premoderation() else "1")
    await shop_view(cb)


@panel.callback_query(F.data == "a:shord")
async def shop_orders(cb: CallbackQuery):
    rows = await q("SELECT id, kind, title, user_id FROM orders WHERE status='new' ORDER BY id LIMIT 10")
    text = "📋 <b>Заказы на решении</b>\n\n"
    buttons = []
    for oid, kind, title, uid in rows:
        text += f"#{oid} {esc(kind)}: {rich(title)} ({esc(await ec.name_of(uid))})\n"
        buttons.append([(f"✅ #{oid}", f"ord:{oid}:ok"), (f"❌ #{oid}", f"ord:{oid}:no")])
    await show(cb, text, kb(buttons + [[("◀️ Назад", "a:shop")]]))


async def prizes_view(target):
    items = await q("SELECT id, title, price, stock FROM shop_items WHERE active=1 ORDER BY id")
    text = "🎁 <b>Призы магазина</b>\n\n"
    rows = []
    for item_id, title, price, stock in items:
        left = "без ограничения" if stock < 0 else f"осталось {stock}"
        text += f"• {rich(title)}: {price} ♦️ ({left})\n"
        rows.append([(f"🗑 {plain(title)[:28]}", f"a:shpd:{item_id}")])
    if not items:
        text += "<i>Призов пока нет.</i>"
    rows += [[("➕ Добавить приз", "a:shpa")], [("◀️ Назад", "a:shop")]]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:shpz")
async def prizes_cb(cb: CallbackQuery):
    await prizes_view(cb)


@panel.callback_query(F.data.startswith("a:shpd:"))
async def prize_delete(cb: CallbackQuery):
    await x("UPDATE shop_items SET active=0 WHERE id=?", (int(cb.data.split(":")[2]),))
    await prizes_view(cb)


@panel.callback_query(F.data == "a:shpa")
async def prize_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.prize_title)
    await show(cb, "Название приза, например «Выбор музыки в голосовом чате». Свои премиум-эмодзи сохранятся как есть:", CANCEL)


@panel.message(S.prize_title)
async def prize_title(message: Message, state: FSMContext):
    t = admin_text(message)
    if not t or len(plain(t)) > 60:
        await message.answer("❌ Название до 60 символов.", reply_markup=CANCEL)
        return
    await state.update_data(title=t)
    await state.set_state(S.prize_price)
    await message.answer("Цена в рубинах (число от 5 до 5000):", reply_markup=CANCEL)


@panel.message(S.prize_price)
async def prize_price(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not t.isdigit() or not 5 <= int(t) <= 5000:
        await message.answer("❌ Нужно число от 5 до 5000.", reply_markup=CANCEL)
        return
    await state.update_data(price=int(t))
    await state.set_state(S.prize_stock)
    await message.answer("Сколько штук? Число от 1 до 100 или 0, если без ограничения:", reply_markup=CANCEL)


@panel.message(S.prize_stock)
async def prize_stock(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not t.isdigit() or int(t) > 100:
        await message.answer("❌ Нужно число от 0 до 100.", reply_markup=CANCEL)
        return
    d = await state.get_data()
    stock = int(t) if int(t) > 0 else -1
    await x("INSERT INTO shop_items (title, price, stock, active) VALUES (?,?,?,1)", (d["title"], d["price"], stock))
    await log_mod(ADMIN_ID, 0, "prize_add", plain(d["title"]))
    await state.clear()
    await prizes_view(message)


async def titles_view(target):
    items = await titles.presets()
    text = "🏷 <b>Готовые звания</b>\n\n" + "\n".join(f"• {esc(t)}" for t in items)
    rows = [[(f"🗑 {t}", f"a:shtd:{i}")] for i, t in enumerate(items)]
    rows += [[("➕ Добавить звание", "a:shta")], [("◀️ Назад", "a:shop")]]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:shtl")
async def titles_cb(cb: CallbackQuery):
    await titles_view(cb)


@panel.callback_query(F.data.startswith("a:shtd:"))
async def title_delete(cb: CallbackQuery):
    items = await titles.presets()
    i = int(cb.data.split(":")[2])
    if 0 <= i < len(items):
        items.pop(i)
        await titles.save_presets(items)
    await titles_view(cb)


@panel.callback_query(F.data == "a:shta")
async def title_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.title_add)
    await show(cb, f"Новое звание (до {titles.MAX_LEN} символов):", CANCEL)


@panel.message(S.title_add)
async def title_add_save(message: Message, state: FSMContext):
    ok, reason, clean = titles.check(message.text or "")
    if not ok:
        await message.answer("❌ " + esc(reason), reply_markup=CANCEL)
        return
    items = await titles.presets()
    if clean not in items:
        await titles.save_presets(items + [clean])
    await state.clear()
    await titles_view(message)


async def tt_view(target):
    text = "🏆 <b>Титулы магазина</b>\n\n"
    rows = []
    for key, (name, base) in ec.SHOP.items():
        text += f"• {rich(name)}: {base} ♦️\n"
        rows.append([(f"✏️ {plain(name)[:26]}", f"a:tte:{key}")])
    if not ec.SHOP:
        text += "<i>Титулов нет.</i>\n"
    text += "\n<i>Цена базовая: итоговая зависит от уровня экономики. Премиум-эмодзи в названии сохраняются.</i>"
    rows += [[("➕ Добавить титул", "a:tta")], [("◀️ Назад", "a:shop")]]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:ttl")
async def tt_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await tt_view(cb)


@panel.callback_query(F.data.startswith("a:tte:"))
async def tt_edit(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    key = cb.data.split(":")[2]
    if key not in ec.SHOP:
        await tt_view(cb)
        return
    name, base = ec.SHOP[key]
    await show(cb, f"🏆 <b>{rich(name)}</b>\nБазовая цена: {base} ♦️",
               kb([[("✏️ Название", f"a:ttn:{key}"), ("💰 Цена", f"a:ttp:{key}")],
                   [("🗑 Удалить", f"a:ttd:{key}"), ("◀️ Назад", "a:ttl")]]))


@panel.callback_query(F.data.startswith("a:ttd:"))
async def tt_delete(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key in ec.SHOP:
        name = ec.SHOP.pop(key)[0]
        await ec.save_shop()
        await log_mod(ADMIN_ID, 0, "title_shop_del", plain(name))
    await tt_view(cb)


@panel.callback_query(F.data.startswith("a:ttn:"))
async def tt_name_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.tt_edit_name)
    await state.update_data(key=cb.data.split(":")[2])
    await show(cb, "Новое название (эмодзи в начале, до 30 символов). Премиум-эмодзи сохранятся:", CANCEL)


@panel.message(S.tt_edit_name)
async def tt_name_save(message: Message, state: FSMContext):
    t = admin_text(message)
    key = (await state.get_data()).get("key")
    if not t or len(plain(t)) > 30 or key not in ec.SHOP:
        await message.answer("❌ Название до 30 символов.", reply_markup=CANCEL)
        return
    ec.SHOP[key] = (t, ec.SHOP[key][1])
    await ec.save_shop()
    await log_mod(ADMIN_ID, 0, "title_shop_edit", plain(t))
    await state.clear()
    await tt_view(message)


@panel.callback_query(F.data.startswith("a:ttp:"))
async def tt_price_ask(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.tt_edit_price)
    await state.update_data(key=cb.data.split(":")[2])
    await show(cb, "Новая базовая цена (число от 5 до 5000):", CANCEL)


@panel.message(S.tt_edit_price)
async def tt_price_save(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    key = (await state.get_data()).get("key")
    if not t.isdigit() or not 5 <= int(t) <= 5000 or key not in ec.SHOP:
        await message.answer("❌ Нужно число от 5 до 5000.", reply_markup=CANCEL)
        return
    ec.SHOP[key] = (ec.SHOP[key][0], int(t))
    await ec.save_shop()
    await log_mod(ADMIN_ID, 0, "title_shop_price", f"{key}: {t}")
    await state.clear()
    await tt_view(message)


@panel.callback_query(F.data == "a:tta")
async def tt_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.tt_name)
    await show(cb, "Название нового титула (эмодзи в начале, до 30 символов). Премиум-эмодзи сохранятся:", CANCEL)


@panel.message(S.tt_name)
async def tt_add_name(message: Message, state: FSMContext):
    t = admin_text(message)
    if not t or len(plain(t)) > 30:
        await message.answer("❌ Название до 30 символов.", reply_markup=CANCEL)
        return
    await state.update_data(name=t)
    await state.set_state(S.tt_price)
    await message.answer("Базовая цена (число от 5 до 5000):", reply_markup=CANCEL)


@panel.message(S.tt_price)
async def tt_add_price(message: Message, state: FSMContext):
    t = (message.text or "").strip()
    if not t.isdigit() or not 5 <= int(t) <= 5000:
        await message.answer("❌ Нужно число от 5 до 5000.", reply_markup=CANCEL)
        return
    name = (await state.get_data()).get("name")
    ec.SHOP[f"t_{secrets.token_hex(3)}"] = (name, int(t))
    await ec.save_shop()
    await log_mod(ADMIN_ID, 0, "title_shop_add", plain(name))
    await state.clear()
    await tt_view(message)
