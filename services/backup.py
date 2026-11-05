import asyncio
import glob
import gzip
import shutil
import os
import sqlite3
from datetime import datetime

from aiogram import Bot
from aiogram.types import FSInputFile

from config import ADMIN_ID, BASE_DIR
from database import lifecycle
from database.core import DB_PATH
from database.messages import flush_messages
from utils.logs import logger
from utils.timeutils import TZ

BACKUP_DIR = os.path.join(BASE_DIR, "backups")
KEEP = 7
REQUIRED_TABLES = {"users", "messages", "settings"}


def _make_backup_sync() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now(TZ).strftime("%Y-%m-%d_%H-%M-%S")
    raw = os.path.join(BACKUP_DIR, f"bot_backup_{stamp}.db")
    path = raw + ".gz"
    src = sqlite3.connect(DB_PATH)
    dst = sqlite3.connect(raw)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    with open(raw, "rb") as f_in, gzip.open(path, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    os.remove(raw)
    files = sorted(glob.glob(os.path.join(BACKUP_DIR, "bot_backup_*")))
    for old in files[:-KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path


async def make_backup() -> str:
    await flush_messages()
    return await asyncio.to_thread(_make_backup_sync)


async def send_backup(bot: Bot, reason: str = "автоматический") -> bool:
    try:
        path = await make_backup()
        size = os.path.getsize(path)
        if size > 45 * 1024 * 1024:
            await bot.send_message(ADMIN_ID, "⚠️ База больше 45 МБ, Telegram не примет файл. "
                                             f"Копия сохранена в папке backups: {os.path.basename(path)}")
            return False
        await bot.send_document(
            ADMIN_ID, FSInputFile(path),
            caption=f"💾 Бэкап базы ({reason})\n{os.path.basename(path)}\n{size / 1024:.0f} КБ (сжатый файл)")
        return True
    except Exception:
        logger.exception("Не удалось отправить бэкап (%s)", reason)
        return False


def unpack(path: str) -> str:
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic != b"\x1f\x8b":
        return path
    out = path + ".unpacked.db"
    with gzip.open(path, "rb") as f_in, open(out, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)
    return out


def _validate_sync(path: str):
    try:
        path = unpack(path)
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                return False, "Файл повреждён (integrity_check)."
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            con.close()
    except sqlite3.Error as ex:
        return False, f"Это не база SQLite: {ex}"
    missing = REQUIRED_TABLES - tables
    if missing:
        return False, "В файле нет таблиц: " + ", ".join(sorted(missing))
    return True, "ok"


async def validate_backup(path: str):
    return await asyncio.to_thread(_validate_sync, path)


async def restore_backup(path: str):
    ok, msg = await validate_backup(path)
    if not ok:
        return False, msg
    await make_backup()
    await lifecycle.replace_database(await asyncio.to_thread(unpack, path))
    return True, "База восстановлена."
