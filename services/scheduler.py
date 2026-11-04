from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config import GROUP_ID, TIMEZONE
from database import lifecycle
from database.core import q, x
from database.messages import cleanup_old_messages, flush_messages
from database.settings import get_setting
from games import core as games_core
from services import backup, giveaways, maintenance, members, tags
from services.quiet import silent_state
from utils.timeutils import now, parse_ts, ts, TZ

scheduler = AsyncIOScheduler(timezone=pytz.timezone(TIMEZONE))
_bot = None


def _at(dt: datetime) -> datetime:
    aware = TZ.localize(dt) if dt.tzinfo is None else dt
    soon = datetime.now(TZ) + timedelta(seconds=5)
    return aware if aware > soon else soon


async def giveaway_job(gid: int):
    silent, resume = await silent_state()
    if silent:
        scheduler.add_job(giveaway_job, DateTrigger(run_date=resume), args=[gid],
                          id=f"gw_{gid}", replace_existing=True, misfire_grace_time=86400)
        return
    await giveaways.finish(_bot, gid)


def schedule_giveaway(gid: int, ends_at: str):
    scheduler.add_job(giveaway_job, DateTrigger(run_date=_at(parse_ts(ends_at))), args=[gid],
                      id=f"gw_{gid}", replace_existing=True, misfire_grace_time=86400)


def unschedule_giveaway(gid: int):
    try:
        scheduler.remove_job(f"gw_{gid}")
    except Exception:
        pass


async def load_giveaways():
    for g in await giveaways.active_list():
        schedule_giveaway(g["id"], g["ends_at"])


async def maint_end_job():
    end = maintenance.end_dt()
    if maintenance._state["on"] and end and end <= now():
        await maintenance.disable()
        try:
            await _bot.send_message(GROUP_ID, "✅ <b>Технические работы завершены.</b>\nБот снова работает!")
        except Exception:
            pass


def schedule_maint_end(end: datetime):
    scheduler.add_job(maint_end_job, DateTrigger(run_date=_at(end)), id="maint_end",
                      replace_existing=True, misfire_grace_time=86400)


def unschedule_maint_end():
    try:
        scheduler.remove_job("maint_end")
    except Exception:
        pass


async def check_perm_tags():
    for uid in list(tags.perm_cache):
        try:
            m = await _bot.get_chat_member(GROUP_ID, uid)
            await tags.enforce(_bot, uid, getattr(m, "tag", None))
        except Exception:
            pass


async def refresh_members_job():
    await members.refresh_members(_bot)


async def quiet_job():
    await games_core.quiet_watch(_bot)


async def expire_tags():
    rows = await q("SELECT id, user_id FROM orders WHERE kind='tag' AND status='approved' AND expires_at <= ?", (ts(),))
    for oid, uid in rows:
        if uid not in tags.perm_cache:
            try:
                await tags.set_member_tag(_bot, uid, None)
            except Exception:
                pass
        await x("UPDATE orders SET status='expired' WHERE id=?", (oid,))


async def maintenance_job():
    from middlewares import ratelimit
    ratelimit.purge()
    if datetime.now(TZ).weekday() == 6 and datetime.now(TZ).hour == 4:
        await lifecycle.maintenance(vacuum=datetime.now(TZ).day <= 7)


async def backup_job():
    await backup.send_backup(_bot, "автоматический")


async def weekly_report_job():
    if (await get_setting("weekly_report", "1")) != "1":
        return
    from services import report_week
    from config import ADMIN_ID
    try:
        await _bot.send_message(ADMIN_ID, await report_week.build())
    except Exception:
        pass


async def startup_jobs():
    await load_giveaways()
    if maintenance.is_on():
        schedule_maint_end(maintenance.end_dt())


def start_scheduler(bot):
    global _bot
    _bot = bot
    scheduler.add_job(cleanup_old_messages, CronTrigger(hour=4, minute=0, timezone=TZ), id="cleanup")
    scheduler.add_job(backup_job, CronTrigger(hour=4, minute=10, timezone=TZ), id="backup")
    scheduler.add_job(refresh_members_job, CronTrigger(hour=4, minute=30, timezone=TZ), id="members_daily")
    scheduler.add_job(flush_messages, IntervalTrigger(seconds=15), id="flush")
    scheduler.add_job(check_perm_tags, IntervalTrigger(minutes=5), id="perm_tags")
    scheduler.add_job(quiet_job, IntervalTrigger(seconds=60), id="quiet_watch")
    scheduler.add_job(expire_tags, IntervalTrigger(hours=1), id="expire_tags")
    scheduler.add_job(maintenance_job, IntervalTrigger(minutes=30), id="maintenance")
    scheduler.add_job(weekly_report_job, CronTrigger(day_of_week="sun", hour=19, minute=0, timezone=TZ),
                      id="weekly_report", misfire_grace_time=3600)
    scheduler.add_job(startup_jobs, DateTrigger(run_date=datetime.now(TZ) + timedelta(seconds=2)),
                      id="startup")
    scheduler.add_job(refresh_members_job, DateTrigger(run_date=datetime.now(TZ) + timedelta(seconds=10)),
                      id="members_startup")
    scheduler.start()
