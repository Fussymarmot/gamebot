from typing import Optional

import aiohttp

from config import AI_BASE_URL, AI_KEY, AI_MODEL
from database.settings import get_setting, set_setting
from utils.timeutils import now

URL = f"{AI_BASE_URL}/v1/messages"
SYSTEM = ("Ты рассказчик в игре «Бункер». Игроки выживают в бункере после катастрофы, голосуют за "
          "решения на событиях, запасы еды и воды ограничены. Пиши только о том, что указано в запросе, ничего не "
          "выдумывай про людей и не добавляй новые события. Пиши по-русски, весело и по-доброму, без имён людей, "
          "без грубостей и без упоминаний болезней, внешности и личных тем. "
          "Отвечай одним коротким абзацем из 1-3 предложений.")


async def enabled() -> bool:
    return bool(AI_KEY) and (await get_setting("ai_enabled", "0")) == "1"


async def used_today() -> int:
    return int(await get_setting(f"ai_used_{now().strftime('%Y-%m-%d')}", "0"))


async def limit() -> int:
    return int(await get_setting("ai_daily_limit", "50"))


async def ask(prompt: str, max_tokens: int = 600, timeout: float = 8.0, system: Optional[str] = None) -> Optional[str]:
    if not await enabled():
        return None
    used = await used_today()
    if used >= await limit():
        return None
    await set_setting(f"ai_used_{now().strftime('%Y-%m-%d')}", str(used + 1))
    body = {"model": AI_MODEL, "max_tokens": max_tokens, "system": system or SYSTEM,
            "messages": [{"role": "user", "content": prompt}]}
    headers = {"x-api-key": AI_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(URL, json=body, headers=headers) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if data.get("stop_reason") == "max_tokens":
            cut = max(text.rfind("."), text.rfind("!"), text.rfind("?"), text.rfind("…"))
            if cut > 20:
                text = text[:cut + 1]
        return text[:900] or None
    except Exception:
        return None
