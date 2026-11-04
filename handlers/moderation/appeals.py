
from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.types import CallbackQuery

from config import ADMIN_ID, GROUP_ID
from database.core import q, x
from database.moderation import log_mod
from services import modtools
from utils.fmt import esc
from utils.timeutils import parse_ts, ts
from utils.ui import kb

router = Router()


@router.callback_query(F.data.startswith("ap:"))
async def appeal_cb(cb: CallbackQuery):
    uid = int(cb.data.split(":")[1])
    if cb.from_user.id != uid:
        await cb.answer("Обжаловать может только тот, кого замутили", show_alert=True)
        return
    if not await q("SELECT 1 FROM mutes WHERE user_id=?", (uid,), one=True):
        await cb.answer("Мут уже снят", show_alert=True)
        return
    if await q("SELECT 1 FROM appeals WHERE user_id=? AND status='new'", (uid,), one=True):
        await cb.answer("Твоя жалоба уже отправлена админу", show_alert=True)
        return
    aid = await x("INSERT INTO appeals (user_id, status, created_at) VALUES (?, 'new', ?)", (uid, ts()))
    last = await q("SELECT admin_id, action, details, created_at FROM mod_log WHERE target_id=? "
                   "AND action IN ('mute') ORDER BY id DESC LIMIT 1", (uid,), one=True)
    info = ""
    if last:
        who = await modtools.name_of(last[0])
        info = (f"\nМут: {esc(last[2] or '')} от {esc(who)}, "
                f"{parse_ts(last[3]).strftime('%d.%m %H:%M')}")
    name = await modtools.name_of(uid)
    try:
        await cb.bot.send_message(
            ADMIN_ID, f"📝 <b>Обжалование мута #{aid}</b>\n\n{esc(name)} <code>{uid}</code>{info}",
            reply_markup=kb([[("🔊 Снять мут", f"apd:{aid}:y"), ("❌ Отклонить", f"apd:{aid}:n")]]))
    except TelegramAPIError:
        await cb.answer("Не получилось отправить жалобу", show_alert=True)
        return
    await cb.answer("Жалоба отправлена админу", show_alert=True)


@router.callback_query(F.data.startswith("apd:"))
async def appeal_decision(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, aid, act = cb.data.split(":")
    row = await q("SELECT user_id, status FROM appeals WHERE id=?", (int(aid),), one=True)
    if not row or row[1] != "new":
        await cb.answer("Уже обработано", show_alert=True)
        return
    uid = row[0]
    name = await modtools.name_of(uid)
    if act == "y":
        err = await modtools.unmute(cb.bot, ADMIN_ID, uid)
        if err:
            await cb.answer(f"Не получилось: {err}"[:190], show_alert=True)
            return
        await x("UPDATE appeals SET status='approved', handled_at=? WHERE id=?", (ts(), int(aid)))
        verdict, note = "мут снят", "🔊 Твоя жалоба одобрена, мут снят."
    else:
        await x("UPDATE appeals SET status='rejected', handled_at=? WHERE id=?", (ts(), int(aid)))
        verdict, note = "отклонено", "❌ Твоя жалоба отклонена."
    await log_mod(ADMIN_ID, uid, "appeal", f"#{aid} {verdict}")
    try:
        await cb.bot.send_message(uid, note)
    except TelegramAPIError:
        if act == "y":
            try:
                await cb.bot.send_message(GROUP_ID, f"🔊 {esc(name)}: мут снят по жалобе.")
            except TelegramAPIError:
                pass
    await cb.message.edit_text(cb.message.html_text + f"\n\n<b>Решение: {verdict}.</b>")
    await cb.answer()
