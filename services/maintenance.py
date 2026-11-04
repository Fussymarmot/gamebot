from datetime import datetime
from typing import Optional

from database.settings import get_setting, set_setting
from utils.fmt import esc
from utils.timeutils import now, parse_ts, ts, TZ

_state = {"on": False, "end": None, "comment": ""}


async def load():
    _state["on"] = (await get_setting("maint_on", "0")) == "1"
    end = await get_setting("maint_end", "")
    _state["end"] = parse_ts(end) if end else None
    _state["comment"] = await get_setting("maint_comment", "")
    if _state["on"] and (not _state["end"] or _state["end"] <= now()):
        await disable()


def is_on() -> bool:
    return bool(_state["on"] and _state["end"] and _state["end"] > now())


def end_dt() -> Optional[datetime]:
    return _state["end"]


def end_aware() -> Optional[datetime]:
    return TZ.localize(_state["end"]) if _state["end"] else None


def comment() -> str:
    return _state["comment"]


async def enable(end: datetime, note: str = ""):
    await set_setting("maint_on", "1")
    await set_setting("maint_end", ts(end))
    await set_setting("maint_comment", note)
    _state.update(on=True, end=end, comment=note)


async def disable():
    await set_setting("maint_on", "0")
    _state.update(on=False)


def notice(short: bool = False) -> str:
    end = _state["end"]
    when = end.strftime("%d.%m в %H:%M") if end else "?"
    t = f"🛠 Идут технические работы. Бот вернётся {when}."
    if short:
        return t + (f"\n{_state['comment']}" if _state["comment"] else "")
    t = f"🛠 <b>Технические работы</b>\n\nБот временно недоступен.\nОкончание: <b>{when}</b>"
    if _state["comment"]:
        t += f"\n\n💬 {esc(_state['comment'])}"
    return t
