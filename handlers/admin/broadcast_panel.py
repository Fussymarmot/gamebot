import asyncio
import logging
import re

from aiogram import F
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID, GROUP_ID
from database.users import mark_private_blocked, private_recipients
from handlers.admin.common import BACK, CANCEL, panel, S, show
from utils.emoji import e, EMOJI
from utils.fmt import esc
from utils.ui import kb

log = logging.getLogger(__name__)
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def fill_placeholders(html: str) -> str:
    return PLACEHOLDER.sub(lambda m: e(m.group(1)) if m.group(1) in EMOJI else m.group(0), html)


def build_payload(src: Message) -> dict:
    html = src.html_text if (src.text or src.caption) else ""
    filled = fill_placeholders(html) if html else html
    kind, file_id = "text", None
    if src.photo:
        kind, file_id = "photo", src.photo[-1].file_id
    elif src.video:
        kind, file_id = "video", src.video.file_id
    elif src.document:
        kind, file_id = "document", src.document.file_id
    elif src.animation:
        kind, file_id = "animation", src.animation.file_id
    return {"chat": src.chat.id, "mid": src.message_id, "rebuild": bool(html and filled != html),
            "kind": kind, "file_id": file_id, "html": filled}


async def send_payload(bot, p: dict, chat_id: int) -> int:
    if p["rebuild"]:
        k = p["kind"]
        if k == "text":
            m = await bot.send_message(chat_id, p["html"])
        elif k == "photo":
            m = await bot.send_photo(chat_id, p["file_id"], caption=p["html"])
        elif k == "video":
            m = await bot.send_video(chat_id, p["file_id"], caption=p["html"])
        elif k == "document":
            m = await bot.send_document(chat_id, p["file_id"], caption=p["html"])
        else:
            m = await bot.send_animation(chat_id, p["file_id"], caption=p["html"])
        return m.message_id
    m = await bot.copy_message(chat_id, p["chat"], p["mid"])
    return m.message_id


@panel.callback_query(F.data == "a:bc")
async def bc_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.bc_msg)
    await show(cb, f"{e('mail')} <b>Рассылка в группу</b>\n\nПришли сообщение для группы: текст, фото с подписью, "
                   "видео или файл. Форматирование и премиум-эмодзи сохранятся "
                   "(если у тебя есть Telegram Premium).\n\n"
                   "<i>Без Premium можно написать в тексте</i> <code>{gold}</code>, <code>{bell}</code> "
                   "<i>и т.п.: бот подставит эмодзи из своего набора.</i>", CANCEL)


@panel.message(S.bc_msg)
async def bc_preview(message: Message, state: FSMContext):
    if not any([message.text, message.photo, message.video, message.document, message.animation]):
        await message.answer("❌ Это сообщение не поддерживается. Пришли текст, фото, видео или файл.",
                             reply_markup=CANCEL)
        return
    payload = build_payload(message)
    note = "\n<i>Альбомы не поддерживаются: уйдёт только это сообщение.</i>" if message.media_group_id else ""
    try:
        await send_payload(message.bot, payload, ADMIN_ID)
    except TelegramAPIError as ex:
        await message.answer(f"❌ Не получилось сформировать сообщение: {esc(str(ex))}", reply_markup=CANCEL)
        return
    await state.update_data(payload=payload)
    await message.answer("☝️ <b>Предпросмотр.</b> Отправить в группу?" + note,
                         reply_markup=kb([[("✅ Отправить", "a:bcok"), ("📌 Закрепить и отправить", "a:bcpin")],
                                          [("❌ Отмена", "a:cancel")]]))


@panel.callback_query(F.data.in_({"a:bcok", "a:bcpin"}), S.bc_msg)
async def bc_send(cb: CallbackQuery, state: FSMContext):
    payload = (await state.get_data()).get("payload")
    try:
        mid = await send_payload(cb.bot, payload, GROUP_ID)
        res = f"{e('check')} Отправлено в группу"
        if cb.data == "a:bcpin":
            try:
                await cb.bot.pin_chat_message(GROUP_ID, mid)
                res += " и закреплено"
            except TelegramAPIError as ex:
                res += f", но закрепить не вышло: {esc(str(ex))}"
    except Exception as ex:
        res = f"❌ Ошибка: {esc(str(ex))}"
    await state.clear()
    await show(cb, res, kb([BACK]))


DM_TEXT = "⚠️ <b>Бот был обновлён!</b> Чтобы увидеть изменения, отправьте /start!"
GROUP_TEXT = "⚠️ <b>Бот был обновлён</b>, никаких действий не требуется."


def update_texts(changes: str):
    tail = f"\n\n📋 <b>Список изменений:</b>\n{esc(changes)}" if changes else ""
    return DM_TEXT + tail, GROUP_TEXT + tail


@panel.callback_query(F.data == "a:upd")
async def upd_start(cb: CallbackQuery, state: FSMContext):
    await state.set_state(S.upd_text)
    await show(cb, "🔄 <b>Сообщение об обновлении</b>\n\nПришли список изменений текстом. "
                   "Если писать нечего, отправь «-»: блок со списком не будет добавлен.\n\n"
                   "<i>Сообщение получат все, кто пользуется ботом в личке, и группа.</i>", CANCEL)


@panel.message(S.upd_text)
async def upd_preview(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    changes = "" if raw in ("", "-") else raw[:2500]
    await state.update_data(changes=changes)
    dm, grp = update_texts(changes)
    n = len(await private_recipients())
    await message.answer(f"<b>В личные сообщения</b> (получателей: {n}):\n\n{dm}\n\n"
                         f"<b>В группу:</b>\n\n{grp}",
                         reply_markup=kb([[("✅ Отправить всем", "a:updok"), ("❌ Отмена", "a:cancel")]]))


async def broadcast_update(bot, dm_text: str):
    sent = failed = 0
    for uid in await private_recipients():
        try:
            await bot.send_message(uid, dm_text)
            sent += 1
        except TelegramForbiddenError:
            await mark_private_blocked(uid)
            failed += 1
        except TelegramAPIError:
            failed += 1
        await asyncio.sleep(0.06)
    return sent, failed


@panel.callback_query(F.data == "a:updok", S.upd_text)
async def upd_send(cb: CallbackQuery, state: FSMContext):
    changes = (await state.get_data()).get("changes", "")
    await state.clear()
    dm, grp = update_texts(changes)
    await cb.answer("Отправляю...")
    try:
        await cb.bot.send_message(GROUP_ID, grp)
        grp_res = "группа: ✅"
    except Exception as ex:
        grp_res = f"группа: ❌ {esc(str(ex))}"
    sent, failed = await broadcast_update(cb.bot, dm)
    await cb.message.answer(f"🔄 <b>Обновление отправлено</b>\n\nЛичные сообщения: {sent} доставлено, "
                            f"{failed} не доставлено\n{grp_res}", reply_markup=kb([BACK]))
