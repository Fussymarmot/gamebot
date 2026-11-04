from datetime import timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.core import q, x
from database.moderation import log_mod
from services import economy as ec, giveaways as gw, shop as shop_core, tags
from utils import titles
from utils.fmt import esc, plain, rich
from utils.timeutils import now, parse_ts, ts
from utils.ui import kb

router = Router()


async def title_cooldown(uid: int):
    row = await q("SELECT title_at FROM wallets WHERE user_id=?", (uid,), one=True)
    if row and row[0]:
        left = parse_ts(row[0]) + timedelta(hours=24) - now()
        if left.total_seconds() > 0:
            hours = int(left.total_seconds() // 3600) + 1
            return f"Звание можно менять раз в сутки. Попробуй через {hours} ч."
    return None


async def apply_title(uid: int, text: str):
    await ec.ensure(uid)
    await x("UPDATE wallets SET title=?, title_at=? WHERE user_id=?", (text, ts(), uid))


async def submit_custom_title(bot, uid: int, name: str, text: str):
    ok, reason, clean = titles.check(text)
    if not ok:
        return False, reason
    wait = await title_cooldown(uid)
    if wait:
        return False, wait
    if await titles.premoderation():
        oid = await shop_core.create_order(uid, "title", clean, clean, 0)
        await bot.send_message(ADMIN_ID, f"🏷 <b>Звание на проверку</b>\n\n{esc(name)}: «{esc(clean)}»",
                               reply_markup=kb([[("✅ Одобрить", f"ord:{oid}:ok"), ("❌ Отклонить", f"ord:{oid}:no")]]))
        return True, "Звание отправлено админу на проверку."
    await apply_title(uid, clean)
    await bot.send_message(ADMIN_ID, f"🏷 {esc(name)} поставил(а) себе звание «{esc(clean)}»",
                           reply_markup=kb([[("🗑 Убрать звание", f"ttlrm:{uid}")]]))
    return True, f"Звание установлено: {clean}"


@router.message(Command("title"))
async def cmd_title(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer(f"Напиши свое звание: <code>/title Мое звание</code> (до {titles.MAX_LEN} символов).\n"
                             "Готовые звания можно выбрать в /profile.")
        return
    ok, text = await submit_custom_title(message.bot, message.from_user.id,
                                         message.from_user.first_name or "Игрок", parts[1])
    await message.answer(("✅ " if ok else "❌ ") + esc(text))


@router.callback_query(F.data.startswith("ttl:"))
async def title_cb(cb: CallbackQuery):
    _, owner, act, *rest = cb.data.split(":")
    uid = cb.from_user.id
    if uid != int(owner):
        await cb.answer("Это профиль другого игрока.", show_alert=True)
        return
    if act == "list":
        presets = await titles.presets()
        mine = [k for k in await ec.owned(uid) if k in ec.SHOP]
        rows = [[(p, f"ttl:{uid}:p:{i}")] for i, p in enumerate(presets)]
        rows += [[(plain(ec.SHOP[k][0]), f"ttl:{uid}:s:{k}")] for k in mine]
        rows.append([("🗑 Снять звание", f"ttl:{uid}:off")])
        await cb.message.answer("🏷 <b>Выбери звание</b>\n<i>Своё: /title текст</i>", reply_markup=kb(rows))
        await cb.answer()
        return
    if act == "off":
        await x("UPDATE wallets SET title=NULL WHERE user_id=?", (uid,))
        await cb.answer("Звание снято", show_alert=True)
        return
    wait = await title_cooldown(uid)
    if wait:
        await cb.answer(wait, show_alert=True)
        return
    if act == "p":
        presets = await titles.presets()
        text = presets[int(rest[0])]
    else:
        if rest[0] not in ec.SHOP:
            await cb.answer("Этого титула больше нет.", show_alert=True)
            return
        text = plain(ec.SHOP[rest[0]][0])
        if rest[0] not in await ec.owned(uid):
            await cb.answer("Этот титул не куплен.", show_alert=True)
            return
    await apply_title(uid, text)
    await cb.answer(f"Звание: {text}", show_alert=True)


@router.callback_query(F.data.startswith("ttlrm:"))
async def title_remove(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    uid = int(cb.data.split(":")[1])
    await x("UPDATE wallets SET title=NULL WHERE user_id=?", (uid,))
    await log_mod(ADMIN_ID, uid, "title_remove", "")
    await cb.message.edit_text(cb.message.html_text + "\n\n<b>Звание убрано.</b>")
    await cb.answer()


@router.message(Command("mytag"))
async def cmd_mytag(message: Message):
    parts = (message.text or "").split(maxsplit=1)
    uid = message.from_user.id
    cost = ec.price(ec.TAG_PRICE)
    if len(parts) < 2:
        await message.answer(f"🏷 Тег рядом с именем на {ec.TAG_DAYS} дней стоит <b>{cost}</b> ♦️.\n"
                             "Напиши: <code>/mytag текст</code> (до 16 символов). Админ проверит заказ, "
                             "при отказе рубины вернутся.")
        return
    ok, reason, clean = titles.check(parts[1], titles.TAG_MAX_LEN)
    if not ok:
        await message.answer("❌ " + esc(reason))
        return
    if (await ec.wallet(uid))[0] < cost:
        await message.answer(f"❌ Не хватает рубинов: нужно {cost}.")
        return
    oid = await shop_core.create_order(uid, "tag", f"Тег «{clean}»", clean, cost, shop_core.tag_expiry())
    name = message.from_user.first_name or "Игрок"
    await message.bot.send_message(
        ADMIN_ID, f"🏷 <b>Заказ тега #{oid}</b>\n\n{esc(name)}: «{esc(clean)}» на {ec.TAG_DAYS} дней",
        reply_markup=kb([[("✅ Одобрить", f"ord:{oid}:ok"), ("❌ Отклонить", f"ord:{oid}:no")]]))
    await message.answer(f"✅ Заказ #{oid} отправлен админу. Списано {cost} ♦️, при отказе вернём.")


@router.callback_query(F.data.startswith("ord:"))
async def order_cb(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, oid, act = cb.data.split(":")
    oid = int(oid)
    approve = act == "ok"
    order = await shop_core.get_order(oid)
    if not order or order[6] != "new":
        await cb.answer("Заказ уже обработан", show_alert=True)
        return
    _, uid, kind, title, payload, cost, _ = order
    note = ""
    if approve and kind == "tag":
        try:
            await tags.set_member_tag(cb.bot, uid, payload)
        except Exception as ex:
            approve = False
            note = f" (не удалось поставить тег: {ex})"
    res = await shop_core.settle(oid, approve)
    if approve and kind == "title":
        await apply_title(uid, payload)
    name = await ec.name_of(uid)
    verdict = "подтверждён" if approve else "отклонён"
    await log_mod(ADMIN_ID, uid, "order", f"#{oid} {kind} {verdict}")
    try:
        await cb.bot.send_message(GROUP_ID, f"{'✅' if approve else '❌'} {name}: заказ «{rich(title)}» {verdict}."
                                            + ("" if approve or not cost else f" Рубины ({cost}) возвращены."))
    except TelegramAPIError:
        pass
    await cb.message.edit_text(cb.message.html_text + f"\n\n<b>Заказ {verdict}.</b>{esc(note)}")
    await cb.answer()


async def shop_main(uid: int):
    coins, _, title, _ = await ec.wallet(uid)
    text = (f"🛍 <b>Магазин</b>\n\nУ тебя: <b>{coins}</b> ♦️\nЗвание: <b>{esc(title) if title else '—'}</b>")
    rows = [[("🏷 Титулы", f"shop:{uid}:titles"), ("🎟 Билеты", f"shop:{uid}:tickets")],
            [("📌 Тег на 7 дней", f"shop:{uid}:tag"), ("🎁 Призы", f"shop:{uid}:prizes")]]
    return text, kb(rows)


async def shop_titles(uid: int):
    coins = (await ec.wallet(uid))[0]
    have = set(await ec.owned(uid))
    text = f"🏷 <b>Титулы</b>\nУ тебя: <b>{coins}</b> ♦️\n\n"
    rows = []
    for key, (name, base) in ec.SHOP.items():
        text += f"• {rich(name)}: {ec.price(base)} ♦️" + (" <i>(куплено)</i>" if key in have else "") + "\n"
        label = plain(name)
        rows.append([(f"Надеть {label}" if key in have else f"Купить {label}", f"shop:{uid}:{'eq' if key in have else 'buy'}:{key}")])
    rows.append([("🗑 Снять титул", f"shop:{uid}:off:-"), ("◀️ Назад", f"shop:{uid}:main")])
    return text, kb(rows)


async def shop_tickets(uid: int):
    price = ec.price(ec.TICKET_PRICE)
    text = (f"🎟 <b>Билеты в розыгрыши</b>\n\nДополнительный билет повышает шанс выиграть. Цена: <b>{price}</b> ♦️, "
            f"до {shop_core.TICKET_MAX} билетов на розыгрыш. Сначала нужно участвовать в розыгрыше.\n\n")
    rows = []
    active = [g for g in await gw.active_list() if g["kind"] == "draw"]
    for g in active:
        have = await shop_core.tickets_of(g["id"], uid)
        text += f"• {rich(g['title'])}: билетов у тебя {have}\n"
        rows.append([(f"🎟 +1 билет: {plain(g['title'])[:22]}", f"shop:{uid}:tk:{g['id']}")])
    if not active:
        text += "<i>Сейчас нет розыгрышей с жеребьёвкой.</i>"
    rows.append([("◀️ Назад", f"shop:{uid}:main")])
    return text, kb(rows)


async def shop_tag(uid: int):
    price = ec.price(ec.TAG_PRICE)
    text = (f"📌 <b>Тег рядом с именем</b>\n\nНа {ec.TAG_DAYS} дней, цена <b>{price}</b> ♦️. Команда: "
            "<code>/mytag текст</code> (до 16 символов). Админ проверит заказ, при отказе рубины вернутся.")
    return text, kb([[("◀️ Назад", f"shop:{uid}:main")]])


async def shop_prizes(uid: int):
    items = await q("SELECT id, title, price, stock FROM shop_items WHERE active=1 AND stock != 0 ORDER BY id")
    text = "🎁 <b>Призы от админа</b>\n\n"
    rows = []
    for item_id, title, base, stock in items:
        left = "" if stock < 0 else f", осталось {stock}"
        text += f"• {rich(title)}: {ec.price(base)} ♦️{left}\n"
        rows.append([(f"Купить: {plain(title)[:26]}", f"shop:{uid}:pz:{item_id}")])
    if not items:
        text += "<i>Пока нет призов. Админ может добавить их в панели.</i>"
    rows.append([("◀️ Назад", f"shop:{uid}:main")])
    return text, kb(rows)


SCREENS = {"main": shop_main, "titles": shop_titles, "tickets": shop_tickets, "tag": shop_tag, "prizes": shop_prizes}


@router.message(Command("shop"))
async def cmd_shop(message: Message):
    text, markup = await shop_main(message.from_user.id)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("shop:"))
async def shop_cb(cb: CallbackQuery):
    _, owner, act, *rest = cb.data.split(":")
    uid = cb.from_user.id
    if uid != int(owner):
        await cb.answer("Это магазин другого игрока. Открой свой: /shop", show_alert=True)
        return
    alert, screen = None, act
    if act == "buy":
        ok, alert = await ec.buy(uid, rest[0])
        screen = "titles"
    elif act == "eq":
        ok, alert = await ec.equip(uid, rest[0])
        screen = "titles"
    elif act == "off":
        ok, alert = await ec.equip(uid, None)
        screen = "titles"
    elif act == "tk":
        ok, alert = await shop_core.buy_ticket(uid, int(rest[0]))
        screen = "tickets"
    elif act == "pz":
        ok, alert, oid = await shop_core.buy_prize(uid, int(rest[0]))
        screen = "prizes"
        if ok:
            await cb.bot.send_message(
                ADMIN_ID, f"🎁 <b>Заказ приза #{oid}</b>\n\n{esc(cb.from_user.first_name or 'Игрок')}: {rich((await shop_core.get_order(oid))[3])}",
                reply_markup=kb([[("✅ Выдано", f"ord:{oid}:ok"), ("❌ Отклонить", f"ord:{oid}:no")]]))
    text, markup = await SCREENS[screen](uid)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except Exception:
        pass
    await cb.answer(alert, show_alert=bool(alert) and ("не" in alert.lower() or "нужно" in alert.lower()))
