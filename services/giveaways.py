import json
import logging
from datetime import timedelta
from typing import List, Optional, Tuple

from config import GROUP_ID
from database.core import q, x
from database.messages import flush_messages
from database.moderation import is_user_banned
from database.users import get_user_name
from utils.emoji import e
from utils.fmt import esc, rich
from utils.timeutils import now, parse_ts, ts

log = logging.getLogger(__name__)

COLS = ("id", "title", "prize", "kind", "metric", "metric_days", "cond_type", "cond_value",
        "winners", "ends_at", "status", "message_id", "result", "created_at")
GAME_TITLES = {"all": "во всех играх", "ttt": "в крестиках-ноликах", "rps": "в КНБ", "duel": "в дуэлях",
               "mafia": "в мафии", "memory": "в мемори",
               "code": "в «Взломай код»", "assoc": "в ассоциациях", "bunker": "в бункере"}


def _row(r) -> dict:
    return dict(zip(COLS, r))


async def get(gid: int) -> Optional[dict]:
    r = await q(f"SELECT {', '.join(COLS)} FROM giveaways WHERE id=?", (gid,), one=True)
    return _row(r) if r else None


async def active_list() -> List[dict]:
    rows = await q(f"SELECT {', '.join(COLS)} FROM giveaways WHERE status='active' ORDER BY ends_at")
    return [_row(r) for r in rows]


async def recent_list(limit: int = 5) -> List[dict]:
    rows = await q(f"SELECT {', '.join(COLS)} FROM giveaways WHERE status!='active' "
                   "ORDER BY id DESC LIMIT ?", (limit,))
    return [_row(r) for r in rows]


async def create(d: dict) -> int:
    return await x("""INSERT INTO giveaways (title, prize, kind, metric, metric_days, cond_type,
                      cond_value, winners, ends_at, status, created_at)
                      VALUES (?,?,?,?,?,?,?,?,?,'active',?)""",
                   (d["title"], d["prize"], d["kind"], d.get("metric"), d.get("days", 7),
                    d.get("cond", "none"), d.get("value", 0), d["winners"], d["ends_at"], ts()))


async def entries_count(gid: int) -> int:
    return (await q("SELECT COUNT(*) FROM giveaway_entries WHERE giveaway_id=?", (gid,), one=True))[0]


async def entries(gid: int) -> List[int]:
    return [r[0] for r in await q("SELECT user_id FROM giveaway_entries WHERE giveaway_id=?", (gid,))]


async def entry_weights(gid: int) -> List[Tuple[int, int]]:
    return [(r[0], r[1]) for r in await q("SELECT user_id, tickets FROM giveaway_entries WHERE giveaway_id=?", (gid,))]


def metric_title(metric: Optional[str], days: int) -> str:
    if not metric:
        return "—"
    if metric == "messages":
        period = {1: "сутки", 7: "неделю", 30: "месяц"}.get(days, f"{days} дн.")
        return f"сообщения за {period}"
    return "победы " + GAME_TITLES.get(metric.split(":", 1)[1], metric)


async def ranking(metric: str, days: int = 7) -> List[Tuple[int, int]]:
    if metric == "messages":
        await flush_messages()
        cutoff = ts(now() - timedelta(days=days))
        rows = await q("""SELECT user_id, COUNT(*) c FROM messages WHERE created_at >= ? AND chat_id = ? AND user_id != 0
                          GROUP BY user_id ORDER BY c DESC, user_id""", (cutoff, GROUP_ID))
    else:
        game = metric.split(":", 1)[1]
        if game == "all":
            rows = await q("""SELECT user_id, SUM(wins) w FROM game_stats GROUP BY user_id
                              HAVING w > 0 ORDER BY w DESC, user_id""")
        else:
            rows = await q("""SELECT user_id, wins FROM game_stats WHERE game_type=? AND wins > 0
                              ORDER BY wins DESC, user_id""", (game,))
    return [(r[0], r[1]) for r in rows]


async def check_eligible(g: dict, uid: int) -> Tuple[bool, str]:
    if await is_user_banned(uid):
        return False, "Ты в чёрном списке."
    if g["kind"] != "draw" or g["cond_type"] == "none":
        return True, ""
    rank = await ranking(g["metric"], g["metric_days"])
    what = metric_title(g["metric"], g["metric_days"])
    if g["cond_type"] == "top":
        place = next((i for i, (u, _) in enumerate(rank, 1) if u == uid), None)
        if place is None or place > g["cond_value"]:
            return False, f"Нужно быть в топ-{g['cond_value']} ({what}). Сейчас твоё место: {place or '—'}."
        return True, ""
    value = next((v for u, v in rank if u == uid), 0)
    if value < g["cond_value"]:
        return False, f"Нужно минимум {g['cond_value']} ({what}). Сейчас у тебя: {value}."
    return True, ""


def condition_text(g: dict) -> str:
    if g["kind"] == "top":
        return f"победители: топ-{g['winners']} ({metric_title(g['metric'], g['metric_days'])}) на момент окончания"
    if g["cond_type"] == "none":
        return "участвовать может любой"
    what = metric_title(g["metric"], g["metric_days"])
    if g["cond_type"] == "top":
        return f"нужно быть в топ-{g['cond_value']} ({what})"
    return f"нужно минимум {g['cond_value']} ({what})"


def announce_text(g: dict, count: Optional[int] = None) -> str:
    ends = parse_ts(g["ends_at"]).strftime("%d.%m в %H:%M")
    t = (f"🎁 <b>Розыгрыш: {rich(g['title'])}</b>\n\n{e('gold')} Приз: <b>{rich(g['prize'])}</b>\n"
         f"Победителей: <b>{g['winners']}</b>\nУсловие: <i>{esc(condition_text(g))}</i>\n"
         f"Итоги: <b>{ends}</b>")
    if g["kind"] == "draw" and count is not None:
        t += f"\n\nУчаствуют: <b>{count}</b>"
    return t


async def pick_winners(g: dict) -> List[int]:
    k = g["winners"]
    if g["kind"] == "top":
        rank = await ranking(g["metric"], g["metric_days"])
        out = []
        for uid, v in rank:
            if v > 0 and not await is_user_banned(uid):
                out.append(uid)
            if len(out) >= k:
                break
        return out
    from services.shop import weighted_pick
    pool = []
    for uid, tickets in await entry_weights(g["id"]):
        ok, _ = await check_eligible(g, uid)
        if ok:
            pool.append((uid, tickets))
    return weighted_pick(pool, k)


def mention(uid: int, name: str) -> str:
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


async def finish(bot, gid: int) -> Optional[List[int]]:
    g = await get(gid)
    if not g or g["status"] != "active":
        return None
    winners = await pick_winners(g)
    await x("UPDATE giveaways SET status='done', result=? WHERE id=?", (json.dumps(winners), gid))
    if winners:
        lines = []
        for i, uid in enumerate(winners, 1):
            lines.append(f"{i}. {mention(uid, await get_user_name(uid))}")
        body = "\n".join(lines)
        text = (f"{e('party')} <b>Итоги розыгрыша «{rich(g['title'])}»</b>\n\n"
                f"{e('gold')} Приз: <b>{rich(g['prize'])}</b>\n\nПобедители:\n{body}\n\n<i>Поздравляем!</i>")
    else:
        if g["kind"] == "top":
            why = "за выбранный период никто не набрал очков."
        else:
            n = await entries_count(gid)
            why = ("никто не нажал «Участвую»." if n == 0 else
                   f"участников {n}, но на момент итогов никто не подошёл под условие.")
        text = (f"🎁 <b>Итоги розыгрыша «{rich(g['title'])}»</b>\n\n"
                f"<i>Победителей нет: {why}</i>")
    try:
        await bot.send_message(GROUP_ID, text)
        if g["message_id"]:
            await bot.edit_message_text(
                f"{announce_text(g, await entries_count(gid))}\n\n<b>Розыгрыш завершён.</b>",
                chat_id=GROUP_ID, message_id=g["message_id"])
    except Exception:
        log.exception("Не удалось объявить итоги розыгрыша %s", gid)
    return winners


async def cancel(bot, gid: int):
    g = await get(gid)
    if not g or g["status"] != "active":
        return False
    await x("UPDATE giveaways SET status='cancelled' WHERE id=?", (gid,))
    try:
        if g["message_id"]:
            await bot.edit_message_text(f"{announce_text(g)}\n\n<b>Розыгрыш отменён.</b>",
                                        chat_id=GROUP_ID, message_id=g["message_id"])
    except Exception:
        pass
    return True
