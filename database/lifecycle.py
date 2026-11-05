"""Жизненный цикл базы: закрытие, обслуживание, подмена файла при восстановлении из бэкапа."""
import os
import shutil

from database import settings, users
from database.core import DB_PATH, disconnect, exclusive
from database.messages import flush_messages
from database.schema import init_db


def invalidate_caches():
    settings.clear_cache()
    users.clear_cache()


async def close():
    await flush_messages()
    await disconnect()


async def maintenance(vacuum: bool = False):
    await flush_messages()
    async with exclusive() as c:  # VACUUM нельзя внутри транзакции, поэтому просто занимаем писателя
        for sql in ("PRAGMA wal_checkpoint(TRUNCATE)", "PRAGMA optimize") + (("VACUUM",) if vacuum else ()):
            cur = await c.execute(sql)
            await cur.close()


async def replace_database(new_path: str):
    await close()
    for suffix in ("-wal", "-shm"):
        try:
            os.remove(DB_PATH + suffix)
        except FileNotFoundError:
            pass
    shutil.copyfile(new_path, DB_PATH)
    invalidate_caches()
    await init_db()
