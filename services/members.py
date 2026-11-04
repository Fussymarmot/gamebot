import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramAPIError

from config import ADMIN_ID, GROUP_ID
from database.users import all_known_user_ids, count_members, get_member_flag, save_user, set_member

log = logging.getLogger(__name__)

_cache: dict = {}
TTL_YES = 3600
TTL_NO = 120


def _status_ok(member) -> bool:
    st = getattr(member, "status", None)
    if st in ("creator", "administrator", "member"):
        return True
    if st == "restricted":
        return bool(getattr(member, "is_member", True))
    return False


def remember(user_id: int, value: bool):
    _cache[user_id] = (value, time.time() + (TTL_YES if value else TTL_NO))


async def register(user, in_group: bool = True):
    """Запомнить человека, замеченного в группе: профиль и признак участника.

    Дёшево вызывать на каждое событие: повторные записи отсекаются кэшем."""
    if user is None or user.is_bot:
        return
    await save_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    hit = _cache.get(user.id)
    if hit and hit[0] == in_group and hit[1] > time.time():
        return
    remember(user.id, in_group)
    await set_member(user.id, in_group)


async def is_member(bot: Bot, user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return True
    hit = _cache.get(user_id)
    if hit and hit[1] > time.time():
        return hit[0]
    try:
        m = await bot.get_chat_member(GROUP_ID, user_id)
        ok = _status_ok(m)
    except (TelegramBadRequest, TelegramForbiddenError):
        ok = False
    except TelegramAPIError:
        flag = await get_member_flag(user_id)
        return bool(flag)
    remember(user_id, ok)
    await set_member(user_id, ok)
    return ok


async def refresh_members(bot: Bot):
    checked = left = 0
    try:
        for adm in await bot.get_chat_administrators(GROUP_ID):
            if not adm.user.is_bot:
                await set_member(adm.user.id, True)
                remember(adm.user.id, True)
    except TelegramAPIError as ex:
        log.warning("Не удалось получить админов группы: %s", ex)
    for uid in await all_known_user_ids():
        if uid == ADMIN_ID:
            continue
        _cache.pop(uid, None)
        before = await get_member_flag(uid)
        ok = await is_member(bot, uid)
        checked += 1
        if before and not ok:
            left += 1
        await asyncio.sleep(0.05)
    total = None
    try:
        total = await bot.get_chat_member_count(GROUP_ID)
    except TelegramAPIError:
        pass
    log.info("Сканирование участников: проверено %s, вышли %s, в базе %s, в группе %s",
             checked, left, await count_members(), total)
    return checked, left, await count_members(), total
