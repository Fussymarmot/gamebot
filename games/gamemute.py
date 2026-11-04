import asyncio
import logging
from typing import Dict, Iterable, List, Set, Tuple

from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatPermissions

from database.core import q, x
from utils.fmt import esc
from utils.timeutils import now, parse_ts

log = logging.getLogger(__name__)
_bot = None
_muted: Dict[int, Set[int]] = {}


def set_bot(bot):
    global _bot
    _bot = bot


async def mention(uid: int, name: str) -> str:
    row = await q("SELECT username FROM users WHERE user_id=?", (uid,), one=True)
    if row and row[0]:
        return f"@{row[0]}"
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


async def _default_perms(bot, chat_id: int) -> ChatPermissions:
    try:
        perms = (await bot.get_chat(chat_id)).permissions
        if perms:
            return perms
    except TelegramAPIError:
        pass
    return ChatPermissions(can_send_messages=True, can_send_audios=True, can_send_documents=True,
                           can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
                           can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
                           can_add_web_page_previews=True)


async def mute_dead(bot, chat_id: int, players: Iterable[Tuple[int, str, str]], verb: str = "убит") -> List[str]:
    lines = []
    for uid, name, role in players:
        who = await mention(uid, name)
        extra = f" <i>({esc(role)})</i>" if role else ""
        ok = True
        manual = await q("SELECT until_date FROM mutes WHERE user_id=?", (uid,), one=True)
        if manual and parse_ts(manual[0]) > now():
            lines.append(f"💀 {who} {_phrase(verb)} и уже в муте.{extra}")
            continue
        try:
            await bot.restrict_chat_member(chat_id, uid, permissions=ChatPermissions(can_send_messages=False))
        except TelegramAPIError as ex:
            ok = False
            log.info("Не удалось замутить %s: %s", uid, ex)
        if ok:
            _muted.setdefault(chat_id, set()).add(uid)
            await x("INSERT OR REPLACE INTO game_mutes (user_id, chat_id) VALUES (?,?)", (uid, chat_id))
            lines.append(f"💀 {who} {_phrase(verb)} и замучен на время игры.{extra}")
        else:
            lines.append(f"💀 {who} {_phrase(verb)}.{extra} <i>(замутить не получилось: у бота нет прав "
                         "или это админ чата)</i>")
    return lines


def _phrase(verb: str) -> str:
    return {"убит": "был убит", "казнён": "был казнён"}.get(verb, verb)


async def unmute_game(chat_id: int):
    bot = _bot
    ids = set(_muted.pop(chat_id, set()))
    rows = await q("SELECT user_id FROM game_mutes WHERE chat_id=?", (chat_id,))
    ids |= {r[0] for r in rows}
    if not ids or bot is None:
        return
    perms = await _default_perms(bot, chat_id)
    for uid in ids:
        row = await q("SELECT until_date FROM mutes WHERE user_id=?", (uid,), one=True)
        manual = bool(row and parse_ts(row[0]) > now())
        if not manual:
            try:
                await bot.restrict_chat_member(chat_id, uid, permissions=perms)
            except TelegramAPIError as ex:
                log.info("Не удалось размутить %s: %s", uid, ex)
        await x("DELETE FROM game_mutes WHERE user_id=? AND chat_id=?", (uid, chat_id))
        await asyncio.sleep(0.05)


def schedule_unmute(chat_id: int):
    if _muted.get(chat_id) or _bot is None:
        try:
            asyncio.get_running_loop().create_task(unmute_game(chat_id))
        except RuntimeError:
            pass


async def restore_after_restart(bot, active_chats: Iterable[int]):
    set_bot(bot)
    for (chat_id,) in await q("SELECT DISTINCT chat_id FROM game_mutes"):
        if chat_id not in set(active_chats):
            await unmute_game(chat_id)
