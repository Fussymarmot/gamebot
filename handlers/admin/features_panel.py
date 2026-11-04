from aiogram import F
from aiogram.types import CallbackQuery

from config import ADMIN_ID
from database.moderation import log_mod
from database.settings import get_setting, set_setting
from handlers.admin.common import BACK, panel, show
from services import features
from services.commands import COMMANDS, SECTIONS, set_commands
from utils.ui import kb


def mark(on: bool) -> str:
    return "✅" if on else "❌"


async def main_view(target):
    rows = []
    for key, emoji, title in SECTIONS:
        if key == "admin":
            continue
        rows.append([(f"{mark(features.section_on(key))} {title}", f"a:ftt:{key}"), ("Команды ➜", f"a:fts:{key}")])
    rich = (await get_setting("rich_games", "0")) == "1"
    rows.append([(f"{mark(rich)} Игры таблицами (Rich)", "a:ftr")])
    text = ("🧩 <b>Функции бота</b>\n\nВыключенное пропадает из /help, меню и /games, а команда перестаёт работать. "
            "Раздел можно выключить целиком или по одной команде.\n\n<i>Админские команды, /help и /start "
            "отключить нельзя.</i>\n\n<b>Игры таблицами</b>: морской бой рисуется таблицей с кнопками "
            "внутри (Bot API 10.3). Сначала проверь командой /richtest, что твой Telegram их показывает.")
    await show(target, text, kb(rows + [BACK]))


async def section_view(target, key: str):
    title = next(t for k, _, t in SECTIONS if k == key)
    rows = []
    for name, ctitle, usage, section, adm, menu in COMMANDS:
        if section != key or not features.toggleable(name) or name == "start":
            continue
        on = name not in features._off
        rows.append([(f"{mark(on)} {ctitle} (/{name})", f"a:ftc:{key}:{name}")])
    sec_on = features.section_on(key)
    text = (f"🧩 <b>{title}</b>\n\nРаздел: <b>{'включён' if sec_on else 'выключен'}</b>"
            + ("" if sec_on else "\n<i>Пока раздел выключен, отдельные команды в нём не работают.</i>"))
    rows.append([(f"{mark(sec_on)} Весь раздел", f"a:ftt:{key}")])
    rows.append([("◀️ Назад", "a:ft")])
    await show(target, text, kb(rows))


@panel.callback_query(F.data == "a:ft")
async def ft_main(cb: CallbackQuery):
    await main_view(cb)


@panel.callback_query(F.data.startswith("a:fts:"))
async def ft_section(cb: CallbackQuery):
    await section_view(cb, cb.data.split(":")[2])


@panel.callback_query(F.data.startswith("a:ftt:"))
async def ft_toggle_section(cb: CallbackQuery):
    key = cb.data.split(":")[2]
    if key == "admin" or key not in {k for k, _, _ in SECTIONS}:
        await cb.answer("Нельзя", show_alert=True)
        return
    on = await features.toggle(f"s:{key}")
    await log_mod(ADMIN_ID, 0, "feature", f"раздел {key}: {'вкл' if on else 'выкл'}")
    await set_commands(cb.bot)
    await main_view(cb)


@panel.callback_query(F.data.startswith("a:ftc:"))
async def ft_toggle_command(cb: CallbackQuery):
    _, _, key, name = cb.data.split(":")
    if not features.toggleable(name):
        await cb.answer("Эту команду нельзя отключить", show_alert=True)
        return
    on = await features.toggle(name)
    await log_mod(ADMIN_ID, 0, "feature", f"/{name}: {'вкл' if on else 'выкл'}")
    await set_commands(cb.bot)
    await section_view(cb, key)


@panel.callback_query(F.data == "a:ftr")
async def ft_rich(cb: CallbackQuery):
    on = (await get_setting("rich_games", "0")) == "1"
    await set_setting("rich_games", "0" if on else "1")
    await log_mod(ADMIN_ID, 0, "feature", f"rich-игры: {'выкл' if on else 'вкл'}")
    await main_view(cb)
