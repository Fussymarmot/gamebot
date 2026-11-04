import json
from typing import Set

from database.settings import get_setting, set_setting

KEY = "features_off"
PROTECTED = {"help", "start", "admin", "cancel", "stopgame", "stopmafia", "pause", "resume", "emoji_id"}
_off: Set[str] = set()


async def load():
    global _off
    try:
        _off = {str(v) for v in json.loads(await get_setting(KEY, "[]"))}
    except (ValueError, TypeError):
        _off = set()


def section_of(cmd: str):
    from services.commands import COMMANDS
    for name, _, _, section, _, _ in COMMANDS:
        if name == cmd:
            return section
    return None


def toggleable(cmd: str) -> bool:
    return cmd not in PROTECTED and section_of(cmd) not in (None, "admin")


def is_on(cmd: str) -> bool:
    if cmd in PROTECTED:
        return True
    section = section_of(cmd)
    if section == "admin":
        return True
    if section and f"s:{section}" in _off:
        return False
    return cmd not in _off


def section_on(section: str) -> bool:
    return f"s:{section}" not in _off


async def toggle(name: str) -> bool:
    if name in _off:
        _off.discard(name)
    else:
        _off.add(name)
    await set_setting(KEY, json.dumps(sorted(_off), ensure_ascii=False))
    return name not in _off
