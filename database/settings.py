"""Настройки бота (таблица settings) с кэшем в памяти."""
from typing import Dict, Optional

from database.core import q, x

_settings: Dict[str, Optional[str]] = {}


def clear_cache():
    _settings.clear()


async def get_setting(key: str, default: Optional[str] = None) -> Optional[str]:
    if key not in _settings:
        row = await q("SELECT value FROM settings WHERE key=?", (key,), one=True)
        _settings[key] = row[0] if row else None
    v = _settings[key]
    return default if v is None else v


async def set_setting(key: str, value: str):
    await x("""INSERT INTO settings (key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))
    _settings[key] = str(value)
