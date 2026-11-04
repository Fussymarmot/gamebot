import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.core import q, x
from database.moderation import log_mod
from services import mods, modtools, tags
from services.modtools import appeal_kb, label, MUTE_MINUTES, muted_text, name_of
from utils.emoji import e
from utils.fmt import code, esc
from utils.targets import parse_target
from utils.timeutils import parse_ts, ts
from utils.ui import kb

router = Router()
log = logging.getLogger(__name__)


def is_admin(uid) -> bool:
    return uid == ADMIN_ID


async def allowed(uid: int, perm: str) -> bool:
    if uid == ADMIN_ID:
        return True
    return perm != "admin" and await mods.has(uid, perm)


async def admin_target(message: Message, perm: str = "admin"):
    if message.chat.id != GROUP_ID and message.chat.type != "private":
        return None
    if not message.from_user or not await allowed(message.from_user.id, perm):
        if message.chat.id == GROUP_ID or message.chat.type == "private":
            await message.answer("❌ Нет доступа")
        return None
    t = await parse_target(message)
    if not t:
        cmd = message.text.split()[0].split("@")[0]
        await message.answer(f"❌ Укажи участника: {code(cmd + ' @username')} или {code(cmd + ' 123456789')}, "
                             "либо ответь на его сообщение")
        return None
    if need_id_missing(t):
        await message.answer("❌ Я пока не знаю этого пользователя. Укажи его ID, пусть он напишет в группу "
                             "или ответь командой на его сообщение.")
        return None
    if t.id == ADMIN_ID:
        await message.answer("❌ Это админ бота")
        return None
    if t.id == message.bot.id:
        await message.answer("❌ Это я")
        return None
    if message.from_user.id != ADMIN_ID and await mods.is_mod(t.id):
        await message.answer("❌ Это модератор")
        return None
    return t


def need_id_missing(t) -> bool:
    return not t.id


@router.message(Command("mute"))
async def cmd_mute(message: Message):
    t = await admin_target(message, "mute")
    if not t:
        return
    rows = [[(label(m), f"mute:{t.id}:{m}") for m in MUTE_MINUTES[i:i + 3]] for i in range(0, len(MUTE_MINUTES), 3)]
    rows.append([("🚫 Отмена", "mute:cancel:0")])
    await message.reply(f"Выбери длительность мута для <b>{esc(t.name)}</b>:", reply_markup=kb(rows))


@router.callback_query(F.data.startswith("mute:"))
async def mute_cb(cb: CallbackQuery):
    if not await allowed(cb.from_user.id, "mute"):
        await cb.answer("Нет доступа", show_alert=True)
        return
    _, target, minutes = cb.data.split(":")
    if target == "cancel":
        await cb.message.edit_text("🚫 Мут отменён")
        await cb.answer()
        return
    target, minutes = int(target), int(minutes)
    if minutes not in MUTE_MINUTES:
        await cb.answer("Недопустимое время", show_alert=True)
        return
    if target == ADMIN_ID or (cb.from_user.id != ADMIN_ID and await mods.is_mod(target)):
        await cb.answer("Этого участника мутить нельзя", show_alert=True)
        return
    err = await modtools.mute(cb.bot, cb.from_user.id, target, minutes)
    if err:
        await cb.message.edit_text(f"❌ Не получилось: {esc(err)}")
        await cb.answer()
        return
    text = muted_text(await name_of(target), minutes)
    in_group = cb.message.chat.id == GROUP_ID
    await cb.message.edit_text(text, reply_markup=appeal_kb(target) if in_group else None)
    if not in_group:
        try:
            await cb.bot.send_message(GROUP_ID, text, reply_markup=appeal_kb(target))
        except TelegramAPIError:
            log.warning("Не удалось сообщить о муте в группу")
    await cb.answer()


@router.message(Command("unmute"))
async def cmd_unmute(message: Message):
    t = await admin_target(message, "mute")
    if not t:
        return
    err = await modtools.unmute(message.bot, message.from_user.id, t.id)
    if err:
        await message.answer(f"❌ Не получилось: {esc(err)}")
        return
    await message.answer(f"{e('check')} Мут снят с <b>{esc(t.name)}</b>")


@router.message(Command("mutes"))
async def cmd_mutes(message: Message):
    if not message.from_user or not await allowed(message.from_user.id, "mute"):
        await message.answer("❌ Нет доступа")
        return
    await x("DELETE FROM mutes WHERE until_date < ?", (ts(),))
    rows = await q("""SELECT m.until_date, u.first_name, u.username, m.user_id FROM mutes m
                      LEFT JOIN users u ON u.user_id = m.user_id""")
    if not rows:
        await message.answer(f"{e('info')} Активных мутов нет")
        return
    t = f"{e('lock')} <b>Активные муты:</b>\n\n"
    for until, fn, un, uid in rows:
        t += f"• <b>{esc(fn or un or uid)}</b> {code(str(uid))} до {code(parse_ts(until).strftime('%d.%m %H:%M'))}\n"
    await message.answer(t)


@router.message(Command("tag"))
async def cmd_tag(message: Message):
    t = await admin_target(message)
    if not t:
        return
    text = t.rest.strip()
    if not text:
        await message.answer(f"❌ Укажи текст тега: {code('/tag @username текст')} или {code('/tag 123456789 текст')}")
        return
    if len(text) > 16:
        await message.answer("❌ Тег не может быть длиннее 16 символов")
        return
    try:
        await tags.set_member_tag(message.bot, t.id, text)
    except Exception as ex:
        await message.answer(f"❌ Не получилось поставить тег: {esc(str(ex))}\n"
                             "<i>Проверь, что у бота есть право управлять тегами, а участник "
                             "не администратор.</i>")
        return
    await x("INSERT OR REPLACE INTO perm_tags (user_id, chat_id, tag) VALUES (?,?,?)",
            (t.id, GROUP_ID, text))
    tags.perm_cache[t.id] = text
    await log_mod(message.from_user.id, t.id, "tag", text)
    await message.answer(f"{e('check')} Вечный тег для <b>{esc(t.name)}</b>: {code(text)}")


@router.message(Command("untag"))
async def cmd_untag(message: Message):
    t = await admin_target(message)
    if not t:
        return
    await x("DELETE FROM perm_tags WHERE user_id=? AND chat_id=?", (t.id, GROUP_ID))
    tags.perm_cache.pop(t.id, None)
    try:
        await tags.set_member_tag(message.bot, t.id, None)
    except Exception as ex:
        log.warning("untag: %s", ex)
    await log_mod(message.from_user.id, t.id, "untag")
    await message.answer(f"{e('check')} Тег снят с <b>{esc(t.name)}</b>")


@router.message(Command("tags"))
async def cmd_tags(message: Message):
    if not message.from_user or not is_admin(message.from_user.id):
        await message.answer("❌ Нет доступа")
        return
    rows = await q("""SELECT p.tag, u.first_name, u.username, p.user_id FROM perm_tags p
                      LEFT JOIN users u ON u.user_id = p.user_id WHERE p.chat_id=?""", (GROUP_ID,))
    if not rows:
        await message.answer(f"{e('info')} Вечных тегов нет")
        return
    t = f"{e('bookmark')} <b>Вечные теги:</b>\n\n"
    for tag, fn, un, uid in rows:
        t += f"• <b>{esc(fn or un or uid)}</b>: {code(tag)}\n"
    await message.answer(t)
