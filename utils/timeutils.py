"""Время: часовые пояса и форматирование меток времени, которые хранятся в базе."""
from datetime import datetime
from typing import Optional

import pytz

from config import TIMEZONE

TZ = pytz.timezone(TIMEZONE)
MSK = pytz.timezone("Europe/Moscow")
FMT = "%Y-%m-%d %H:%M:%S"


def now() -> datetime:
    return datetime.now(TZ).replace(tzinfo=None)


def ts(dt: Optional[datetime] = None) -> str:
    return (dt or now()).strftime(FMT)


def parse_ts(s: str) -> datetime:
    return datetime.strptime(str(s)[:19], FMT)
