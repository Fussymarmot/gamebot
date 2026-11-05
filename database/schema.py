"""Создание таблиц и мелкие миграции колонок."""
from database.core import tx

TABLES = [
    """CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        last_name TEXT, updated_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, chat_id INTEGER,
        text TEXT, created_at TEXT)""",
    "CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at)",
    "CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id)",
    """CREATE TABLE IF NOT EXISTS game_stats (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game_type TEXT,
        wins INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0,
        UNIQUE(user_id, game_type))""",
    """CREATE TABLE IF NOT EXISTS mafia_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT, winner TEXT, roles_json TEXT, created_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS banned (user_id INTEGER PRIMARY KEY)",
    """CREATE TABLE IF NOT EXISTS mutes (
        user_id INTEGER PRIMARY KEY, until_date TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS perm_tags (
        user_id INTEGER, chat_id INTEGER, tag TEXT, PRIMARY KEY (user_id, chat_id))""",
    """CREATE TABLE IF NOT EXISTS members (
        user_id INTEGER PRIMARY KEY, is_member INTEGER DEFAULT 1, checked_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS warns (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, admin_id INTEGER, reason TEXT,
        created_at TEXT, active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS reports (
        id INTEGER PRIMARY KEY AUTOINCREMENT, reporter_id INTEGER, target_id INTEGER,
        target_name TEXT, text TEXT, status TEXT DEFAULT 'new', handled_by INTEGER,
        created_at TEXT, handled_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS wallets (
        user_id INTEGER PRIMARY KEY, coins INTEGER DEFAULT 0, total_earned INTEGER DEFAULT 0,
        title TEXT, last_reward TEXT)""",
    """CREATE TABLE IF NOT EXISTS achievements (
        user_id INTEGER, key TEXT, earned_at TEXT, PRIMARY KEY (user_id, key))""",
    """CREATE TABLE IF NOT EXISTS daily_tasks (
        user_id INTEGER, day TEXT, task TEXT, progress INTEGER DEFAULT 0, done INTEGER DEFAULT 0,
        PRIMARY KEY (user_id, day, task))""",
    """CREATE TABLE IF NOT EXISTS purchases (
        user_id INTEGER, item TEXT, bought_at TEXT, PRIMARY KEY (user_id, item))""",
    """CREATE TABLE IF NOT EXISTS game_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, game_key TEXT, result TEXT,
        played_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS shop_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, price INTEGER, stock INTEGER DEFAULT -1,
        active INTEGER DEFAULT 1)""",
    """CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, title TEXT, payload TEXT,
        price INTEGER, status TEXT DEFAULT 'new', created_at TEXT, handled_at TEXT, expires_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS game_mutes (
        user_id INTEGER, chat_id INTEGER, PRIMARY KEY (user_id, chat_id))""",
    """CREATE TABLE IF NOT EXISTS private_users (
        user_id INTEGER PRIMARY KEY, first_seen TEXT, blocked INTEGER DEFAULT 0)""",
    """CREATE TABLE IF NOT EXISTS giveaways (
        id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, prize TEXT, kind TEXT,
        metric TEXT, metric_days INTEGER DEFAULT 7, cond_type TEXT DEFAULT 'none',
        cond_value INTEGER DEFAULT 0, winners INTEGER DEFAULT 1, ends_at TEXT,
        status TEXT DEFAULT 'active', message_id INTEGER, result TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS giveaway_entries (
        giveaway_id INTEGER, user_id INTEGER, joined_at TEXT,
        PRIMARY KEY (giveaway_id, user_id))""",
    """CREATE TABLE IF NOT EXISTS moderators (
        user_id INTEGER PRIMARY KEY, perms TEXT, until TEXT, added_by INTEGER, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS appeals (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'new',
        created_at TEXT, handled_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS word_cache (
        word TEXT PRIMARY KEY, valid INTEGER, hint TEXT)""",
    """CREATE TABLE IF NOT EXISTS mod_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, admin_id INTEGER, target_id INTEGER,
        action TEXT, details TEXT, created_at TEXT)""",
]

# (таблица, колонка, определение): добавляются к старым базам, где колонки ещё нет
COLUMNS = [
    ("messages", "message_id", "INTEGER"),
    ("users", "first_seen", "TEXT"),
    ("wallets", "title_at", "TEXT"),
    ("giveaway_entries", "tickets", "INTEGER DEFAULT 1"),
]


async def init_db():
    async with tx() as t:
        for sql in TABLES:
            await t.execute(sql)
        for table, column, definition in COLUMNS:
            if column not in [r[1] for r in await t.fetchall(f"PRAGMA table_info({table})")]:
                await t.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await t.execute("CREATE INDEX IF NOT EXISTS idx_game_log_user ON game_log(user_id, played_at)")
        await t.execute("CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, created_at)")
