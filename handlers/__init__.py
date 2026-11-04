"""Обработчики команд и кнопок (не игровые).

user/        /start, /help
economy/     рубины и профиль (economy.py), магазин (shop.py), розыгрыши (giveaway.py)
moderation/  муты и предупреждения, жалобы, обжалования, роли модераторов, теги, очистка чата
system/      пауза игр, проверка Rich, ID эмодзи
admin/       админ-панель в личке (по файлу на раздел)

Сами игры живут в пакете games/.
"""


def get_routers():
    """Все роутеры бота. ПОРЯДОК ВАЖЕН: aiogram берёт первый подходящий хендлер."""
    from games import core as games_core
    from games import ratings
    from games import menu as games_menu
    from games.casual import codebreaker, memory, wordseek
    from games.duel import challenge, duel, hangman, rps, seabattle, tictactoe
    from games.party import associations, roulette
    from games.party.poker import handlers as poker
    from games.party.bunker import handlers as bunker
    from games.party.mafia import handlers as mafia
    from handlers import admin
    from handlers.economy import economy, giveaway, shop
    from handlers.moderation import admin_tools, appeals, mod_roles, moderation, tag_enforcer
    from handlers.system import emoji_helper, pause_cmds, rich_test
    from handlers.user import common

    return [
        admin.router, admin.panel,
        common.router, ratings.router, games_menu.input_router,
        giveaway.router, economy.router, shop.router, admin_tools.router, moderation.router,
        challenge.router, games_core.router,
        tictactoe.router, rps.router, duel.router, mafia.router, memory.router,
        codebreaker.router, associations.router, bunker.router, roulette.router, poker.router,
        seabattle.router, hangman.router, wordseek.router,
        appeals.router, mod_roles.router, rich_test.router, games_menu.router, pause_cmds.router,
        emoji_helper.router, tag_enforcer.router,
    ]
