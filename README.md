# Игровой бот

Игровой Telegram-бот для группового чата (aiogram 3): мафия, бункер, морской бой и другие игры,
рубины, магазин, достижения, розыгрыши и модерация чата.

## Запуск

```
python -m venv venv
venv\Scripts\activate        # Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env       # Linux/macOS: cp .env.example .env  — и заполнить значения
python main.py
```

Боту нужны права администратора в группе. Обязательные переменные в `.env`: `BOT_TOKEN`, `ADMIN_ID`, `GROUP_ID`.
`ANTHROPIC_API_KEY` нужен только для необязательных ИИ-подсказок в играх (включаются в админ-панели).

## Структура

```
main.py            точка входа: запуск, старт/остановка бота
config.py          настройки из .env
data/              слова и вопросы для игр, id эмодзи (json)
backups/           резервные копии базы

database/          слой данных (SQLite)
  core.py            соединение, q / x / batch
  schema.py          таблицы и миграции
  lifecycle.py       закрытие, обслуживание, восстановление из бэкапа
  settings.py        настройки бота (key-value)
  users.py           пользователи, участники группы, личка
  messages.py        журнал сообщений (для розыгрышей и /clear)
  game_stats.py      результаты игр, рейтинг
  moderation.py      чёрный список игр, журнал модерации

games/             всё, что относится к играм
  core.py            лобби, реестр активных игр, запись результатов
  menu.py            /games: кнопки запуска всех игр и выбор ставки
  ratings.py         /top_games
  invites.py         занятость игроков в играх на двоих
  pause.py gamemute.py content.py ai_content.py
  duel/              игры на двоих: tictactoe, rps, duel, seabattle, hangman;
                     challenge.py — общий набор со ставкой, игра стартует сразу после присоединения соперника
  party/             групповые: mafia/, bunker/ (handlers.py + engine.py), roulette, associations
  casual/            лёгкие: memory, codebreaker, wordseek

handlers/          команды и кнопки не из игр
  user/              /start, /help
  economy/           economy.py (рубины, профиль), shop.py (магазин), giveaway.py (розыгрыши)
  moderation/        moderation.py, admin_tools.py (/clear, жалобы), mod_roles.py, appeals.py, tag_enforcer.py
  system/            pause_cmds.py, rich_test.py, emoji_helper.py
  admin/             админ-панель (по файлу на раздел)

services/          бизнес-логика без привязки к командам
  economy.py shop.py giveaways.py      рубины, достижения, заказы, розыгрыши
  mods.py modtools.py                  модераторы, муты, предупреждения
  members.py tags.py features.py maintenance.py quiet.py
  commands.py                          реестр команд и меню Telegram
  scheduler.py backup.py report_week.py ai.py

middlewares/       доступ, техработы, антиспам, автомодерация, учёт сообщений, тихое время, отключённые функции
utils/             fmt, emoji, ui (кнопки), premium, richui, targets, titles, timeutils
```

## Как добавить игру

0. Игра на двоих: описать запуск партии функцией `start_game(msg, p1, n1, p2, n2)`, один раз зарегистрировать её через
   `register_duel(key, title, emoji, start_game)` из `games/duel/challenge.py` и в команде вызвать `open_challenge(message, key)`.
   Остальное (набор, ставка, кнопка в /games, автостарт после присоединения) сделает challenge/menu.
   Кнопку и подписи добавить в `games/menu.py` (ICONS, LABELS, DUEL_ORDER).
1. Создать файл в `games/duel/`, `games/party/` или `games/casual/` (или пакет с `handlers.py` и `engine.py`),
   зарегистрировать через `register(GameDef(...))` из `games/core.py` и объявить `router`.
2. Добавить роутер в `get_routers()` в `handlers/__init__.py`.
3. Добавить команду в `services/commands.py` и запись в `RATING_GAMES` в `games/ratings.py`.
4. Игра с лобби и ставкой: по образцу `open_roulette` в `games/party/roulette.py`; добавить её в `has_stake()` в `games/menu.py`.
