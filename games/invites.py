import asyncio
import secrets
from typing import Awaitable, Callable, Dict

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message

from database.moderation import is_user_banned
from games import pause
from utils.emoji import e
from utils.fmt import code, esc
from utils.targets import parse_target
from utils.ui import kb

router = Router()

active_invites: Dict[str, dict] = {}
busy: Dict[int, str] = {}
INVITE_TTL = 120


def is_busy(uid: int) -> bool:
    return uid in busy


def acquire(a: int, b: int, tag: str) -> bool:
    if a in busy or b in busy:
        return False
    busy[a] = busy[b] = tag
    return True


def release(*ids: int):
    for i in ids:
        busy.pop(i, None)


def _kb(inv_id: str):
    return kb([[("✅ Принять", f"inv:{inv_id}:accept"), ("❌ Отказаться", f"inv:{inv_id}:decline")],
               [("🚫 Отмена", f"inv:{inv_id}:cancel")]])


async def send_invite(message: Message, cmd: str, emoji_key: str, title: str,
                      start: Callable[..., Awaitable[None]]):
    me = message.from_user
    if pause.is_paused():
        await message.answer("⏸ Сейчас игры на паузе.")
        return
    if await is_user_banned(me.id):
        await message.answer("❌ Ты в чёрном списке игр")
        return
    if is_busy(me.id):
        await message.answer("⚠️ Ты уже участвуешь в игре")
        return
    t = await parse_target(message)
    if not t:
        await message.answer(f"Используй: {code(f'/{cmd} @username')} или {code(f'/{cmd} 123456789')}\n"
                             "<i>или ответь командой на сообщение соперника</i>")
        return
    if t.id == me.id or (t.username and me.username and t.username.lower() == me.username.lower()):
        await message.answer("❌ Нельзя вызвать самого себя")
        return
    if t.id == message.bot.id or (t.id is None and t.username.lower().endswith("bot")):
        await message.answer("❌ Нельзя вызвать бота")
        return
    if t.id and await is_user_banned(t.id):
        await message.answer("❌ Этот пользователь в чёрном списке игр")
        return
    if t.id and is_busy(t.id):
        await message.answer("⚠️ Этот игрок уже участвует в игре")
        return

    creator = me.first_name or me.username or "Игрок"
    target_html = f"<b>@{esc(t.username)}</b>" if t.username else f"<b>{esc(t.name)}</b>"
    text = f"{e(emoji_key)} <b>{esc(creator)}</b> вызывает {target_html} на {title}!"
    inv_id = secrets.token_hex(4)
    msg = await message.answer(text, reply_markup=_kb(inv_id))
    active_invites[inv_id] = {
        "creator_id": me.id, "creator": creator, "target_id": t.id,
        "target_username": (t.username or "").lower(), "msg": msg,
        "start": start, "tag": cmd,
    }
    asyncio.create_task(_expire(inv_id))


async def _expire(inv_id: str):
    await asyncio.sleep(INVITE_TTL)
    inv = active_invites.pop(inv_id, None)
    if inv:
        try:
            await inv["msg"].edit_text(f"{inv['msg'].html_text}\n\n<i>Приглашение истекло</i>")
        except Exception:
            pass


@router.callback_query(F.data.startswith("inv:"))
async def invite_callback(cb: CallbackQuery):
    try:
        _, inv_id, action = cb.data.split(":")
    except ValueError:
        await cb.answer()
        return
    inv = active_invites.get(inv_id)
    if not inv:
        await cb.answer("Приглашение больше не активно", show_alert=True)
        return
    user = cb.from_user
    base = cb.message.html_text

    if action == "cancel":
        if user.id != inv["creator_id"]:
            await cb.answer("Отменить может только создатель", show_alert=True)
            return
        active_invites.pop(inv_id, None)
        await cb.message.edit_text(f"{base}\n\n<i>Отменено создателем</i>")
        await cb.answer()
        return

    is_target = (inv["target_id"] and user.id == inv["target_id"]) or \
                (user.username and user.username.lower() == inv["target_username"])
    if not is_target:
        await cb.answer("Это приглашение не для тебя", show_alert=True)
        return

    if action == "decline":
        active_invites.pop(inv_id, None)
        await cb.message.edit_text(f"{base}\n\n<i>Отклонено</i>")
        await cb.answer()
        return

    if await is_user_banned(user.id):
        await cb.answer("Ты в чёрном списке игр", show_alert=True)
        return
    if not acquire(inv["creator_id"], user.id, inv["tag"]):
        await cb.answer("Кто-то из вас уже участвует в другой игре", show_alert=True)
        return
    active_invites.pop(inv_id, None)
    await cb.message.edit_text(f"{base}\n\n<b>Игра начата!</b>")
    await cb.answer()
    name = user.first_name or user.username or "Игрок"
    asyncio.create_task(inv["start"](cb.message, inv["creator_id"], inv["creator"], user.id, name))
