import re
from datetime import datetime, timedelta

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from config import ADMIN_ID
from utils.emoji import e
from utils.timeutils import now
from utils.ui import kb

router = Router()
panel = Router()
panel.message.filter(F.from_user.id == ADMIN_ID, F.chat.type == "private")
panel.callback_query.filter(F.from_user.id == ADMIN_ID)


class S(StatesGroup):
    bc_msg = State()
    upd_text = State()
    ban_user = State()
    maint_end = State()
    maint_comment = State()
    restore_file = State()
    gw_title = State()
    gw_prize = State()
    gw_value = State()
    gw_end = State()
    title_add = State()
    prize_title = State()
    prize_price = State()
    prize_stock = State()
    clean_user = State()
    mod_user = State()
    tt_name = State()
    tt_price = State()
    tt_edit_name = State()
    tt_edit_price = State()


MENU = [
    [("🎁 Розыгрыши", "a:gw")],
    [("✉️ Рассылка", "a:bc"), ("🔄 Обновление", "a:upd")],
    [("🎮 Игры и топы", "a:gm"), ("📊 Статистика", "a:st")],
    [("🛍 Магазин и звания", "a:shop"), ("🧹 Очистка", "a:cl")],
    [("🔒 Модерация", "a:mod"), ("⛔ Бан в играх", "a:ban")],
    [("🛡 Модераторы", "a:mods"), ("🧩 Функции", "a:ft")],
    [("🌙 Тихое время", "a:qt")],
    [("🛠 Тех. работы", "a:mt"), ("⚙️ Бот", "a:bot")],
]
BACK = [("◀️ В меню", "a:menu")]
CANCEL = kb([[("❌ Отмена", "a:cancel")]])


async def show(target, text: str, markup=None):
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as ex:
            if "not modified" not in str(ex):
                await target.message.answer(text, reply_markup=markup)
        await target.answer()
    else:
        await target.answer(text, reply_markup=markup)


async def menu(target):
    await show(target, f"{e('gear')} <b>Админ-панель</b>\n\n<i>Выбери раздел:</i>", kb(MENU))


@router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if message.chat.type != "private":
        return
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ Нет доступа")
        return
    await state.clear()
    await menu(message)


@panel.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    await state.clear()
    await menu(message)


@panel.callback_query(F.data == "a:cancel")
async def cancel_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await menu(cb)


@panel.callback_query(F.data == "a:menu")
async def menu_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await menu(cb)


def norm_time(s: str):
    m = re.fullmatch(r"\s*([01]?\d|2[0-3])[:.]([0-5]\d)\s*", s or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else None


def parse_when(s: str):
    s = (s or "").strip()
    n = now()
    for fmt in ("%d.%m.%Y %H:%M", "%d.%m %H:%M"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(year=n.year) if fmt == "%d.%m %H:%M" else d
        except ValueError:
            pass
    t = norm_time(s)
    if t:
        h, mi = map(int, t.split(":"))
        d = n.replace(hour=h, minute=mi, second=0, microsecond=0)
        return d if d > n else d + timedelta(days=1)
    return None
