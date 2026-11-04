import random
from datetime import timedelta
from typing import List, Optional, Tuple

from database.core import batch, q, x
from services import economy as ec
from utils.fmt import plain
from utils.timeutils import now, ts

TICKET_MAX = 3


async def create_order(uid: int, kind: str, title: str, payload: str, cost: int,
                       expires: Optional[str] = None) -> int:
    await batch([("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (cost, uid))] if cost else [])
    return await x("INSERT INTO orders (user_id, kind, title, payload, price, status, created_at, expires_at) "
                   "VALUES (?,?,?,?,?, 'new', ?, ?)", (uid, kind, title, payload, cost, ts(), expires))


async def get_order(oid: int):
    return await q("SELECT id, user_id, kind, title, payload, price, status FROM orders WHERE id=?", (oid,), one=True)


async def settle(oid: int, approve: bool) -> Optional[Tuple[int, str, str, str]]:
    o = await get_order(oid)
    if not o or o[6] != "new":
        return None
    _, uid, kind, title, payload, cost, _ = o
    status = "approved" if approve else "rejected"
    stmts = [("UPDATE orders SET status=?, handled_at=? WHERE id=?", (status, ts(), oid))]
    if not approve:
        if cost:
            stmts.append(("UPDATE wallets SET coins = coins + ? WHERE user_id=?", (cost, uid)))
        if kind == "prize":
            stmts.append(("UPDATE shop_items SET stock = stock + 1 WHERE id=? AND stock >= 0", (int(payload),)))
    await batch(stmts)
    return uid, kind, title, payload


def tag_expiry() -> str:
    return ts(now() + timedelta(days=ec.TAG_DAYS))


async def buy_prize(uid: int, item_id: int) -> Tuple[bool, str, Optional[int]]:
    row = await q("SELECT title, price, stock FROM shop_items WHERE id=? AND active=1", (item_id,), one=True)
    if not row:
        return False, "Этот товар недоступен.", None
    title, base, stock = row
    cost = ec.price(base)
    if stock == 0:
        return False, "Товар закончился.", None
    coins = (await ec.wallet(uid))[0]
    if coins < cost:
        return False, f"Не хватает монет: нужно {cost}, у тебя {coins}.", None
    if stock > 0:
        await x("UPDATE shop_items SET stock = stock - 1 WHERE id=?", (item_id,))
    oid = await create_order(uid, "prize", title, str(item_id), cost)
    return True, f"Заказ отправлен админу: {plain(title)}.", oid


async def tickets_of(gid: int, uid: int) -> int:
    r = await q("SELECT tickets FROM giveaway_entries WHERE giveaway_id=? AND user_id=?", (gid, uid), one=True)
    return r[0] if r else 0


async def buy_ticket(uid: int, gid: int) -> Tuple[bool, str]:
    have = await tickets_of(gid, uid)
    if not have:
        return False, "Сначала нужно участвовать в розыгрыше кнопкой «Участвую»."
    if have > TICKET_MAX:
        return False, f"Можно взять не больше {TICKET_MAX} дополнительных билетов."
    cost = ec.price(ec.TICKET_PRICE)
    coins = (await ec.wallet(uid))[0]
    if coins < cost:
        return False, f"Не хватает монет: нужно {cost}, у тебя {coins}."
    await batch([("UPDATE wallets SET coins = coins - ? WHERE user_id=?", (cost, uid)),
                 ("UPDATE giveaway_entries SET tickets = tickets + 1 WHERE giveaway_id=? AND user_id=?", (gid, uid))])
    return True, f"Билет куплен. Всего билетов: {have + 1}."


def weighted_pick(pool: List[Tuple[int, int]], k: int) -> List[int]:
    pool = list(pool)
    out = []
    while pool and len(out) < k:
        pick = random.choices(range(len(pool)), weights=[max(1, w) for _, w in pool])[0]
        out.append(pool.pop(pick)[0])
    return out
