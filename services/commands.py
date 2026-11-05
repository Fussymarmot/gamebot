"""Реестр команд бота и установка меню команд в Telegram.

COMMANDS: (команда, название, строка справки, раздел, только_админ, в_меню).
Разделы (SECTIONS) используются в /help и в админ-панели «Функции».
"""

from aiogram import Bot
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats,
                           BotCommandScopeChat, BotCommandScopeChatMember)

from config import ADMIN_ID, GROUP_ID
from services import features


SECTIONS = [
    ("general", "info", "Общее"),
    ("games", "game", "Игры"),
    ("profile", "gold", "Профиль и рубины"),
    ("support", "flag", "Связь с админом"),
    ("admin", "lock", "Только для админа"),
]

COMMANDS = [
    ("help", "Справка", "/help: список всех команд", "general", False, True),
    ("start", "Начать", "/start: приветствие и обновление бота", "general", False, False),
    ("games", "Игры", "/games: все игры кнопками", "games", False, True),
    ("top_games", "Рейтинги игр", "/top_games: топ по каждой игре", "games", False, False),
    ("giveaways", "Розыгрыши", "/giveaways: активные розыгрыши", "games", False, False),
    ("ttt", "Крестики-нолики", "/ttt [ставка]: крестики-нолики, играть может любой желающий", "games", False, False),
    ("rps", "Камень-ножницы-бумага", "/rps [ставка]: камень, ножницы, бумага", "games", False, False),
    ("duel", "Дуэль", "/duel [ставка]: дуэль на скорость", "games", False, False),
    ("mafia", "Мафия", "/mafia: мафия, 5+ игроков", "games", False, False),
    ("bunker", "Бункер", "/bunker: «Бункер: 60 секунд», 4-12 игроков", "games", False, False),
    ("roulette", "Русская рулетка", "/roulette [ставка]: русская рулетка, 2-6 игроков", "games", False, False),
    ("poker", "Покер", "/poker [ставка]: техасский холдем, 2-8 игроков; без ставки или на весь баланс", "games", False, False),
    ("blackjack", "Блэкджек", "/blackjack [ставка]: блэкджек, 2-8 игроков, кто ближе к 21, тот выиграл; карты в личке (ещё, хватит, выйти)", "games", False, False),
    ("wordseek", "WordSeek", "/wordseek: угадай слово из 5 букв", "games", False, False),
    ("seabattle", "Морской бой", "/seabattle [ставка]: морской бой, карта в личке (/seabattle bot: против бота)", "games", False, False),
    ("hangman", "Виселица", "/hangman [ставка]: виселица, слово загадываешь в личке с ботом", "games", False, False),
    ("coin", "Орёл и решка", "/coin [ставка]: орёл или решка, угадай сторону монеты — ставка удвоится", "games", False, False),
    ("slot", "Слот-машина", "/slot [ставка]: слот-машина 🎰, три разных — проигрыш, два одинаковых — ставка возвращается, три — умножение (7️⃣ ×10, BAR ×6, 🍇 ×5, 🍋 ×4)", "games", False, False),
    ("mines", "Сапёр", "/mines [ставка]: сапёр, открывай клетки с рубинами и забирай выигрыш, пока не наступил на мину", "games", False, False),
    ("tower", "Башня", "/tower [ставка]: башня, поднимайся по этажам, выбирая безопасную клетку, и забирай выигрыш, пока не наступил на мину", "games", False, False),
    ("bowling", "Боулинг", "/bowling [ставка]: боулинг 🎳, страйк ×3, бонус: серия страйков подряд повышает множитель до ×5", "games", False, False),
    ("darts", "Дартс", "/darts [ставка]: дартс 🎯, яблочко ×2.5, бонус: после яблочка бонусный дротик, второе яблочко даёт ×4", "games", False, False),
    ("dice", "Кубик", "/dice [ставка]: кубик, шестёрка ×2, бонус: после шестёрки бонусный бросок добавляет число/10 ставки", "games", False, False),
    ("basket", "Баскетбол", "/basket [ставка]: баскетбол 🏀, чистое попадание ×2.5, бонус: хет-трик (3 попадания подряд) даёт +1 ставку", "games", False, False),
    ("memory", "Мемори", "/memory: запомни эмодзи", "games", False, False),
    ("code", "Взломай код", "/code: угадай код из 4 цифр", "games", False, False),
    ("assoc", "Ассоциации", "/assoc: цепочка слов", "games", False, False),
    ("profile", "Профиль", "/profile: профиль, звания, достижения", "profile", False, True),
    ("balance", "Баланс рубинов", "/balance: сколько у тебя рубинов", "profile", False, False),
    ("pay", "Перевод рубинов", "/pay N: перевести рубины (ответом на сообщение, либо /pay @username N или ID N)", "profile", False, False),
    ("topcoins", "Топ по рубинам", "/topcoins: у кого больше всех рубинов", "profile", False, False),
    ("achievements", "Достижения", "/achievements: список достижений", "profile", False, False),
    ("daily", "Задания дня", "/daily: ежедневные задания", "profile", False, False),
    ("shop", "Магазин", "/shop: титулы, билеты, тег, призы", "profile", False, False),
    ("title", "Своё звание", "/title текст: своё звание до 20 символов", "profile", False, False),
    ("mytag", "Тег на 7 дней", "/mytag текст: тег рядом с именем (после покупки)", "profile", False, False),
    ("stats", "Статистика бота", "/stats: общая статистика", "profile", False, False),
    ("report", "Жалоба", "/report @username или ID описание: жалоба админу", "support", False, False),
    ("admin", "Админ-панель", "/admin: панель (в личке с ботом)", "admin", True, True),
    ("mute", "Мут", "/mute @username или ID: мут с выбором времени", "admin", True, False),
    ("unmute", "Снять мут", "/unmute @username или ID", "admin", True, False),
    ("mutes", "Список мутов", "/mutes", "admin", True, False),
    ("tag", "Вечный тег", "/tag @username или ID, текст", "admin", True, False),
    ("untag", "Снять тег", "/untag @username или ID", "admin", True, False),
    ("tags", "Список тегов", "/tags", "admin", True, False),
    ("warn", "Предупреждение", "/warn @username или ID причина", "admin", True, False),
    ("unwarn", "Снять предупреждение", "/unwarn @username или ID", "admin", True, False),
    ("warns", "История предупреждений", "/warns @username или ID", "admin", True, False),
    ("reports", "Открытые жалобы", "/reports", "admin", True, False),
    ("adminlog", "Журнал действий", "/adminlog", "admin", True, False),
    ("clear", "Очистка чата", "/clear 50, /clear all, /clear bot, /clear 20 @username или ID: удалить сообщения", "admin", True, False),
    ("coins", "Рубины", "/coins @username или ID ±N причина", "admin", True, False),
    ("uncoins", "Забрать рубины", "/uncoins @username или ID N причина: забрать N рубинов (all: все)", "admin", True, False),
    ("gameban", "Бан в играх", "/gameban @username или ID: запретить играть", "admin", True, False),
    ("gameunban", "Снять бан в играх", "/gameunban @username или ID", "admin", True, False),
    ("pause", "Пауза игр", "/pause и /resume: пауза и продолжение игр", "admin", True, False),
    ("resume", "Продолжить игры", "/resume", "admin", True, False),
    ("stopgame", "Остановить игру", "/stopgame: остановить игру (создатель или админ)", "admin", True, False),
    ("addmod", "Назначить модератора", "/addmod @username или ID [дни]: модератор (без срока, если дни не указаны)", "admin", True, False),
    ("delmod", "Снять модератора", "/delmod @username или ID", "admin", True, False),
    ("mods", "Список модераторов", "/mods", "admin", True, False),
    ("richtest", "Проверка Rich", "/richtest: проверить таблицы с кнопками (Bot API 10.3)", "admin", True, False),
    ("weekly", "Недельный отчёт", "/weekly: отчёт за неделю", "admin", True, False),
    ("emoji_id", "ID эмодзи", "/emoji_id: ID премиум-эмодзи в сообщении", "admin", True, False),
]


MOD_PERMS = {"mute": "mute", "unmute": "mute", "mutes": "mute", "warn": "warn", "unwarn": "warn", "warns": "warn"}
BY_NAME = {c[0]: c for c in COMMANDS}


def menu_commands(admin: bool):
    return [(n, d) for n, d, _, _, adm, menu in COMMANDS if menu and (admin or not adm) and features.is_on(n)]


def help_sections(admin: bool, private: bool = False, mod_perms=None):
    out = []
    for key, emoji, title in SECTIONS:
        if key == "admin":
            if not (admin and private):
                continue
        elif not features.section_on(key):
            continue
        lines = [u for n, d, u, s, adm, m in COMMANDS
                 if s == key and n not in ("resume", "start") and features.is_on(n)]
        if lines:
            out.append((emoji, title, lines))
    if mod_perms and not admin and private:
        lines = [BY_NAME[n][2] for n, p in MOD_PERMS.items() if p in mod_perms]
        if lines:
            out.append(("lock", "Модерация", lines))
    return out


def build(admin: bool):
    return [BotCommand(command=n, description=d) for n, d in menu_commands(admin)]


async def set_commands(bot: Bot):
    try:
        user, admin = build(False), build(True)
        await bot.set_my_commands(user, scope=BotCommandScopeAllGroupChats())
        await bot.set_my_commands([c for c in user if c.command in ("help", "profile")],
                                  scope=BotCommandScopeAllPrivateChats())
        # В общей группе /admin в меню не показываем: панель открывается только в личке с ботом
        await bot.set_my_commands([c for c in admin if c.command != "admin"],
                                  scope=BotCommandScopeChatMember(chat_id=GROUP_ID, user_id=ADMIN_ID))
        await bot.set_my_commands(admin, scope=BotCommandScopeChat(chat_id=ADMIN_ID))
        return True
    except Exception:
        return False
