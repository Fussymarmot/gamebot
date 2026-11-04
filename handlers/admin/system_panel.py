import glob
import os
import time
from datetime import timedelta

from aiogram import F
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, ChatPermissions, Message

from config import ADMIN_ID, GROUP_ID
from database import lifecycle
from database.core import DB_PATH, q, x
from database.moderation import ban_user, log_mod, unban_user
from database.settings import get_setting, set_setting
from database.users import count_members, get_user_name
from games import core as games_core
from handlers.admin.common import BACK, CANCEL, norm_time, panel, parse_when, S, show
from services import backup, maintenance, members, tags
from services.commands import set_commands
from services.scheduler import schedule_maint_end, unschedule_maint_end
from utils import ui
from utils.emoji import e
from utils.fmt import code, esc
from utils.targets import parse_free
from utils.timeutils import now, parse_ts, ts, TZ
from utils.ui import kb

START_TIME = time.time()


@panel.callback_query(F.data == "a:st")
async def st_view(cb: CallbackQuery):
    msgs = (await q("SELECT COUNT(*) FROM messages", one=True))[0]
    users = (await q("SELECT COUNT(*) FROM users", one=True))[0]
    games = (await q("SELECT COALESCE(SUM(wins),0) FROM game_stats", one=True))[0]
    mafia = (await q("SELECT COUNT(*) FROM mafia_games", one=True))[0]
    text = (f"{e('chart')} <b>Статистика</b>\n\nСообщений: {code(str(msgs))}\n"
            f"Пользователей: {code(str(users))}\nСыграно игр на двоих: {code(str(games))}\n"
            f"Партий мафии: {code(str(mafia))}")
    await show(cb, text, kb([[("🗑 Сбросить за сутки", "a:str:1"), ("за неделю", "a:str:7")],
                              [("за месяц", "a:str:30"), ("Всю", "a:str:all")], BACK]))


@panel.callback_query(F.data.startswith("a:str:"))
async def st_reset(cb: CallbackQuery):
    parts = cb.data.split(":")
    p = parts[2]
    if len(parts) > 3:
        if p == "all":
            await x("DELETE FROM messages")
        else:
            await x("DELETE FROM messages WHERE created_at >= ?", (ts(now() - timedelta(days=int(p))),))
        await st_view(cb)
        return
    name = "всю статистику" if p == "all" else f"сообщения за последние {p} дн."
    await show(cb, f"Точно удалить {name}?", kb([[("✅ Да", f"a:str:{p}:y"), ("❌ Нет", "a:st")]]))


async def qt_view(target):
    on = await get_setting("quiet_enabled", "1") == "1"
    s = int(await get_setting("quiet_start", "3"))
    en = int(await get_setting("quiet_end", "5"))
    await show(target, f"{e('moon')} <b>Тихое время</b>\n\nСтатус: <b>{'включено' if on else 'выключено'}</b>\n"
                       f"С {code(f'{s:02d}:00')} до {code(f'{en:02d}:00')} по Москве\n\n"
                       "<i>В это время бот не пишет в группу сам и не отвечает на команды.</i>",
               kb([[("Выключить" if on else "Включить", "a:qtt")],
                   [("Начало", "a:qth:s"), ("Конец", "a:qth:e")], BACK]))


@panel.callback_query(F.data == "a:qt")
async def qt_cb(cb: CallbackQuery):
    await qt_view(cb)


@panel.callback_query(F.data == "a:qtt")
async def qt_toggle(cb: CallbackQuery):
    on = await get_setting("quiet_enabled", "1") == "1"
    await set_setting("quiet_enabled", "0" if on else "1")
    await qt_view(cb)


@panel.callback_query(F.data.startswith("a:qth:"))
async def qt_hour(cb: CallbackQuery):
    parts = cb.data.split(":")
    which = parts[2]
    if len(parts) > 3:
        await set_setting("quiet_start" if which == "s" else "quiet_end", parts[3])
        await qt_view(cb)
        return
    hours = [(f"{h:02d}", f"a:qth:{which}:{h}") for h in range(24)]
    rows = [hours[i:i + 6] for i in range(0, 24, 6)]
    await show(cb, "Выбери час (по Москве):", kb(rows + [[("◀️ Назад", "a:qt")]]))


async def ban_view(target):
    rows_db = await q("SELECT user_id FROM banned")
    text = "🚫 <b>Бан в играх</b>\n\n"
    rows = []
    for (uid,) in rows_db:
        n = await get_user_name(uid)
        text += f"• {esc(n)}\n"
        rows.append([(f"✅ Разбанить {n[:20]}", f"a:bu:{uid}")])
    if not rows_db:
        text += "<i>Чёрный список пуст</i>"
    rows += [[("➕ Добавить", "a:badd2")], BACK]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:ban")
async def ban_cb(cb: CallbackQuery):
    await ban_view(cb)


@panel.callback_query(F.data.startswith("a:bu:"))
async def ban_remove(cb: CallbackQuery):
    await unban_user(int(cb.data.split(":")[2]))
    await ban_view(cb)


@panel.callback_query(F.data == "a:badd2")
async def ban_add(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.ban_user)
    await show(cb, "Пришли @username или ID участника. Если username нет, можно переслать его сообщение "
                   "или выбрать его через упоминание.", CANCEL)


@panel.message(S.ban_user)
async def ban_add_user(message: Message, state: FSMContext):
    t = await parse_free(message)
    if not t or not t.id:
        await message.answer("❌ Не знаю такого пользователя. Пришли его ID.", reply_markup=CANCEL)
        return
    if t.id == ADMIN_ID:
        await message.answer("❌ Это админ бота.", reply_markup=CANCEL)
        return
    await ban_user(t.id)
    await log_mod(ADMIN_ID, t.id, "game_ban", "")
    await state.clear()
    await ban_view(message)


@panel.callback_query(F.data == "a:mod")
async def mod_view(cb: CallbackQuery):
    await x("DELETE FROM mutes WHERE until_date < ?", (ts(),))
    mutes = await q("SELECT user_id, until_date FROM mutes")
    ptags = await q("SELECT user_id, tag FROM perm_tags WHERE chat_id=?", (GROUP_ID,))
    log = await q("SELECT action, target_id, created_at FROM mod_log ORDER BY id DESC LIMIT 5")
    text = f"{e('lock')} <b>Модерация</b>\n\n<b>Муты:</b>\n"
    rows = []
    for uid, until in mutes:
        n = await get_user_name(uid)
        text += f"• {esc(n)} до {code(parse_ts(until).strftime('%H:%M'))}\n"
        rows.append([(f"🔊 Снять мут: {n[:18]}", f"a:mu:{uid}")])
    if not mutes:
        text += "<i>нет</i>\n"
    text += "\n<b>Вечные теги:</b>\n"
    for uid, tag in ptags:
        n = await get_user_name(uid)
        text += f"• {esc(n)}: {code(tag)}\n"
        rows.append([(f"🗑 Снять тег: {n[:18]}", f"a:tu:{uid}")])
    if not ptags:
        text += "<i>нет</i>\n"
    if log:
        text += "\n<b>Журнал:</b>\n"
        for a, t, c in log:
            text += f"• {esc(a)} → {esc(await get_user_name(t))} ({parse_ts(c).strftime('%d.%m %H:%M')})\n"
    await show(cb, text, kb(rows + [BACK]))


@panel.callback_query(F.data.startswith("a:mu:"))
async def mod_unmute(cb: CallbackQuery):
    from aiogram.types import ChatPermissions
    uid = int(cb.data.split(":")[2])
    try:
        chat = await cb.bot.get_chat(GROUP_ID)
        perms = chat.permissions or ChatPermissions(can_send_messages=True, can_send_other_messages=True,
                                                    can_add_web_page_previews=True)
        await cb.bot.restrict_chat_member(GROUP_ID, uid, permissions=perms)
    except Exception as ex:
        await cb.answer(f"Ошибка: {ex}", show_alert=True)
        return
    await x("DELETE FROM mutes WHERE user_id=?", (uid,))
    await log_mod(ADMIN_ID, uid, "unmute", "панель")
    await mod_view(cb)


@panel.callback_query(F.data.startswith("a:tu:"))
async def mod_untag(cb: CallbackQuery):
    uid = int(cb.data.split(":")[2])
    await x("DELETE FROM perm_tags WHERE user_id=? AND chat_id=?", (uid, GROUP_ID))
    tags.perm_cache.pop(uid, None)
    try:
        await tags.set_member_tag(cb.bot, uid, None)
    except Exception:
        pass
    await log_mod(ADMIN_ID, uid, "untag", "панель")
    await mod_view(cb)


async def mt_view(target):
    if maintenance.is_on():
        text = (f"🛠 <b>Технические работы</b>\n\nСтатус: <b>идут</b>\nДо: <b>{maintenance.end_dt().strftime('%d.%m %H:%M')}</b>"
                + (f"\nКомментарий: {esc(maintenance.comment())}" if maintenance.comment() else "")
                + "\n\n<i>Бот отвечает только тебе.</i>")
        rows = [[("✅ Завершить сейчас", "a:mtoff")], [("✏️ Изменить", "a:mton")], BACK]
    else:
        text = ("🛠 <b>Технические работы</b>\n\nСтатус: <b>выключены</b>\n\n"
                "<i>При включении бот объявит об этом в группе и перестанет отвечать всем, кроме тебя. "
                "Нужно указать дату окончания, комментарий по желанию.</i>")
        rows = [[("🛠 Включить", "a:mton")], BACK]
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:mt")
async def mt_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await mt_view(cb)


@panel.callback_query(F.data == "a:mton")
async def mt_on(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.maint_end)
    await show(cb, "Когда закончатся работы? <b>(обязательно)</b>\nФормат: "
                   f"{code('25.12.2026 18:30')}, {code('25.12 18:30')} или {code('18:30')} "
                   "(сегодня или завтра).", CANCEL)


@panel.message(S.maint_end)
async def mt_end(message: Message, state: FSMContext):
    d = parse_when(message.text)
    if not d or d <= now():
        await message.answer("❌ Не понял дату или она уже прошла. Пример: " + code("25.12 18:30"),
                             reply_markup=CANCEL)
        return
    await state.update_data(end=ts(d))
    await state.set_state(S.maint_comment)
    await message.answer("Комментарий для участников (или «-», если не нужен):", reply_markup=CANCEL)


@panel.message(S.maint_comment)
async def mt_comment(message: Message, state: FSMContext):
    note = (message.text or "").strip()
    note = "" if note == "-" else note[:300]
    await state.update_data(note=note)
    d = await state.get_data()
    end = parse_ts(d["end"])
    await message.answer(f"Включить тех. работы до <b>{end.strftime('%d.%m %H:%M')}</b>?"
                         + (f"\nКомментарий: {esc(note)}" if note else ""),
                         reply_markup=kb([[("✅ Включить", "a:mtok"), ("❌ Отмена", "a:cancel")]]))


@panel.callback_query(F.data == "a:mtok", S.maint_comment)
async def mt_confirm(cb: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    end = parse_ts(d["end"])
    await maintenance.enable(end, d.get("note", ""))
    schedule_maint_end(end)
    await state.clear()
    for chat_id in list(games_core.registry):
        await games_core.stop_active(cb.bot, chat_id, "Начались технические работы.")
    try:
        await cb.bot.send_message(GROUP_ID, maintenance.notice())
    except Exception:
        pass
    await mt_view(cb)


@panel.callback_query(F.data == "a:mtoff")
async def mt_off(cb: CallbackQuery):
    await maintenance.disable()
    unschedule_maint_end()
    try:
        await cb.bot.send_message(GROUP_ID, "✅ <b>Технические работы завершены.</b>\nБот снова работает!")
    except Exception:
        pass
    await mt_view(cb)


@panel.callback_query(F.data == "a:bot")
async def bot_view(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    up = int(time.time() - START_TIME)
    d, h, m = up // 86400, up % 86400 // 3600, up % 3600 // 60
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    msgs = (await q("SELECT COUNT(*) FROM messages", one=True))[0]
    backups = len(glob.glob(os.path.join(backup.BACKUP_DIR, "bot_backup_*")))
    text = (f"{e('gear')} <b>Бот</b>\n\nАптайм: {code(f'{d}д {h}ч {m}м')}\n"
            f"База данных: {code(f'{size:.0f} КБ')}\nСообщений в базе: {code(str(msgs))}\n"
            f"Участников группы в базе: {code(str(await count_members()))}\n"
            f"Резервных копий: {code(str(backups))} (автобэкап в 04:10)\n"
            f"Иконки на кнопках: <b>{'вкл' if ui.ICONS_ENABLED else 'выкл'}</b>\n"
            f"Группа: {code(str(GROUP_ID))}\nАдмин: {code(str(ADMIN_ID))}\n\n"
            "<i>Токен, ID группы и ID админа меняются в файле .env</i>")
    await show(cb, text, kb([[("💾 Бэкап сейчас", "a:bkp"), ("♻️ Восстановить", "a:rst")],
                              [("🎨 Иконки кнопок вкл/выкл", "a:ico")],
                              [("📈 Диагностика", "a:diag")],
                              [("🔄 Обновить команды", "a:botcmd")],
                              [("👥 Сканировать участников", "a:botscan")], BACK]))


def memory_mb() -> float:
    try:
        with open("/proc/self/status", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except Exception:
        return 0.0


@panel.callback_query(F.data == "a:diag")
async def bot_diag(cb: CallbackQuery):
    import platform
    from services.scheduler import scheduler
    rows = {}
    for table in ("messages", "users", "game_log", "wallets", "orders", "giveaways"):
        rows[table] = (await q(f"SELECT COUNT(*) FROM {table}", one=True))[0]
    size = os.path.getsize(DB_PATH) / 1024 if os.path.exists(DB_PATH) else 0
    text = (f"📈 <b>Диагностика</b>\n\nПамять процесса: {code(f'{memory_mb():.0f} МБ')}\n"
            f"База: {code(f'{size:.0f} КБ')}\nЗадач в планировщике: {code(str(len(scheduler.get_jobs())))}\n"
            f"Активных игр: {code(str(len(games_core.registry)))}\nPython: {code(platform.python_version())}\n\n"
            "<b>Строк в таблицах:</b>\n" + "\n".join(f"{k}: {code(str(v))}" for k, v in rows.items()))
    await show(cb, text, kb([[("🧹 Обслужить базу", "a:dbopt")], [("◀️ Назад", "a:bot")]]))


@panel.callback_query(F.data == "a:dbopt")
async def bot_dbopt(cb: CallbackQuery):
    await lifecycle.maintenance(vacuum=True)
    await cb.answer("База обслужена", show_alert=True)


@panel.callback_query(F.data == "a:botcmd")
async def bot_cmd(cb: CallbackQuery):
    ok = await set_commands(cb.bot)
    await cb.answer("Команды обновлены" if ok else "Не получилось, смотри bot.log", show_alert=True)


@panel.callback_query(F.data == "a:botscan")
async def bot_scan(cb: CallbackQuery):
    await cb.answer("Сканирую...")
    checked, left, inbase, total = await members.refresh_members(cb.bot)
    await cb.message.answer(f"👥 Проверено: {checked}\nВышли из группы: {left}\n"
                            f"В базе участников: {inbase}\nВсего в группе: {total if total else '?'}")


@panel.callback_query(F.data == "a:bkp")
async def bot_backup(cb: CallbackQuery):
    await cb.answer("Делаю бэкап...")
    ok = await backup.send_backup(cb.bot, "по запросу")
    if not ok:
        await cb.message.answer("❌ Не получилось отправить бэкап, смотри bot.log")


@panel.callback_query(F.data == "a:ico")
async def bot_icons(cb: CallbackQuery):
    if ui.ICONS_ENABLED:
        await set_setting("button_icons", "0")
        ui.set_icons(False)
        await cb.answer("Иконки на кнопках выключены", show_alert=True)
    else:
        await set_setting("button_icons", "1")
        ok = await ui.probe_icons(cb.bot)
        await cb.answer("Иконки включены" if ok else
                        "Telegram не принял иконки (нужен Premium у владельца бота)", show_alert=True)


@panel.callback_query(F.data == "a:rst")
async def restore_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.restore_file)
    await show(cb, "♻️ <b>Восстановление из бэкапа</b>\n\nПришли файл <code>bot_backup_….db</code>, "
                   "который бот присылал раньше. Перед заменой я сохраню копию текущей базы.\n"
                   "<i>Текущие данные будут заменены данными из файла.</i>", CANCEL)


@panel.message(S.restore_file, F.document)
async def restore_file(message: Message, state: FSMContext):
    os.makedirs(backup.BACKUP_DIR, exist_ok=True)
    path = os.path.join(backup.BACKUP_DIR, "restore_tmp.db")
    await message.bot.download(message.document, destination=path)
    ok, msg = await backup.validate_backup(path)
    if not ok:
        await message.answer(f"❌ {esc(msg)}", reply_markup=CANCEL)
        return
    await state.update_data(path=path)
    await message.answer("Файл проверен, он корректный. Заменить текущую базу?",
                         reply_markup=kb([[("✅ Восстановить", "a:rstok"), ("❌ Отмена", "a:cancel")]]))


@panel.message(S.restore_file)
async def restore_wrong(message: Message):
    await message.answer("Пришли именно файл базы (.db) документом.", reply_markup=CANCEL)


@panel.callback_query(F.data == "a:rstok", S.restore_file)
async def restore_confirm(cb: CallbackQuery, state: FSMContext):
    path = (await state.get_data()).get("path")
    await state.clear()
    ok, msg = await backup.restore_backup(path)
    await cb.message.answer(("✅ " if ok else "❌ ") + esc(msg))
    await cb.answer()
