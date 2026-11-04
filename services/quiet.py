from datetime import datetime, timedelta

from database.settings import get_setting
from utils.timeutils import MSK


async def quiet_config():
    enabled = (await get_setting("quiet_enabled", "1")) == "1"
    start = int(await get_setting("quiet_start", "3"))
    end = int(await get_setting("quiet_end", "5"))
    return enabled, start, end


async def quiet_state():
    enabled, start, end = await quiet_config()
    if not enabled or start == end:
        return False, end
    h = datetime.now(MSK).hour
    inside = (start <= h < end) if start < end else (h >= start or h < end)
    return inside, end


def next_quiet_end(end_hour: int) -> datetime:
    n = datetime.now(MSK)
    t = n.replace(hour=end_hour, minute=0, second=0, microsecond=0)
    if t <= n:
        t += timedelta(days=1)
    return t


async def silent_state():
    from services import maintenance
    quiet, end = await quiet_state()
    resume = next_quiet_end(end) if quiet else None
    if maintenance.is_on():
        m_end = maintenance.end_aware()
        if m_end and (resume is None or m_end > resume):
            resume = m_end
    return resume is not None, resume
