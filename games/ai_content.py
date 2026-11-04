import logging
import random
import re
from typing import List, Optional

from database.core import q, x
from services import ai

log = logging.getLogger(__name__)
SYSTEM = ("Ты помощник в игре для группы друзей. Пиши по-русски, весело и по-доброму, "
          "без имён людей, грубостей, мата, политики, внешности, болезней и личных тем. "
          "Следуй формату ответа из запроса и ничего не добавляй сверх него.")
BAD = re.compile(r"(?i)\b(?:хуй|пизд|бляд|ебан|сука|гандон|педик|нацист|секс|наркот|убий|смерт)")
WORD = re.compile(r"^[а-яё]{5}$")
THEMES = ("природа", "еда", "животные", "космос", "спорт", "дом", "музыка", "транспорт", "одежда")


async def assoc_start() -> Optional[str]:
    text = await ai.ask("Назови одно простое русское существительное в начальной форме для игры в ассоциации "
                        "(бытовая или природная тема). Ответ: только одно слово, без точки.",
                        max_tokens=20, timeout=8.0, system=SYSTEM)
    word = (text or "").strip().lower().strip(".!?«»\"' ")
    if re.fullmatch(r"[а-яё]{3,15}", word) and not BAD.search(word):
        return word
    return None


async def memory_comment(scores: List[int]) -> Optional[str]:
    if not scores:
        return None
    text = await ai.ask(
        f"Игра на память закончилась. Очки игроков по убыванию: {', '.join(map(str, scores))}. "
        "Напиши одну короткую весёлую фразу (до 120 символов) с итогом для группы, без имён.",
        max_tokens=120, timeout=8.0, system=SYSTEM)
    if text and not BAD.search(text):
        return text.strip()
    return None


async def word_known(word: str) -> Optional[bool]:
    row = await q("SELECT valid FROM word_cache WHERE word=?", (word,), one=True)
    if row is not None:
        return bool(row[0])
    text = await ai.ask(f"Существует ли в русском языке слово «{word}» (нарицательное существительное в именительном "
                        "падеже, допустимо в игре «Слова из 5 букв»)? Ответь одним словом: ДА или НЕТ.",
                        max_tokens=5, timeout=8.0, system=SYSTEM)
    if not text:
        return None
    verdict = text.strip().upper().startswith("ДА")
    await x("INSERT OR REPLACE INTO word_cache (word, valid, hint) VALUES (?,?,NULL)", (word, 1 if verdict else 0))
    return verdict


async def secret_word() -> Optional[str]:
    theme = random.choice(THEMES)
    text = await ai.ask(f"Загадай одно русское существительное ровно из 5 букв в именительном падеже, единственное "
                        f"число, тема «{theme}», простое и известное всем. Ответ: только слово, без точки.",
                        max_tokens=15, timeout=8.0, system=SYSTEM)
    word = (text or "").strip().lower().replace("ё", "е").strip(".!?«»\"' ")
    if WORD.fullmatch(word) and not BAD.search(word):
        await x("INSERT OR IGNORE INTO word_cache (word, valid, hint) VALUES (?,1,NULL)", (word,))
        return word
    return None


async def hint_for(word: str) -> Optional[str]:
    text = await ai.ask(f"Загаданное слово: «{word}». Напиши одну короткую подсказку (до 80 символов) о его значении, "
                        "не называя само слово и однокоренные слова.",
                        max_tokens=80, timeout=8.0, system=SYSTEM)
    if text and word not in text.lower().replace("ё", "е") and not BAD.search(text):
        return text.strip()
    return None
