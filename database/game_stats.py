"""Результаты игр: счётчики побед/поражений, журнал партий и рейтинг."""
import logging
from typing import List, Optional, Tuple

from database.core import q, x
from utils.timeutils import ts

log = logging.getLogger(__name__)

# Подписчики на результат партии (например, экономика начисляет монеты)
RESULT_HOOKS: list = []


async def add_game_result(user_id: int, game_type: str, result: str):
    field = {"win": "wins", "loss": "losses", "draw": "draws"}[result]
    await x(f"""INSERT INTO game_stats (user_id, game_type, {field}) VALUES (?,?,1)
                ON CONFLICT(user_id, game_type) DO UPDATE SET {field} = {field} + 1""",
            (user_id, game_type))
    await x("INSERT INTO game_log (user_id, game_key, result, played_at) VALUES (?,?,?,?)",
            (user_id, game_type, result, ts()))
    for hook in RESULT_HOOKS:
        try:
            await hook(user_id, game_type, result)
        except Exception:
            log.exception("Ошибка обработчика результата игры")


async def get_game_top(game_type: Optional[str], limit: int = 3) -> List[Tuple]:
    where = "WHERE g.game_type = ?" if game_type else ""
    params = (game_type, limit) if game_type else (limit,)
    return await q(f"""SELECT u.first_name, u.username, SUM(g.wins) AS w, SUM(g.losses) AS l,
                       SUM(g.draws) AS d
                       FROM game_stats g LEFT JOIN users u ON u.user_id = g.user_id
                       {where} GROUP BY g.user_id HAVING w > 0
                       ORDER BY w DESC, (w * 1.0 / (w + l + d)) DESC, (w + l + d) ASC
                       LIMIT ?""", params)
