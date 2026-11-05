from typing import Dict, Optional

import aiosqlite
from aiogram import Bot
from aiogram.methods import TelegramMethod

from config import GROUP_ID
from database.core import DB_PATH
from utils.logs import logger


perm_cache: Dict[int, str] = {}


class SetChatMemberTagRaw(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "setChatMemberTag"

    chat_id: int
    user_id: int
    tag: Optional[str] = None


async def load_cache():
    perm_cache.clear()
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT user_id, tag FROM perm_tags WHERE chat_id=?", (GROUP_ID,))
        for uid, tag in await cur.fetchall():
            perm_cache[uid] = tag


async def set_member_tag(bot: Bot, user_id: int, tag: Optional[str]) -> bool:
    native = getattr(bot, "set_chat_member_tag", None)
    if native:
        return await native(chat_id=GROUP_ID, user_id=user_id, tag=tag or None)
    return await bot(SetChatMemberTagRaw(chat_id=GROUP_ID, user_id=user_id, tag=tag or None))


async def enforce(bot: Bot, user_id: int, current: Optional[str]):
    expected = perm_cache.get(user_id)
    if expected is None or (current or "") == expected:
        return
    try:
        await set_member_tag(bot, user_id, expected)
    except Exception:
        logger.warning("Не удалось привести тег игрока %s в порядок", user_id, exc_info=True)
