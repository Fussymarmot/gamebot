import asyncio
from datetime import timedelta
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

from database import game_stats, settings as db_settings
from database.core import q, tx, x
from database.settings import get_setting
from utils.fmt import plain
from utils.logs import logger
from utils.timeutils import now, parse_ts, ts


announce: Optional[Callable[[str], Awaitable[None]]] = None
notify_admin: Optional[Callable[[str], Awaitable[None]]] = None

REWARD = {"win": 4, "draw": 1, "loss": 1}
WIN_BONUS = {"mafia": 6, "bunker": 6}
DAILY_CAP = 40
COOLDOWN_SEC = 60

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


_ENSURE = "INSERT OR IGNORE INTO wallets (user_id, coins, total_earned) VALUES (?,0,0)"
_SPEND = "UPDATE wallets SET coins = coins - ? WHERE user_id=? AND coins >= ?"


async def ensure(uid: int):
    await x("INSERT OR IGNORE INTO wallets (user_id, coins, total_earned) VALUES (?,0,0)", (uid,))


_WALLET_SQL = "SELECT coins, total_earned, title, last_reward FROM wallets WHERE user_id=?"


async def wallet(uid: int):
    """Кошелёк игрока. Сначала обычное чтение: писательская блокировка нужна, только если кошелька ещё нет.

    Раньше каждый просмотр баланса (меню, проверки ставок) занимал единственного писателя ради INSERT OR IGNORE
    и стоял в одной очереди с выплатами и результатами игр."""
    row = await q(_WALLET_SQL, (uid,), one=True)
    if row is None:
        await ensure(uid)
        row = await q(_WALLET_SQL, (uid,), one=True)
    return row


async def add_coins(uid: int, amount: int):
    async with tx() as t:
        await t.execute(_ENSURE, (uid,))
        await t.execute("UPDATE wallets SET coins = MAX(0, coins + ?), total_earned = total_earned + ? WHERE user_id=?",
                        (amount, max(amount, 0), uid))




async def transfer(src: int, dst: int, amount: int) -> bool:
    """Перевод рубинов от src к dst. False, если у отправителя не хватает рубинов.

    Списание и зачисление идут одной транзакцией: если что-то упало на полпути, откатывается
    всё целиком. Списание условное (coins >= amount), баланс не уйдёт в минус.
    total_earned не трогаем: перевод не «заработок».
    """
    if amount <= 0 or src == dst:
        return False
    async with tx() as t:
        await t.execute(_ENSURE, (src,))
        await t.execute(_ENSURE, (dst,))
        if await t.execute(_SPEND, (amount, src, amount)) != 1:
            return False
        await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (amount, dst))
    return True


async def debit(uid: int, amount: int) -> bool:
    """Списывает ставку. False, если рубинов не хватает (баланс не уходит в минус)."""
    if amount <= 0:
        return False
    async with tx() as t:
        await t.execute(_ENSURE, (uid,))
        return await t.execute(_SPEND, (amount, uid, amount)) == 1


async def credit(uid: int, amount: int):
    """Выплата или возврат ставки. total_earned не трогаем: это не заработок за игры."""
    if amount > 0:
        async with tx() as t:       # одна блокировка и один коммит вместо двух
            await t.execute(_ENSURE, (uid,))
            await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (amount, uid))


async def credit_once(operation_id: str, uid: int, amount: int) -> bool:
    """Идемпотентная выплата: повтор с тем же operation_id не начисляет рубины снова.

    Запись ключа операции и изменение кошелька фиксируются одной транзакцией.
    Возвращает True только если выплата была проведена этим вызовом.
    """
    if not operation_id or amount <= 0:
        return False
    async with tx() as t:
        await t.execute(_ENSURE, (uid,))
        inserted = await t.execute(
            "INSERT OR IGNORE INTO balance_operations (operation_id, user_id, amount) VALUES (?,?,?)",
            (operation_id, uid, amount),
        )
        if inserted != 1:
            return False
        await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (amount, uid))
    return True


# ---------- эскроу: ставки игр на нескольких игроков ----------
# Ставка списывается при старте и лежит в таблице escrow, пока игра идёт. Выплата и возврат идут
# одной транзакцией и идемпотентны: рассчитанная партия (строк в escrow уже нет) повторно не платит.

class _EscrowFail(Exception):
    pass


async def escrow_hold(ref: str, amounts: Dict[int, int], strict: bool = False) -> Dict[int, int]:
    """Списывает ставки и кладёт их в эскроу. amounts: {uid: ставка}, -1 = весь баланс.
    Возвращает {uid: удержано}; кого не удалось списать, в результате нет. strict: либо все, либо никто ({})."""
    held: Dict[int, int] = {}
    try:
        async with tx() as t:
            for uid, amt in amounts.items():
                await t.execute(_ENSURE, (uid,))
                if amt < 0:
                    row = await t.fetchone("SELECT coins FROM wallets WHERE user_id=?", (uid,))
                    amt = row[0] if row else 0
                if amt <= 0 or await t.execute(_SPEND, (amt, uid, amt)) != 1:
                    if strict:
                        raise _EscrowFail()
                    continue
                await t.execute("INSERT INTO escrow (ref, user_id, amount) VALUES (?,?,?) "
                                "ON CONFLICT(ref, user_id) DO UPDATE SET amount = amount + excluded.amount",
                                (ref, uid, amt))
                held[uid] = amt
    except _EscrowFail:
        return {}
    return held


async def escrow_get(ref: str) -> Dict[int, int]:
    return {r[0]: r[1] for r in await q("SELECT user_id, amount FROM escrow WHERE ref=?", (ref,))}


async def escrow_refund(ref: str) -> Dict[int, int]:
    """Возвращает всё, что осталось в эскроу ref. Если уже рассчитано или возвращено, ничего не делает."""
    async with tx() as t:
        rows = await t.fetchall("SELECT user_id, amount FROM escrow WHERE ref=?", (ref,))
        for uid, amt in rows:
            await t.execute(_ENSURE, (uid,))
            await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (amt, uid))
        if rows:
            await t.execute("DELETE FROM escrow WHERE ref=?", (ref,))
    return {u: a for u, a in rows}


async def escrow_payout(ref: str, payouts: Dict[int, int]) -> Optional[Dict[int, int]]:
    """Раздаёт эскроу ref одной транзакцией. None, если ref уже рассчитан. Сумма выплат не может
    превышать банк; недораспределённый остаток возвращается внёсшим пропорционально взносам."""
    async with tx() as t:
        rows = await t.fetchall("SELECT user_id, amount FROM escrow WHERE ref=?", (ref,))
        if not rows:
            return None
        total = sum(a for _, a in rows)
        pay = {u: v for u, v in payouts.items() if v > 0}
        left = total - sum(pay.values())
        if left < 0:
            raise ValueError("выплаты превышают банк эскроу")
        if left:
            given = 0
            for uid, amt in rows:
                part = left * amt // total
                pay[uid] = pay.get(uid, 0) + part
                given += part
            pay[rows[0][0]] += left - given
        for uid, v in pay.items():
            await t.execute(_ENSURE, (uid,))
            await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (v, uid))
        await t.execute("DELETE FROM escrow WHERE ref=?", (ref,))
    return pay


async def escrow_recover() -> int:
    """На старте: игры в памяти не переживают перезапуск, значит всё, что осталось в эскроу, возвращаем."""
    n = 0
    for (ref,) in await q("SELECT DISTINCT ref FROM escrow"):
        n += len(await escrow_refund(ref))
    return n


def escrow_refund_bg(ref: str):
    """Возврат из синхронного кода (finish): запускает задачу и держит на неё ссылку."""
    try:
        t = asyncio.get_running_loop().create_task(escrow_refund(ref))
    except RuntimeError:
        return
    _bg.add(t)
    t.add_done_callback(_bg_done)


def _bg_done(t: asyncio.Task):
    _bg.discard(t)
    if t.cancelled():
        return
    exc = t.exception()
    if exc:
        logger.error("Фоновый возврат эскроу не удался", exc_info=exc)


_bg: set = set()


async def bet(uid: int, stake: int, payout: int) -> bool:
    """Ставка одиночной игры: списание и выплата одной транзакцией. False, если рубинов не хватает."""
    async with tx() as t:
        await t.execute(_ENSURE, (uid,))
        if await t.execute(_SPEND, (stake, uid, stake)) != 1:
            return False
        if payout > 0:
            await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (payout, uid))
    return True


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
            logger.exception("Не удалось отправить анонс экономики")


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
    lines = []
    # Кулдаун, дневной лимит, задания и достижения проверяются и пишутся в одной транзакции:
    # два одновременных результата не выдадут награду дважды.
    async with tx() as t:
        coins, total, title, last = await wallet(uid)
        p = await progress(uid, day)
        rows = {r[0]: (r[1], r[2]) for r in await q(
            "SELECT task, progress, done FROM daily_tasks WHERE user_id=? AND day=?", (uid, day))}
        earned = rows.get("_earned", (0, 0))[0]
        gain, stmts = 0, []
        if game and result:
            ok = True
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
                # Важен результат INSERT OR IGNORE: награда только за реально новое достижение.
                inserted = await t.execute(
                    "INSERT OR IGNORE INTO achievements (user_id, key, earned_at) VALUES (?,?,?)", (uid, key, ts()))
                if inserted:
                    gain += reward(base)
                    lines.append(f"🏆 {{n}} получил(а) достижение «{name}»! (+{reward(base)} рубинов)")
        if gain:
            stmts.append(("UPDATE wallets SET coins = coins + ?, total_earned = total_earned + ? WHERE user_id=?",
                          (gain, gain, uid)))
        for sql, params in stmts:
            await t.execute(sql, params)
        task_state = [(TASKS[k][0], min(values[k], TASKS[k][1]), TASKS[k][1],
                       bool(rows.get(k, (0, 0))[1]) or values[k] >= TASKS[k][1]) for k in TASKS]
    if lines:
        who = await name_of(uid)
        await say("\n".join(l.format(n=who) for l in lines))
    return task_state


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
    title, base = SHOP[key]
    cost = price(base)
    # Проверка «уже куплено» и списание — в одной транзакции с условием coins >= cost:
    # два одновременных нажатия не купят титул дважды и не уведут баланс в минус.
    async with tx() as t:
        await t.execute(_ENSURE, (uid,))
        if await t.fetchone("SELECT 1 FROM purchases WHERE user_id=? AND item=?", (uid, key)):
            return False, "Это уже куплено."
        if await t.execute(_SPEND, (cost, uid, cost)) != 1:
            row = await t.fetchone("SELECT coins FROM wallets WHERE user_id=?", (uid,))
            coins = row[0] if row else 0
            return False, f"Не хватает рубинов: нужно {cost}, у тебя {coins}."
        await t.execute("INSERT INTO purchases (user_id, item, bought_at) VALUES (?,?,?)", (uid, key, ts()))
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
