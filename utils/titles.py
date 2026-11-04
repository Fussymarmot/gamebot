import json
import re
from typing import List, Tuple

from database.settings import get_setting, set_setting

MAX_LEN = 20
TAG_MAX_LEN = 16
DEFAULTS = ["Чемпион", "Легенда", "Шутник", "Тихоня", "Геймер"]
BAD = re.compile(
    r"(хуй|хуе|хуя|пизд|ебан|ебат|ебал|блят|бляд|сука|сучк|мудак|мудил|залуп|гандон|пидор|пидар|пидр|"
    r"шлюх|долбо|лох|лошар|дебил|идиот|урод|чмо|даун|тупой|тупая|придур|кретин|говно|гавно)", re.I)
LINK = re.compile(r"https?://|t\.me|www\.|@|\.ru\b|\.com\b|\.net\b", re.I)


def check(text: str, limit: int = MAX_LEN) -> Tuple[bool, str, str]:
    cleaned = " ".join((text or "").replace("<", "").replace(">", "").replace("&", "").split())
    if not cleaned:
        return False, "Пустой текст.", ""
    if len(cleaned) > limit:
        return False, f"Не длиннее {limit} символов (у тебя {len(cleaned)}).", ""
    if LINK.search(cleaned):
        return False, "Без ссылок и @.", ""
    letters = [c for c in cleaned if c.isalpha()]
    if len(letters) >= 6 and all(c.isupper() for c in letters):
        return False, "Не пиши капсом.", ""
    if BAD.search(cleaned.lower().replace("ё", "е")):
        return False, "Такое нельзя: без оскорблений.", ""
    return True, "", cleaned


async def presets() -> List[str]:
    raw = await get_setting("title_presets", "")
    try:
        data = json.loads(raw) if raw else DEFAULTS
        return [str(t)[:MAX_LEN] for t in data] or DEFAULTS
    except ValueError:
        return DEFAULTS


async def save_presets(items: List[str]):
    await set_setting("title_presets", json.dumps(items, ensure_ascii=False))


async def premoderation() -> bool:
    return (await get_setting("title_premod", "0")) == "1"
