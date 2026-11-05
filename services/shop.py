import random
from datetime import timedelta
from typing import List, Optional, Tuple

from database.core import q, tx
from services import economy as ec
from utils.fmt import plain
from utils.timeutils import now, ts

TICKET_MAX = 3
_ENSURE = "INSERT OR IGNORE INTO wallets (user_id, coins, total_earned) VALUES (?,0,0)"
_SPEND = "UPDATE wallets SET coins = coins - ? WHERE user_id=? AND coins >= ?"


class _OutOfStock(Exception):
    pass


async def _create_order_tx(t, uid: int, kind: str, title: str, payload: str, cost: int,
                           expires: Optional[str]) -> Optional[int]:
    """Создаёт заказ, списывая рубины в той же транзакции. None, если рубинов не хватает."""
    if cost:
        await t.execute(_ENSURE, (uid,))
        if await t.execute(_SPEND, (cost, uid, cost)) != 1:
            return None
    await t.execute("INSERT INTO orders (user_id, kind, title, payload, price, status, created_at, expires_at) "
                    "VALUES (?,?,?,?,?, 'new', ?, ?)", (uid, kind, title, payload, cost, ts(), expires))
    return t.lastrowid


async def create_order(uid: int, kind: str, title: str, payload: str, cost: int,
                       expires: Optional[str] = None) -> Optional[int]:
    async with tx() as t:
        return await _create_order_tx(t, uid, kind, title, payload, cost, expires)


async def get_order(oid: int):
    return await q("SELECT id, user_id, kind, title, payload, price, status FROM orders WHERE id=?", (oid,), one=True)


async def settle(oid: int, approve: bool) -> Optional[Tuple[int, str, str, str]]:
    async with tx() as t:
        o = await t.fetchone("SELECT id, user_id, kind, title, payload, price, status FROM orders WHERE id=?", (oid,))
        if not o or o[6] != "new":
            return None
        status = "approved" if approve else "rejected"
        # Условное обновление: два одновременных нажатия не обработают заказ и не вернут рубины дважды.
        if await t.execute("UPDATE orders SET status=?, handled_at=? WHERE id=? AND status='new'",
                           (status, ts(), oid)) != 1:
            return None
        _, uid, kind, title, payload, cost, _ = o
        if not approve:
            if cost:
                await t.execute(_ENSURE, (uid,))
                await t.execute("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (cost, uid))
            if kind == "prize":
                await t.execute("UPDATE shop_items SET stock = stock + 1 WHERE id=? AND stock >= 0", (int(payload),))
    return uid, kind, title, payload


def tag_expiry() -> str:
    return ts(now() + timedelta(days=ec.TAG_DAYS))


async def buy_prize(uid: int, item_id: int) -> Tuple[bool, str, Optional[int]]:
    try:
        async with tx() as t:
            row = await t.fetchone("SELECT title, price, stock FROM shop_items WHERE id=? AND active=1", (item_id,))
            if not row:
                return False, "Этот товар недоступен.", None
            title, base, stock = row
            cost = ec.price(base)
            if stock == 0:
                return False, "Товар закончился.", None
            await t.execute(_ENSURE, (uid,))
            coins = (await t.fetchone("SELECT coins FROM wallets WHERE user_id=?", (uid,)))[0]
            if coins < cost:
                return False, f"Не хватает рубинов: нужно {cost}, у тебя {coins}.", None
            oid = await _create_order_tx(t, uid, "prize", title, str(item_id), cost, None)
            if oid is None:
                return False, "Не хватает рубинов.", None
            if stock > 0 and await t.execute(
                    "UPDATE shop_items SET stock = stock - 1 WHERE id=? AND stock > 0", (item_id,)) != 1:
                raise _OutOfStock()      # откатит и списание, и заказ
    except _OutOfStock:
        return False, "Товар закончился.", None
    return True, f"Заказ отправлен админу: {plain(title)}.", oid


async def tickets_of(gid: int, uid: int) -> int:
    r = await q("SELECT tickets FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (gid, uid), one=True)
    return r[0] if r else 0


async def buy_ticket(uid: int, gid: int) -> Tuple[bool, str]:
    cost = ec.price(ec.TICKET_PRICE)
    async with tx() as t:
        row = await t.fetchone("SELECT tickets FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (gid, uid))
        have = row[0] if row else 0
        if not have:
            return False, "Сначала нужно участвовать в розыгрыше кнопкой «Участвую»."
        if have > TICKET_MAX:
            return False, f"Можно взять не больше {TICKET_MAX} дополнительных билетов."
        await t.execute(_ENSURE, (uid,))
        coins = (await t.fetchone("SELECT coins FROM wallets WHERE user_id=?", (uid,)))[0]
        if coins < cost:
            return False, f"Не хватает рубинов: нужно {cost}, у тебя {coins}."
        if await t.execute(_SPEND, (cost, uid, cost)) != 1:
            return False, f"Не хватает рубинов: нужно {cost}."
        await t.execute("UPDATE giveaway_entries SET tickets = tickets + 1 WHERE giveaway_id=? AND user_id=?",
                        (gid, uid))
    return True, f"Билет куплен. Всего билетов: {have + 1}."


def weighted_pick(pool: List[Tuple[int, int]], k: int) -> List[int]:
    pool = list(pool)
    out = []
    while pool and len(out) < k:
        pick = random.choices(range(len(pool)), weights=[max(1, w) for _, w in pool])[0]
        out.append(pool.pop(pick)[0])
    return out
