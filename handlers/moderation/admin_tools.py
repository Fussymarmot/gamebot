import asyncio
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import CallbackQuery, ChatPermissions, Message

from config import ADMIN_ID, GROUP_ID
from database.core import q, x
from database.messages import flush_messages
from database.moderation import ban_user, is_user_banned, log_mod, unban_user
from database.settings import get_setting
from database.users import get_user_name
from services import mods, modtools
from utils.fmt import code, esc
from utils.logs import logger
from utils.targets import parse_target
from utils.timeutils import now, parse_ts, ts
from utils.ui import kb

router = Router()


def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id == ADMIN_ID)


async def warn_limit() -> int:
    return int(await get_setting("warn_limit", "3"))


async def warn_target(message: Message):
    if message.chat.id != GROUP_ID and message.chat.type != "private":
        return None
    uid = message.from_user.id if message.from_user else 0
    if uid != ADMIN_ID and not await mods.has(uid, "warn"):
        await message.answer("❌ Нет доступа")
        return None
    t = await parse_target(message)
    if not t or not t.id:
        cmd = (message.text or "/warn").split()[0].split("@")[0]
        await message.answer(f"❌ Формат: {code(cmd + ' @username причина')} или {code(cmd + ' 123456789 причина')}\n"
                             "Можно ответить командой на сообщение.")
        return None
    if t.id == ADMIN_ID or t.id == message.bot.id:
        await message.answer("❌ Это нельзя")
        return None
    if uid != ADMIN_ID and await mods.is_mod(t.id):
        await message.answer("❌ Это модератор")
        return None
    return t


@router.message(Command("warn"))
async def cmd_warn(message: Message):
    t = await warn_target(message)
    if not t:
        return
    if not t.rest:
        await message.answer("❌ Укажи причину предупреждения.")
        return
    text, muted = await modtools.warn(message.bot, message.from_user.id, t.id, t.name, t.rest)
    markup = modtools.appeal_kb(t.id) if muted else None
    if message.chat.id == GROUP_ID:
        await message.answer(text, reply_markup=markup)
        return
    try:
        await message.bot.send_message(GROUP_ID, text, reply_markup=markup)
    except TelegramAPIError:
        pass
    await message.answer(f"✅ Предупреждение выдано: {esc(t.name)}")


@router.message(Command("unwarn"))
async def cmd_unwarn(message: Message):
    t = await warn_target(message)
    if not t:
        return
    row = await q("SELECT id FROM warns WHERE user_id=? AND active=1 ORDER BY id DESC LIMIT 1", (t.id,), one=True)
    if not row:
        await message.answer("У этого участника нет активных предупреждений.")
        return
    await x("UPDATE warns SET active=0 WHERE id=?", (row[0],))
    await log_mod(message.from_user.id, t.id, "unwarn", "")
    await message.answer(f"✅ Последнее предупреждение у <b>{esc(t.name)}</b> снято.")


@router.message(Command("warns"))
async def cmd_warns(message: Message):
    t = await warn_target(message)
    if not t:
        return
    rows = await q("SELECT reason, created_at, active FROM warns WHERE user_id=? ORDER BY id DESC LIMIT 10", (t.id,))
    if not rows:
        await message.answer(f"У <b>{esc(t.name)}</b> нет предупреждений.")
        return
    text = f"⚠️ <b>Предупреждения: {esc(t.name)}</b>\n\n"
    for reason, at, active in rows:
        text += f"{'🔴' if active else '⚪'} {esc(reason)} <i>({parse_ts(at).strftime('%d.%m %H:%M')})</i>\n"
    await message.answer(text)


STATUS_RU = {"new": "новая", "review": "рассматривается", "resolved": "решена", "rejected": "отклонена"}


def report_kb(rid: int):
    return kb([[("🔎 Рассматривается", f"rp:{rid}:review"), ("✅ Решена", f"rp:{rid}:resolved")],
               [("❌ Отклонена", f"rp:{rid}:rejected")]])


def report_text(r) -> str:
    rid, rep, tid, tname, text, status, at = r
    return (f"🚩 <b>Жалоба #{rid}</b> ({STATUS_RU.get(status, status)})\n\n"
            f"На: <b>{esc(tname or tid)}</b> {code(str(tid))}\n"
            f"От: {code(str(rep))}\nВремя: {parse_ts(at).strftime('%d.%m %H:%M')}\n\n{esc(text)}")


@router.message(Command("report"))
async def cmd_report(message: Message):
    t = await parse_target(message)
    if not t or not t.rest:
        await message.answer(f"❌ Формат: {code('/report @username описание')} или {code('/report 123456789 описание')}")
        return
    uid = message.from_user.id
    recent = (await q("SELECT COUNT(*) FROM reports WHERE reporter_id=? AND created_at >= ?",
                      (uid, ts(now() - timedelta(minutes=10))), one=True))[0]
    if recent >= 3:
        await message.answer("⏳ Слишком много жалоб. Подожди немного.")
        return
    if t.id is None:
        await message.answer("❌ Я не знаю этого пользователя по username. Используй его ID "
                             "или ответь командой на его сообщение.")
        return
    rid = await x("INSERT INTO reports (reporter_id, target_id, target_name, text, status, created_at) "
                  "VALUES (?,?,?,?, 'new', ?)", (uid, t.id, t.name, t.rest[:500], ts()))
    r = (rid, uid, t.id, t.name, t.rest[:500], "new", ts())
    try:
        await message.bot.send_message(ADMIN_ID, report_text(r), reply_markup=report_kb(rid))
    except TelegramAPIError:
        pass
    try:
        await message.delete()
    except TelegramAPIError:
        pass
    note = await message.answer("✅ Жалоба отправлена администратору.")
    try:
        import asyncio
        await asyncio.sleep(5)
        await note.delete()
    except Exception:
        logger.debug("Не удалось удалить служебную заметку", exc_info=True)


@router.message(Command("reports"))
async def cmd_reports(message: Message):
    if not is_admin(message):
        return
    rows = await q("""SELECT id, reporter_id, target_id, target_name, text, status, created_at FROM reports
                      WHERE status IN ('new','review') ORDER BY id DESC LIMIT 5""")
    if not rows:
        await message.answer("🚩 Открытых жалоб нет.")
        return
    for r in rows:
        await message.answer(report_text(r), reply_markup=report_kb(r[0]))


@router.callback_query(F.data.startswith("rp:"))
async def report_cb(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, rid, status = cb.data.split(":")
    await x("UPDATE reports SET status=?, handled_by=?, handled_at=? WHERE id=?",
            (status, ADMIN_ID, ts(), int(rid)))
    await log_mod(ADMIN_ID, 0, "report", f"#{rid} -> {status}")
    r = await q("""SELECT id, reporter_id, target_id, target_name, text, status, created_at
                   FROM reports WHERE id=?""", (int(rid),), one=True)
    try:
        await cb.message.edit_text(report_text(r), reply_markup=report_kb(int(rid)) if status == "review" else None)
    except Exception:
        logger.debug("Не удалось обновить карточку жалобы %s", rid, exc_info=True)
    await cb.answer("Статус обновлён")


@router.message(Command("adminlog"))
async def cmd_adminlog(message: Message):
    if not is_admin(message):
        return
    rows = await q("SELECT action, target_id, details, created_at FROM mod_log ORDER BY id DESC LIMIT 15")
    if not rows:
        await message.answer("Журнал пуст.")
        return
    text = "📜 <b>Журнал действий</b>\n\n"
    for action, target, details, at in rows:
        who = await get_user_name(target) if target else ""
        text += (f"<code>{parse_ts(at).strftime('%d.%m %H:%M')}</code> {esc(action)}"
                 + (f" → {esc(who)}" if who else "") + (f": {esc(details)}" if details else "") + "\n")
    await message.answer(text)


CHUNK = 100
pending_clear = {}
clear_job = {"task": None, "stop": False}


async def delete_range(bot, upper: int, count: int, progress=None) -> int:
    sent, low = 0, max(0, upper - count)
    top = upper - 1
    while top > low and not clear_job["stop"]:
        ids = list(range(top, max(low, top - CHUNK), -1))
        try:
            await bot.delete_messages(GROUP_ID, ids)
        except TelegramRetryAfter as ex:
            await asyncio.sleep(ex.retry_after + 1)
            continue
        except TelegramAPIError:
            pass
        sent += len(ids)
        top -= CHUNK
        if progress:
            await progress(sent, count)
        await asyncio.sleep(0.35)
    return sent


async def delete_ids(bot, ids) -> int:
    done = 0
    for i in range(0, len(ids), CHUNK):
        part = ids[i:i + CHUNK]
        try:
            await bot.delete_messages(GROUP_ID, part)
            done += len(part)
        except TelegramAPIError:
            for mid in part:
                try:
                    await bot.delete_message(GROUP_ID, mid)
                    done += 1
                except TelegramAPIError:
                    pass
    return done


async def known_ids(n: int, user_id=None):
    await flush_messages()
    sql = "SELECT message_id FROM messages WHERE chat_id=? AND message_id > 0"
    params = [GROUP_ID]
    if user_id is not None:
        sql += " AND user_id=?"
        params.append(user_id)
    rows = await q(sql + " ORDER BY id DESC LIMIT ?", tuple(params + [n]))
    return [r[0] for r in rows]


@router.message(Command("clear"))
async def cmd_clear(message: Message):
    if message.chat.id != GROUP_ID:
        return
    if not is_admin(message):
        await message.answer("❌ Нет доступа")
        return
    parts = (message.text or "").split()
    usage = ("❌ Формат:\n<code>/clear 50</code>: последние 50 сообщений чата, в том числе бота\n"
             "<code>/clear all</code>: вообще все сообщения чата\n<code>/clear bot 30</code>: только сообщения бота\n"
             "<code>/clear 20 @username</code> или <code>/clear 20 123456789</code>: последние 20 сообщений человека")
    if len(parts) < 2:
        await message.answer(usage)
        return
    mode, n, uid, who = parts[1].lower(), 0, None, ""
    if mode == "all":
        pass
    elif mode == "bot":
        n = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 50
    elif mode.isdigit():
        n = int(mode)
        mode = "range"
        if len(parts) > 2:
            t = await parse_target(message, skip=1)
            if not t or not t.id:
                await message.answer("❌ Не нашёл такого пользователя. Укажи @username или ID.")
                return
            uid, who, mode = t.id, t.name, "user"
    else:
        await message.answer(usage)
        return
    if mode != "all" and not 1 <= n <= 1000:
        await message.answer("❌ Число должно быть от 1 до 1000.")
        return
    token = str(message.message_id)
    pending_clear[token] = {"mode": mode, "n": n, "uid": uid, "cmd": message.message_id}
    what = {"all": "<b>ВСЕ сообщения чата</b>, включая сообщения бота и закреплённые",
            "bot": f"последние <b>{n}</b> сообщений бота", "user": f"последние <b>{n}</b> сообщений: {esc(who)}",
            "range": f"последние <b>{n}</b> сообщений чата (людей и бота)"}[mode]
    warn = "\n\n⚠️ Это может занять несколько минут и не отменяется." if mode == "all" else ""
    await message.answer(f"🗑 Удалить {what}?{warn}",
                         reply_markup=kb([[("✅ Да, удалить", f"clr:{token}:y"), ("❌ Отмена", f"clr:{token}:n")]]))


@router.callback_query(F.data.startswith("clr:"))
async def clear_cb(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID:
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, token, ans = cb.data.split(":")
    if token == "stop":
        clear_job["stop"] = True
        await cb.answer("Останавливаю...")
        return
    job = pending_clear.pop(token, None)
    if ans == "n" or not job:
        await cb.message.edit_text("Отменено." if ans == "n" else "Запрос устарел, повтори команду.")
        await cb.answer()
        return
    await cb.answer("Удаляю...")
    bot, mode, n = cb.bot, job["mode"], job["n"]
    upper = cb.message.message_id
    clear_job["stop"] = False
    stop_kb = kb([[("⛔ Остановить", "clr:stop:x")]])
    if mode in ("range", "all"):
        count = upper if mode == "all" else n + 1
        await cb.message.edit_text("🗑 Удаляю сообщения...", reply_markup=stop_kb)
        state = {"last": 0}

        async def progress(done, total):
            if done - state["last"] >= 1000:
                state["last"] = done
                try:
                    await cb.message.edit_text(f"🗑 Обработано номеров: {done} из {total}", reply_markup=stop_kb)
                except TelegramAPIError:
                    pass

        sent = await delete_range(bot, upper, count, progress)
        result = f"обработано номеров сообщений: {sent}"
    else:
        ids = await known_ids(n, 0 if mode == "bot" else job["uid"])
        sent = await delete_ids(bot, ids)
        result = f"удалено сообщений: {sent}"
    await log_mod(ADMIN_ID, job["uid"] or 0, "clear", f"{mode} {n}: {result}")
    try:
        await bot.delete_message(GROUP_ID, job["cmd"])
    except TelegramAPIError:
        pass
    done_text = f"🗑 Готово: {result}." + (" Остановлено." if clear_job["stop"] else "")
    try:
        await cb.message.edit_text(done_text)
    except TelegramAPIError:
        pass
    await asyncio.sleep(6)
    try:
        await cb.message.delete()
    except TelegramAPIError:
        pass


@router.message(Command("gameban"))
async def cmd_gameban(message: Message):
    if not is_admin(message):
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    if not t or not t.id:
        await message.answer(f"❌ Формат: {code('/gameban @username')} или {code('/gameban 123456789')}\n"
                             "Можно ответить командой на сообщение.")
        return
    if t.id == ADMIN_ID:
        await message.answer("❌ Это админ бота.")
        return
    if await is_user_banned(t.id):
        await message.answer(f"{esc(t.name)} уже в чёрном списке игр.")
        return
    await ban_user(t.id)
    await log_mod(ADMIN_ID, t.id, "game_ban", t.rest[:100])
    await message.answer(f"⛔ <b>{esc(t.name)}</b> больше не может играть.")


@router.message(Command("gameunban"))
async def cmd_gameunban(message: Message):
    if not is_admin(message):
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    if not t or not t.id:
        await message.answer(f"❌ Формат: {code('/gameunban @username')} или {code('/gameunban 123456789')}")
        return
    if not await is_user_banned(t.id):
        await message.answer(f"{esc(t.name)} не в чёрном списке игр.")
        return
    await unban_user(t.id)
    await log_mod(ADMIN_ID, t.id, "game_unban", "")
    await message.answer(f"✅ <b>{esc(t.name)}</b> снова может играть.")
