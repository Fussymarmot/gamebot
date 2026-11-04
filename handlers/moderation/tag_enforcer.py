from aiogram import Router
from aiogram.types import ChatMemberUpdated

from config import GROUP_ID
from database.users import set_member
from services import members, tags

router = Router()


@router.chat_member()
async def on_member_update(event: ChatMemberUpdated):
    if event.chat.id != GROUP_ID:
        return
    new = event.new_chat_member
    uid = new.user.id
    if new.user.is_bot:
        return
    ok = members._status_ok(new)
    members.remember(uid, ok)
    await set_member(uid, ok)
    if ok and event.from_user.id != event.bot.id and uid in tags.perm_cache:
        await tags.enforce(event.bot, uid, getattr(new, "tag", None))
