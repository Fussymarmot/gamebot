from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from config import ADMIN_ID
from database.core import q
from database.moderation import log_mod
from database.settings import get_setting
from database.users import get_user_name
from services import economy as ec
from services.quiet import silent_state
from utils.emoji import e
from utils.fmt import code, esc
from utils.targets import from_mention, from_text, from_user, parse_target
from utils.ui import kb

router = Router()
MEDALS = ["gold", "silver", "bronze"]


def setup(bot):
    from config import GROUP_ID

    async def announce(text: str):
        silent, _ = await silent_state()
        if not silent:
            await bot.send_message(GROUP_ID, text)

    async def notify_admin(text: str):
        await bot.send_message(ADMIN_ID, text)

    ec.announce, ec.notify_admin = announce, notify_admin
    ec.register_hooks()


def with_title(name: str, title) -> str:
    return f"{esc(name)} <i>{esc(title)}</i>" if title else esc(name)


@router.message(Command("balance"))
async def cmd_balance(message: Message):
    uid = message.from_user.id
    coins = (await ec.wallet(uid))[0]
    place, total = await ec.coin_rank(uid)
    await message.answer(f"♦️ <b>Твой баланс:</b> {code(str(coins))}\n"
                         f"Место в рейтинге рубинов: <b>{place}</b> из {total}")


async def topcoins_size() -> int:
    try:
        return max(5, min(20, int(await get_setting("topcoins_size", "10"))))
    except ValueError:
        return 10


PAY_USAGE = (f"❌ Формат: ответь на сообщение командой {code('/pay 50')}\n"
             f"или укажи получателя: {code('/pay @username 50')}, {code('/pay 123456789 50')}")


async def pay_target(message: Message):
    """Получатель и сумма: по реплаю (/pay 50), @username или ID (/pay @user 50)."""
    parts = (message.text or "").split(maxsplit=1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return None
    t = await from_mention(message, rest)
    if not t:
        words = rest.split(maxsplit=1)
        # одно число без @ — это сумма (иначе «/pay 10000» приняли бы за ID)
        if len(words) == 2 or words[0].startswith("@"):
            t = await from_text(rest)
    if not t:
        rp = message.reply_to_message
        if rp and rp.from_user:
            if rp.from_user.is_bot:
                return "bot"
            t = await from_user(rp.from_user, rest)
    return t


@router.message(Command("pay"))
async def cmd_pay(message: Message):
    sender = message.from_user
    if not sender or sender.is_bot:
        return
    t = await pay_target(message)
    if t == "bot":
        await message.answer("❌ Боту рубины переводить нельзя.")
        return
    if t is None:
        await message.answer(PAY_USAGE)
        return
    words = t.rest.split()
    if not words:
        await message.answer(PAY_USAGE)
        return
    try:
        amount = int(words[0])
    except ValueError:
        await message.answer(PAY_USAGE)
        return
    if amount <= 0:
        await message.answer("❌ Сумма должна быть больше нуля.")
        return
    if not t.id:
        await message.answer("❌ Не знаю такого пользователя. Укажи ID или ответь на его сообщение.")
        return
    if t.id == sender.id:
        await message.answer("❌ Нельзя перевести рубины самому себе.")
        return
    if not await q("SELECT 1 FROM users WHERE user_id=?", (t.id,), one=True):
        await message.answer("❌ Не знаю такого пользователя. Проверь ID.")
        return
    await from_user(sender)
    if not await ec.transfer(sender.id, t.id, amount):
        balance = (await ec.wallet(sender.id))[0]
        await message.answer(f"❌ Не хватает рубинов: у тебя {balance}, а нужно {amount}.")
        return
    left = (await ec.wallet(sender.id))[0]
    to_name = await get_user_name(t.id)
    await message.answer(
        f"💸 <a href=\"tg://user?id={sender.id}\">{esc(sender.first_name or 'Игрок')}</a> перевёл(а) "
        f"<b>{amount}</b> ♦️ → <a href=\"tg://user?id={t.id}\">{esc(to_name)}</a>\n"
        f"<i>Остаток у отправителя: {left}</i>")


@router.message(Command("topcoins"))
async def cmd_topcoins(message: Message):
    n = await topcoins_size()
    rows = await ec.top_coins(n)
    text = f"♦️ <b>Топ-{n} по рубинам</b>\n\n"
    if not rows:
        await message.answer(text + "<i>Пока ни у кого нет рубинов. Сыграйте в игры!</i>")
        return
    for i, (uid, name, coins, title) in enumerate(rows):
        mark = e(MEDALS[i]) if i < 3 else f"{i + 1}."
        text += f"{mark} <b>{with_title(name, title)}</b>: {coins}\n"
    me = message.from_user.id
    if me not in [r[0] for r in rows]:
        place, _ = await ec.coin_rank(me)
        text += f"\n<i>Ты на {place}-м месте: {(await ec.wallet(me))[0]} рубинов</i>"
    await message.answer(text)


async def profile_text(uid: int, name: str, own: bool) -> str:
    wins, losses, draws = await ec.game_totals(uid)
    coins, _, title, _ = await ec.wallet(uid)
    place = await ec.wins_rank(uid)
    got = await ec.earned_keys(uid)
    text = f"👤 <b>{esc(name)}</b>" + (f" <i>{esc(title)}</i>" if title else "") + f"\nID: {code(str(uid))}\n\n"
    text += f"{e('game')} Игр сыграно: <b>{wins + losses + draws}</b>\n"
    text += f"🏆 Побед: <b>{wins}</b>, поражений: <b>{losses}</b>, ничьих: <b>{draws}</b>\n"
    text += f"{e('gold')} Место по победам: <b>{place or '—'}</b>\n♦️ Рубины: <b>{coins}</b>\n"
    if got:
        text += "\n<b>Достижения:</b> " + ", ".join(esc(ec.ACHIEVEMENTS[k][0]) for k in got)
    else:
        text += "\n<i>Достижений пока нет</i>"
    if own:
        warns = (await q("SELECT COUNT(*) FROM warns WHERE user_id=? AND active=1", (uid,), one=True))[0]
        if warns:
            text += f"\n\n⚠️ Активных предупреждений: <b>{warns}</b>"
    return text


@router.message(Command("profile"))
async def cmd_profile(message: Message):
    target = await parse_target(message)
    own = target is None or target.id == message.from_user.id
    uid = message.from_user.id if own else target.id
    if uid is None:
        await message.answer("❌ Не знаю такого пользователя. Укажи ID или ответь на его сообщение.")
        return
    name = (message.from_user.first_name or "Игрок") if own else await get_user_name(uid)
    markup = kb([[("🏷 Выбрать звание", f"ttl:{uid}:list"), ("🛍 Магазин", f"shop:{uid}:main")]]) if own else None
    await message.answer(await profile_text(uid, name, own), reply_markup=markup)


@router.message(Command("achievements"))
async def cmd_achievements(message: Message):
    have = set(await ec.earned_keys(message.from_user.id))
    text = "🏆 <b>Достижения</b>\n\n"
    for key, (title, desc, base) in ec.ACHIEVEMENTS.items():
        text += f"{'✅' if key in have else '🔒'} <b>{esc(title)}</b>: {esc(desc)} <i>(+{ec.reward(base)} ♦️)</i>\n"
    await message.answer(text)


@router.message(Command("daily"))
async def cmd_daily(message: Message):
    tasks = await ec.update_tasks(message.from_user.id)
    text = "📅 <b>Задания на сегодня</b>\n\n"
    for (title, progress, goal, done), key in zip(tasks, ec.TASKS):
        text += f"{'✅' if done else '▫️'} {esc(title)}: {progress}/{goal} <i>(+{ec.reward(ec.TASKS[key][2])} ♦️)</i>\n"
    await message.answer(text + f"\n<i>За игры в день можно получить до {ec.daily_cap()} рубинов. "
                                "Считаются только завершённые игры.</i>")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    users = (await q("SELECT COUNT(*) FROM private_users", one=True))[0]
    parts = (await q("SELECT COUNT(*) FROM game_log", one=True))[0]
    pop = await q("SELECT game_key, COUNT(*) c FROM game_log GROUP BY game_key ORDER BY c DESC LIMIT 3")
    coins = (await q("SELECT COALESCE(SUM(total_earned),0) FROM wallets", one=True))[0]
    ach = (await q("SELECT COUNT(*) FROM achievements", one=True))[0]
    top = await q("""SELECT COALESCE(u.first_name,'Игрок'), SUM(g.wins) w FROM game_stats g
                     LEFT JOIN users u ON u.user_id=g.user_id GROUP BY g.user_id HAVING w>0
                     ORDER BY w DESC LIMIT 3""")
    names = {"ttt": "крестики-нолики", "rps": "КНБ", "duel": "дуэль", "mafia": "мафия",
             "memory": "мемори", "slot": "слот-машина", "mines": "сапёр", "tower": "башня", "code": "взломай код", "assoc": "ассоциации", "bunker": "бункер", "blackjack": "блэкджек"}
    text = f"{e('chart')} <b>Статистика бота</b>\n\nПользователей в личке: <b>{users}</b>\nУчастий в играх: <b>{parts}</b>\n"
    if pop:
        text += "Популярные игры: " + ", ".join(f"{names.get(k, k)} ({c})" for k, c in pop) + "\n"
    if top:
        text += "Больше всего побед: " + ", ".join(f"{esc(n)} ({w})" for n, w in top) + "\n"
    await message.answer(text + f"Выдано рубинов: <b>{coins}</b>, достижений: <b>{ach}</b>")


@router.message(Command("coins"))
async def cmd_coins(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    usage = f"❌ Формат: {code('/coins @username +50 причина')} или {code('/coins 123456789 +50 причина')}"
    if not t or not t.id:
        await message.answer(usage)
        return
    parts = t.rest.split(maxsplit=1)
    try:
        amount = int(parts[0])
    except (ValueError, IndexError):
        await message.answer(usage)
        return
    reason = parts[1].strip() if len(parts) > 1 else ""
    if not reason:
        await message.answer("❌ Укажи причину изменения баланса.")
        return
    if abs(amount) > 100000:
        await message.answer("❌ Слишком большая сумма.")
        return
    await ec.add_coins(t.id, amount)
    await log_mod(ADMIN_ID, t.id, "coins", f"{amount:+d}: {reason}")
    await message.answer(f"{e('check')} Баланс {esc(t.name)} изменён на <b>{amount:+d}</b>. "
                         f"Теперь: {(await ec.wallet(t.id))[0]}")


@router.message(Command("uncoins"))
async def cmd_uncoins(message: Message):
    if not message.from_user or message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    t = await parse_target(message)
    usage = (f"❌ Формат: {code('/uncoins @username 50 причина')} или {code('/uncoins 123456789 50 причина')}\n"
             f"Вместо числа можно написать {code('all')}, чтобы забрать всё.")
    if not t or not t.id:
        await message.answer(usage)
        return
    parts = t.rest.split(maxsplit=1)
    if not parts:
        await message.answer(usage)
        return
    word = parts[0].lower().lstrip("-")
    balance = (await ec.wallet(t.id))[0]
    if word in ("all", "все", "всё"):
        amount = balance
    else:
        try:
            amount = int(word)
        except ValueError:
            await message.answer(usage)
            return
    if amount <= 0:
        await message.answer("❌ Укажи положительное число рубинов.")
        return
    if amount > 1000000:
        await message.answer("❌ Слишком большая сумма.")
        return
    reason = parts[1].strip() if len(parts) > 1 else ""
    taken = min(amount, balance)
    if taken <= 0:
        await message.answer(f"У {esc(t.name)} нет рубинов.")
        return
    await ec.add_coins(t.id, -taken)
    await log_mod(ADMIN_ID, t.id, "uncoins", f"-{taken}" + (f": {reason}" if reason else ""))
    text = f"{e('check')} У {esc(t.name)} забрано <b>{taken}</b> ♦️. Теперь: {balance - taken}"
    if taken < amount:
        text += f"\n<i>Запрошено {amount}, но на балансе было только {balance}.</i>"
    await message.answer(text)
