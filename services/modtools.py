from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions

from config import ADMIN_ID, GROUP_ID
from database.core import q, x
from database.moderation import log_mod
from database.settings import get_setting
from utils.emoji import e
from utils.fmt import code, esc
from utils.timeutils import now, ts
from utils.ui import kb

MUTE_MINUTES = (5, 10, 15, 30, 60, 120, 180)


def label(minutes: int) -> str:
    return f"{minutes} мин" if minutes < 60 else f"{minutes // 60} ч"


def appeal_kb(uid: int):
    return kb([[("📝 Обжаловать", f"ap:{uid}")]])


async def mute(bot, actor: int, target: int, minutes: int, note: str = "") -> Optional[str]:
    try:
        await bot.restrict_chat_member(
            GROUP_ID, target, permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now(timezone.utc) + timedelta(minutes=minutes))
    except TelegramAPIError as ex:
        return str(ex)
    await x("INSERT OR REPLACE INTO mutes (user_id, until_date, created_at) VALUES (?,?,?)",
            (target, ts(now() + timedelta(minutes=minutes)), ts()))
    await log_mod(actor, target, "mute", label(minutes) + (f" ({note})" if note else ""))
    return None


async def unmute(bot, actor: int, target: int) -> Optional[str]:
    try:
        chat = await bot.get_chat(GROUP_ID)
        perms = chat.permissions or ChatPermissions(
            can_send_messages=True, can_send_audios=True, can_send_documents=True,
            can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
            can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
            can_add_web_page_previews=True)
        await bot.restrict_chat_member(GROUP_ID, target, permissions=perms)
    except TelegramAPIError as ex:
        return str(ex)
    await x("DELETE FROM mutes WHERE user_id=?", (target,))
    await log_mod(actor, target, "unmute")
    return None


async def warn_limit() -> int:
    return int(await get_setting("warn_limit", "3"))


async def warn(bot, actor: int, target: int, name: str, reason: str) -> Tuple[str, bool]:
    await x("INSERT INTO warns (user_id, admin_id, reason, created_at, active) VALUES (?,?,?,?,1)",
            (target, actor, reason[:300], ts()))
    count = (await q("SELECT COUNT(*) FROM warns WHERE user_id=? AND active=1", (target,), one=True))[0]
    limit = await warn_limit()
    await log_mod(actor, target, "warn", reason[:100])
    text = f"⚠️ <b>{esc(name)}</b> получает предупреждение {count}/{limit}.\nПричина: {esc(reason[:300])}"
    muted = False
    if count % limit == 0:
        minutes = int(await get_setting("warn_mute_min", "60"))
        err = await mute(bot, actor, target, minutes, "лимит предупреждений")
        if err:
            text += f"\n<i>Лимит достигнут, но замутить не получилось: {esc(err)}</i>"
        else:
            muted = True
            text += f"\n🔇 Лимит достигнут: мут на {minutes} мин."
    return text, muted


async def name_of(uid: int) -> str:
    row = await q("SELECT first_name, username FROM users WHERE user_id=?", (uid,), one=True)
    if row:
        return row[0] or (f"@{row[1]}" if row[1] else f"ID {uid}")
    return f"ID {uid}"


def muted_text(name: str, minutes: int) -> str:
    return f"{e('lock')} <b>{esc(name)}</b> замучен на {code(label(minutes))}"
