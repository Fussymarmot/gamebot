import logging
from datetime import timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from database import game_stats, settings as db_settings
from database.core import batch, q, x
from database.settings import get_setting
from utils.fmt import plain
from utils.timeutils import now, parse_ts, ts

log = logging.getLogger(__name__)

announce: Optional[Callable[[str], Awaitable[None]]] = None
notify_admin: Optional[Callable[[str], Awaitable[None]]] = None

REWARD = {"win": 4, "draw": 1, "loss": 1}
WIN_BONUS = {"mafia": 6, "bunker": 6}
DAILY_CAP = 40
COOLDOWN_SEC = 60
HOURLY_LIMIT = 12

SHOP: Dict[str, Tuple[str, int]] = {
    "t_gamer": ("🎮 Геймер", 100),
    "t_brain": ("🧠 Мозг компании", 200),
    "t_agent": ("🕵 Тайный агент", 200),
    "t_star": ("⭐ Звезда", 350),
    "t_crown": ("👑 Король игр", 600),
}
DEFAULT_SHOP: Dict[str, Tuple[str, int]] = dict(SHOP)
TICKET_PRICE = 30
TAG_PRICE = 150
TAG_DAYS = 7

ACHIEVEMENTS: Dict[str, Tuple[str, str, int]] = {
    "first_win": ("Первая победа", "Выиграть первую игру", 10),
    "veteran": ("Ветеран", "Сыграть 50 игр", 30),
    "champion": ("Чемпион", "Одержать 10 побед", 30),
    "streak5": ("Серия побед", "Выиграть 5 игр подряд", 20),
    "explorer": ("Исследователь", "Сыграть в 5 разных игр", 15),
    "rich": ("Богач", "Накопить 300 рубинов", 30),
    "regular": ("Постоянный игрок", "Играть в 7 разных дней", 20),
}
TASKS: Dict[str, Tuple[str, int, int]] = {
    "play3": ("Сыграть 3 игры", 3, 5),
    "win1": ("Выиграть 1 игру", 1, 8),
    "try2": ("Попробовать 2 разные игры", 2, 5),
}
LEVELS = {"easy": ("Лёгкая", 1.5), "normal": ("Средняя", 1.0), "hard": ("Сложная", 0.6)}
level_key = "normal"


async def load_level():
    global level_key
    level_key = await get_setting("economy_level", "normal")
    if level_key not in LEVELS:
        level_key = "normal"


async def set_level(key: str):
    global level_key
    level_key = key if key in LEVELS else "normal"
    await db_settings.set_setting("economy_level", level_key)


def mult() -> float:
    return LEVELS[level_key][1]


def reward(n: int) -> int:
    return max(1, round(n * mult())) if n > 0 else 0


def price(n: int) -> int:
    return max(5, int(round(n / (mult() ** 0.5) / 5.0)) * 5)


def daily_cap() -> int:
    return reward(DAILY_CAP)


async def ensure(uid: int):
    await x("INSERT OR IGNORE INTO wallets (user_id, coins, total_earned) VALUES (?,0,0)", (uid,))


async def wallet(uid: int):
    await ensure(uid)
    return await q("SELECT coins, total_earned, title, last_reward FROM wallets WHERE user_id=?", (uid,), one=True)


async def add_coins(uid: int, amount: int):
    await ensure(uid)
    await x("UPDATE wallets SET coins = MAX(0, coins + ?), total_earned = total_earned + ? WHERE user_id=?",
            (amount, max(amount, 0), uid))


async def name_of(uid: int) -> str:
    r = await q("SELECT username, first_name FROM users WHERE user_id=?", (uid,), one=True)
    if r and r[0]:
        return f"@{r[0]}"
    return r[1] if r and r[1] else "Игрок"


async def say(text: str):
    if announce:
        try:
            await announce(text)
        except Exception:
            log.exception("Не удалось отправить уведомление")


async def progress(uid: int, day: str):
    start, hour = day + " 00:00:00", ts(now() - timedelta(hours=1))
    a = await q("""SELECT COUNT(*), COALESCE(SUM(result='win'),0), COUNT(DISTINCT game_key),
                   COUNT(DISTINCT substr(played_at,1,10)), COALESCE(SUM(played_at >= ?),0),
                   COALESCE(SUM(played_at >= ?),0), COALESCE(SUM(played_at >= ? AND result='win'),0),
                   COUNT(DISTINCT CASE WHEN played_at >= ? THEN game_key END)
                   FROM game_log WHERE user_id=?""", (hour, start, start, start, uid), one=True)
    last5 = [r[0] for r in await q("SELECT result FROM game_log WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))]
    return {"games": a[0], "wins": a[1], "kinds": a[2], "days": a[3], "hour": a[4],
            "t_games": a[5], "t_wins": a[6], "t_kinds": a[7], "last5": last5}


async def process(uid: int, game: Optional[str] = None, result: Optional[str] = None):
    day = now().strftime("%Y-%m-%d")
    coins, total, title, last = await wallet(uid)
    p = await progress(uid, day)
    rows = {r[0]: (r[1], r[2]) for r in await q(
        "SELECT task, progress, done FROM daily_tasks WHERE user_id=? AND day=?", (uid, day))}
    earned = rows.get("_earned", (0, 0))[0]
    gain, stmts, lines = 0, [], []
    if game and result:
        ok = p["hour"] <= HOURLY_LIMIT
        if p["hour"] == HOURLY_LIMIT + 1 and notify_admin:
            await notify_admin(f"⚠️ Подозрительная активность: {await name_of(uid)} сыграл больше "
                               f"{HOURLY_LIMIT} игр за час. Награды приостановлены.")
        if last and (now() - parse_ts(last)).total_seconds() < COOLDOWN_SEC:
            ok = False
        if ok:
            base = WIN_BONUS.get(game, REWARD["win"]) if result == "win" else REWARD[result]
            amount = max(0, min(reward(base), daily_cap() - earned))
            if amount:
                gain += amount
                stmts.append(("""INSERT INTO daily_tasks (user_id, day, task, progress, done) VALUES (?,?, '_earned', ?, 0)
                                 ON CONFLICT(user_id, day, task) DO UPDATE SET progress = progress + excluded.progress""",
                              (uid, day, amount)))
                stmts.append(("UPDATE wallets SET last_reward=? WHERE user_id=?", (ts(), uid)))
    values = {"play3": p["t_games"], "win1": p["t_wins"], "try2": p["t_kinds"]}
    for key, (name, goal, base) in TASKS.items():
        prog = min(values[key], goal)
        was = bool(rows.get(key, (0, 0))[1])
        done = prog >= goal
        stmts.append(("""INSERT INTO daily_tasks (user_id, day, task, progress, done) VALUES (?,?,?,?,?)
                         ON CONFLICT(user_id, day, task) DO UPDATE SET progress=excluded.progress, done=excluded.done""",
                      (uid, day, key, prog, 1 if (done or was) else 0)))
        if done and not was:
            gain += reward(base)
            lines.append(f"📅 {{n}} выполнил(а) задание «{name}» и получает {reward(base)} рубинов!")
    have = {r[0] for r in await q("SELECT key FROM achievements WHERE user_id=?", (uid,))}
    cond = {
        "first_win": p["wins"] >= 1, "veteran": p["games"] >= 50, "champion": p["wins"] >= 10,
        "streak5": len(p["last5"]) == 5 and all(r == "win" for r in p["last5"]),
        "explorer": p["kinds"] >= 5, "regular": p["days"] >= 7, "rich": coins + gain >= 300,
    }
    for key, ok in cond.items():
        if ok and key not in have:
            name, _, base = ACHIEVEMENTS[key]
            stmts.append(("INSERT OR IGNORE INTO achievements (user_id, key, earned_at) VALUES (?,?,?)", (uid, key, ts())))
            gain += reward(base)
            lines.append(f"🏆 {{n}} получил(а) достижение «{name}»! (+{reward(base)} рубинов)")
    if gain:
        stmts.append(("UPDATE wallets SET coins = coins + ?, total_earned = total_earned + ? WHERE user_id=?",
                      (gain, gain, uid)))
    if stmts:
        await batch(stmts)
    if lines:
        who = await name_of(uid)
        await say("\n".join(l.format(n=who) for l in lines))
    return [(TASKS[k][0], min(values[k], TASKS[k][1]), TASKS[k][1],
             bool(rows.get(k, (0, 0))[1]) or values[k] >= TASKS[k][1]) for k in TASKS]


async def on_result(uid: int, game: str, result: str):
    await process(uid, game, result)


async def update_tasks(uid: int):
    return await process(uid)


async def earned_keys(uid: int) -> List[str]:
    return [r[0] for r in await q("SELECT key FROM achievements WHERE user_id=?", (uid,))]


async def coin_rank(uid: int) -> Tuple[int, int]:
    coins = (await wallet(uid))[0]
    r = await q("SELECT COUNT(*) FILTER (WHERE coins > ?), COUNT(*) FILTER (WHERE coins > 0) FROM wallets",
                (coins,), one=True)
    return r[0] + 1, max(r[1], 1)


async def top_coins(limit: int) -> List[Tuple[int, str, int, Optional[str]]]:
    rows = await q("""SELECT w.user_id, COALESCE(u.first_name, u.username, 'Игрок'), w.coins, w.title
                      FROM wallets w LEFT JOIN users u ON u.user_id = w.user_id
                      WHERE w.coins > 0 ORDER BY w.coins DESC, w.user_id LIMIT ?""", (limit,))
    return [(r[0], r[1], r[2], r[3]) for r in rows]


async def game_totals(uid: int) -> Tuple[int, int, int]:
    r = await q("SELECT COALESCE(SUM(wins),0), COALESCE(SUM(losses),0), COALESCE(SUM(draws),0) "
                "FROM game_stats WHERE user_id=?", (uid,), one=True)
    return r[0], r[1], r[2]


async def wins_rank(uid: int) -> Optional[int]:
    mine = (await game_totals(uid))[0]
    if mine == 0:
        return None
    better = (await q("SELECT COUNT(*) FROM (SELECT SUM(wins) w FROM game_stats GROUP BY user_id HAVING w > ?)",
                      (mine,), one=True))[0]
    return better + 1


async def owned(uid: int) -> List[str]:
    return [r[0] for r in await q("SELECT item FROM purchases WHERE user_id=?", (uid,))]


async def buy(uid: int, key: str) -> Tuple[bool, str]:
    if key not in SHOP:
        return False, "Такого товара нет."
    if key in await owned(uid):
        return False, "Это уже куплено."
    title, base = SHOP[key]
    cost = price(base)
    coins = (await wallet(uid))[0]
    if coins < cost:
        return False, f"Не хватает рубинов: нужно {cost}, у тебя {coins}."
    await batch([("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (cost, uid)),
                 ("INSERT INTO purchases (user_id, item, bought_at) VALUES (?,?,?)", (uid, key, ts()))])
    return True, f"Куплено: {plain(title)}!"


async def equip(uid: int, key: Optional[str]) -> Tuple[bool, str]:
    if key is None:
        await x("UPDATE wallets SET title=NULL WHERE user_id=?", (uid,))
        return True, "Титул снят."
    if key not in await owned(uid):
        return False, "Сначала купи этот титул."
    await ensure(uid)
    await x("UPDATE wallets SET title=?, title_at=? WHERE user_id=?", (plain(SHOP[key][0]), ts(), uid))
    return True, f"Надет титул: {plain(SHOP[key][0])}"


def register_hooks():
    if on_result not in game_stats.RESULT_HOOKS:
        game_stats.RESULT_HOOKS.append(on_result)


async def load_shop():
    import json
    raw = await get_setting("shop_titles", "")
    if not raw:
        return
    try:
        data = json.loads(raw)
        items = {str(k): (str(v[0]), int(v[1])) for k, v in data.items()}
    except (ValueError, TypeError, IndexError, AttributeError):
        return
    SHOP.clear()
    SHOP.update(items)


async def save_shop():
    import json
    await db_settings.set_setting("shop_titles", json.dumps({k: [v[0], v[1]] for k, v in SHOP.items()}, ensure_ascii=False))
