import re
from dataclasses import dataclass
from typing import Optional

from aiogram.types import Message, User

from database.users import get_user_by_username, get_user_name, save_user

SEP = " \t\n,:;"
USERNAME = re.compile(r"^@(\w{3,32})(?:[" + re.escape(SEP) + r"]+(.*))?$", re.S)
NUMERIC = re.compile(r"^(?:id[\s:]*)?(\d{5,15})(?:[" + re.escape(SEP) + r"]+(.*))?$", re.S | re.I)


@dataclass
class Target:
    id: Optional[int]
    username: str
    name: str
    rest: str


async def from_user(user: User, rest: str = "") -> Target:
    await save_user(user.id, user.username or "", user.first_name or "", user.last_name or "")
    return Target(user.id, user.username or "", user.first_name or user.username or "Игрок", rest)


async def from_text(rest: str) -> Optional[Target]:
    rest = (rest or "").strip()
    m = USERNAME.match(rest)
    if m:
        username, tail = m.group(1), (m.group(2) or "").strip()
        uid = await get_user_by_username(username)
        name = await get_user_name(uid) if uid else f"@{username}"
        return Target(uid, username, name, tail)
    m = NUMERIC.match(rest)
    if m:
        uid = int(m.group(1))
        return Target(uid, "", await get_user_name(uid), (m.group(2) or "").strip())
    return None


async def from_mention(message: Message, rest: str) -> Optional[Target]:
    text = message.text or message.caption or ""
    for ent in message.entities or message.caption_entities or []:
        if ent.type != "text_mention" or not ent.user or ent.user.is_bot:
            continue
        shown = ent.extract_from(text)
        if shown and rest.startswith(shown):
            return await from_user(ent.user, rest[len(shown):].strip(SEP))
    return None


async def resolve(message: Message, rest: str, forwards: bool = False) -> Optional[Target]:
    rest = (rest or "").strip()
    t = await from_mention(message, rest)
    if t:
        return t
    t = await from_text(rest)
    if t:
        return t
    rp = message.reply_to_message
    if rp and rp.from_user and not rp.from_user.is_bot:
        return await from_user(rp.from_user, rest)
    if forwards:
        origin = getattr(message, "forward_origin", None)
        sender = getattr(origin, "sender_user", None)
        if sender and not sender.is_bot:
            return await from_user(sender, rest)
    return None


async def parse_target(message: Message, skip: int = 0) -> Optional[Target]:
    parts = (message.text or message.caption or "").split(maxsplit=1 + skip)
    rest = parts[1 + skip].strip() if len(parts) > 1 + skip else ""
    return await resolve(message, rest)


async def parse_free(message: Message) -> Optional[Target]:
    return await resolve(message, message.text or "", forwards=True)
