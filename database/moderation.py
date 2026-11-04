"""Чёрный список игр и журнал действий модерации."""
from database.core import q, x
from utils.timeutils import ts


async def is_user_banned(user_id: int) -> bool:
    return bool(await q("SELECT 1 FROM banned WHERE user_id=?", (user_id,), one=True))


async def ban_user(user_id: int):
    await x("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (user_id,))


async def unban_user(user_id: int):
    await x("DELETE FROM banned WHERE user_id=?", (user_id,))


async def log_mod(admin_id: int, target_id: int, action: str, details: str = ""):
    await x("INSERT INTO mod_log (admin_id, target_id, action, details, created_at) VALUES (?,?,?,?,?)",
            (admin_id, target_id, action, details, ts()))
