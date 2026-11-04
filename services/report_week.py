from datetime import timedelta

from database.core import q
from database.messages import flush_messages
from utils.fmt import esc
from utils.timeutils import now, ts

GAME_NAMES = {"ttt": "крестики-нолики", "rps": "КНБ", "duel": "дуэль", "mafia": "мафия",
              "memory": "мемори", "code": "взломай код", "assoc": "ассоциации", "bunker": "бункер",
              "roulette": "рулетка",
              "wordseek": "WordSeek", "seabattle": "морской бой", "hangman": "виселица"}
ACTION_NAMES = {"warn": "предупреждений", "mute": "мутов", "unmute": "снятых мутов", "appeal": "решений по жалобам",
                "game_ban": "банов в играх", "uncoins": "списаний рубинов"}


async def build() -> str:
    await flush_messages()
    cutoff = ts(now() - timedelta(days=7))
    msgs = (await q("SELECT COUNT(*) FROM messages WHERE created_at >= ? AND user_id != 0", (cutoff,), one=True))[0]
    active = (await q("SELECT COUNT(DISTINCT user_id) FROM messages WHERE created_at >= ? AND user_id != 0",
                      (cutoff,), one=True))[0]
    newcomers = (await q("SELECT COUNT(*) FROM users WHERE first_seen >= ?", (cutoff,), one=True))[0]
    games = (await q("SELECT COUNT(*) FROM game_log WHERE played_at >= ?", (cutoff,), one=True))[0]
    popular = await q("SELECT game_key, COUNT(*) c FROM game_log WHERE played_at >= ? GROUP BY game_key "
                      "ORDER BY c DESC LIMIT 3", (cutoff,))
    actions = await q("SELECT action, COUNT(*) FROM mod_log WHERE created_at >= ? GROUP BY action", (cutoff,))
    appeals = (await q("SELECT COUNT(*) FROM appeals WHERE created_at >= ?", (cutoff,), one=True))[0]
    reports = (await q("SELECT COUNT(*) FROM reports WHERE created_at >= ?", (cutoff,), one=True))[0]
    text = f"📋 <b>Отчёт за неделю</b>\n\nСообщений: <b>{msgs}</b>, писали: <b>{active}</b> чел.\n"
    text += f"Новых участников в базе: <b>{newcomers}</b>\n"
    text += f"\nИгровых партий (участий): <b>{games}</b>\n"
    if popular:
        text += "Популярные игры: " + ", ".join(f"{GAME_NAMES.get(k, k)} ({c})" for k, c in popular) + "\n"
    shown = [f"{c} {ACTION_NAMES[a]}" for a, c in actions if a in ACTION_NAMES]
    text += "\n<b>Модерация:</b> " + (", ".join(shown) if shown else "без происшествий") + "\n"
    text += f"Жалоб от участников: {reports}, обжалований: {appeals}"
    return text
