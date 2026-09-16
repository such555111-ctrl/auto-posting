"""
handlers/admin.py

Личный кабинет владельца канала: команда /start открывает inline-панель
управления автопостингом (статус, бренд, интервал, статистика).
Доступ разрешён всем, кто есть в таблице admins (см. database.py) —
изначально это ADMIN_ID из .env, дальше список можно пополнять командой
/addadmin прямо из чата с ботом.
"""

from __future__ import annotations

import logging
import os

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import database
import scheduler

logger = logging.getLogger(__name__)

PRIMARY_ADMIN_ID: int = int(os.environ["ADMIN_ID"])

INTERVAL_PRESETS_MINUTES: list[int] = [15, 30, 60, 120, 240]


class IsAdmin(BaseFilter):
    """Пропускает только пользователей, которые есть в таблице admins."""

    async def __call__(self, event: Message | CallbackQuery) -> bool:
        user = event.from_user
        return user is not None and database.is_admin(user.id)


admin_router = Router(name="admin")
admin_router.message.filter(IsAdmin())
admin_router.callback_query.filter(IsAdmin())


# --------------------------------------------------------------------------- #
# Callback data
# --------------------------------------------------------------------------- #

class MenuCallback(CallbackData, prefix="menu"):
    action: str
    value: str = ""


class IntervalInput(StatesGroup):
    """Ожидание, пока админ введёт свой интервал в минутах текстом."""
    waiting_minutes = State()


# --------------------------------------------------------------------------- #
# Форматирование
# --------------------------------------------------------------------------- #

def _plural(n: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу (1 час / 2 часа / 5 часов)."""
    n_abs = abs(n)
    if n_abs % 10 == 1 and n_abs % 100 != 11:
        return one
    if 2 <= n_abs % 10 <= 4 and not (12 <= n_abs % 100 <= 14):
        return few
    return many


def format_interval(minutes: int) -> str:
    """15 -> '15 мин', 60 -> '1 час', 120 -> '2 часа', 240 -> '4 часа'."""
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"{hours} {_plural(hours, 'час', 'часа', 'часов')}"
    return f"{minutes} мин"


def format_brand(selected_brand: str) -> str:
    return "Все бренды" if selected_brand == "ALL" else selected_brand


def format_status(is_active: bool) -> str:
    return "🟢 Работает" if is_active else "🔴 На паузе"


# --------------------------------------------------------------------------- #
# Построение экранов
# --------------------------------------------------------------------------- #

def build_main_menu() -> tuple[str, InlineKeyboardMarkup]:
    settings = database.get_settings()

    text = (
        "<b>🤖 Панель автопостера</b>\n\n"
        f"Статус: <b>{format_status(settings.is_active)}</b>\n"
        f"Бренд: <b>{format_brand(settings.selected_brand)}</b>\n"
        f"Интервал: <b>{format_interval(settings.interval_minutes)}</b>"
    )

    if settings.target_channel_id is None:
        text += "\n\n⚠️ Целевой канал не настроен (TARGET_CHANNEL_ID)."

    builder = InlineKeyboardBuilder()

    if settings.is_active:
        builder.button(
            text="⏸ Поставить на паузу",
            callback_data=MenuCallback(action="toggle"),
        )
    else:
        builder.button(
            text="▶️ Запустить",
            callback_data=MenuCallback(action="toggle"),
        )

    builder.button(text="🏷 Выбрать бренд", callback_data=MenuCallback(action="brand_menu"))
    builder.button(text="⏱ Изменить интервал", callback_data=MenuCallback(action="interval_menu"))
    builder.button(text="📊 Статистика", callback_data=MenuCallback(action="stats"))

    builder.adjust(1)
    return text, builder.as_markup()


def build_brand_menu() -> tuple[str, InlineKeyboardMarkup]:
    settings = database.get_settings()
    brands = database.get_all_brand_tags()

    text = f"<b>🏷 Выбор бренда</b>\n\nТекущий: <b>{format_brand(settings.selected_brand)}</b>"

    builder = InlineKeyboardBuilder()

    all_mark = "✅ " if settings.selected_brand == "ALL" else ""
    builder.button(
        text=f"{all_mark}Все бренды",
        callback_data=MenuCallback(action="brand_set", value="ALL"),
    )

    for brand in brands:
        mark = "✅ " if brand == settings.selected_brand else ""
        builder.button(
            text=f"{mark}{brand}",
            callback_data=MenuCallback(action="brand_set", value=brand),
        )

    builder.button(text="⬅️ Назад", callback_data=MenuCallback(action="main"))
    builder.adjust(1)
    return text, builder.as_markup()


def build_interval_menu() -> tuple[str, InlineKeyboardMarkup]:
    settings = database.get_settings()
    text = (
        f"<b>⏱ Интервал публикаций</b>\n\n"
        f"Текущий: <b>{format_interval(settings.interval_minutes)}</b>"
    )

    builder = InlineKeyboardBuilder()
    for minutes in INTERVAL_PRESETS_MINUTES:
        mark = "✅ " if minutes == settings.interval_minutes else ""
        builder.button(
            text=f"{mark}{format_interval(minutes)}",
            callback_data=MenuCallback(action="interval_set", value=str(minutes)),
        )
    builder.adjust(2)

    builder.row(
        InlineKeyboardButton(
            text="✏️ Свой интервал (мин)",
            callback_data=MenuCallback(action="interval_custom").pack(),
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data=MenuCallback(action="main").pack(),
        )
    )
    return text, builder.as_markup()


def build_stats_screen() -> tuple[str, InlineKeyboardMarkup]:
    total = database.count_posts()
    by_brand = database.count_posts_by_brand()

    lines = ["<b>📊 Статистика базы постов</b>", "", f"Всего постов: <b>{total}</b>"]

    if by_brand:
        lines.append("")
        lines.append("По брендам:")
        for brand, count in by_brand.items():
            lines.append(f"  {brand} — {count}")
    else:
        lines.append("")
        lines.append("Постов с брендами пока нет.")

    without_brand = total - sum(by_brand.values())
    if without_brand > 0:
        lines.append("")
        lines.append(f"Без бренда: {without_brand}")

    text = "\n".join(lines)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад", callback_data=MenuCallback(action="main"))
    return text, builder.as_markup()


# --------------------------------------------------------------------------- #
# Отрисовка (с защитой от "message is not modified")
# --------------------------------------------------------------------------- #

async def _render(message: Message, text: str, markup: InlineKeyboardMarkup) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as error:
        if "message is not modified" not in str(error):
            raise


# --------------------------------------------------------------------------- #
# Хендлеры команд
# --------------------------------------------------------------------------- #

@admin_router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    text, markup = build_main_menu()
    await message.answer(text, reply_markup=markup)


# --------------------------------------------------------------------------- #
# Управление списком админов
# --------------------------------------------------------------------------- #

def _parse_user_id(raw: str | None) -> int | None:
    if raw is None:
        return None
    raw = raw.strip()
    if not raw.lstrip("-").isdigit():
        return None
    return int(raw)


@admin_router.message(Command("admins"))
async def cmd_list_admins(message: Message) -> None:
    ids = database.list_admins()
    lines = ["<b>👤 Админы бота</b>", ""]
    for user_id in ids:
        owner_mark = " (владелец, из .env)" if user_id == PRIMARY_ADMIN_ID else ""
        lines.append(f"• <code>{user_id}</code>{owner_mark}")
    lines.append("")
    lines.append("Добавить: /addadmin ID")
    lines.append("Убрать: /removeadmin ID")
    await message.reply("\n".join(lines))


@admin_router.message(Command("addadmin"))
async def cmd_add_admin(message: Message, command: CommandObject) -> None:
    new_id = _parse_user_id(command.args)
    if new_id is None:
        await message.reply(
            "Использование: <code>/addadmin ID</code>\n"
            "Узнать ID человека можно через бота @userinfobot — "
            "пусть перешлёт вам его ответ, либо он сам вам его пришлёт."
        )
        return

    added = database.add_admin(new_id)
    if added:
        await message.reply(f"✅ Пользователь <code>{new_id}</code> теперь админ бота.")
    else:
        await message.reply(f"Пользователь <code>{new_id}</code> уже был в списке админов.")


@admin_router.message(Command("removeadmin"))
async def cmd_remove_admin(message: Message, command: CommandObject) -> None:
    target_id = _parse_user_id(command.args)
    if target_id is None:
        await message.reply("Использование: <code>/removeadmin ID</code>")
        return

    if len(database.list_admins()) <= 1:
        await message.reply("⚠️ Нельзя удалить последнего оставшегося админа.")
        return

    removed = database.remove_admin(target_id)
    if removed:
        await message.reply(f"✅ Пользователь <code>{target_id}</code> больше не админ.")
    else:
        await message.reply(f"Пользователя <code>{target_id}</code> и так не было в списке.")


# --------------------------------------------------------------------------- #
# Хендлеры callback_query
# --------------------------------------------------------------------------- #

@admin_router.callback_query(MenuCallback.filter(F.action == "main"))
async def cb_main_menu(query: CallbackQuery) -> None:
    text, markup = build_main_menu()
    await _render(query.message, text, markup)
    await query.answer()


@admin_router.callback_query(MenuCallback.filter(F.action == "toggle"))
async def cb_toggle(query: CallbackQuery) -> None:
    settings = database.get_settings()
    new_active = not settings.is_active

    if new_active and settings.target_channel_id is None:
        await query.answer(
            "Сначала настройте TARGET_CHANNEL_ID в .env и перезапустите бота.",
            show_alert=True,
        )
        return

    database.update_settings(is_active=new_active)
    scheduler.apply_settings(query.bot)

    text, markup = build_main_menu()
    await _render(query.message, text, markup)
    await query.answer("Запущено" if new_active else "Поставлено на паузу")


@admin_router.callback_query(MenuCallback.filter(F.action == "brand_menu"))
async def cb_brand_menu(query: CallbackQuery) -> None:
    text, markup = build_brand_menu()
    await _render(query.message, text, markup)
    await query.answer()


@admin_router.callback_query(MenuCallback.filter(F.action == "brand_set"))
async def cb_brand_set(query: CallbackQuery, callback_data: MenuCallback) -> None:
    database.update_settings(selected_brand=callback_data.value)
    scheduler.apply_settings(query.bot)

    text, markup = build_main_menu()
    await _render(query.message, text, markup)
    await query.answer(f"Бренд: {format_brand(callback_data.value)}")


@admin_router.callback_query(MenuCallback.filter(F.action == "interval_menu"))
async def cb_interval_menu(query: CallbackQuery) -> None:
    text, markup = build_interval_menu()
    await _render(query.message, text, markup)
    await query.answer()


@admin_router.callback_query(MenuCallback.filter(F.action == "interval_set"))
async def cb_interval_set(query: CallbackQuery, callback_data: MenuCallback) -> None:
    minutes = int(callback_data.value)
    database.update_settings(interval_minutes=minutes)
    scheduler.apply_settings(query.bot)

    text, markup = build_main_menu()
    await _render(query.message, text, markup)
    await query.answer(f"Интервал: {format_interval(minutes)}")


@admin_router.callback_query(MenuCallback.filter(F.action == "interval_custom"))
async def cb_interval_custom(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(IntervalInput.waiting_minutes)
    # Запоминаем id сообщения с панелью, чтобы потом отредактировать именно его.
    await state.update_data(panel_message_id=query.message.message_id)

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Отмена", callback_data=MenuCallback(action="interval_menu"))

    await _render(
        query.message,
        "<b>✏️ Свой интервал</b>\n\n"
        "Пришлите одним сообщением число минут (целое, от 1).\n"
        "Например: <code>45</code>",
        builder.as_markup(),
    )
    await query.answer()


@admin_router.message(IntervalInput.waiting_minutes)
async def on_interval_custom_input(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()

    if not raw.isdigit() or int(raw) < 1:
        await message.reply(
            "⚠️ Нужно целое число минут, не меньше 1. Например: 45.\n"
            "Попробуйте ещё раз, или нажмите «⬅️ Отмена» в панели выше."
        )
        return

    minutes = int(raw)
    database.update_settings(interval_minutes=minutes)
    scheduler.apply_settings(message.bot)

    data = await state.get_data()
    await state.clear()

    text, markup = build_main_menu()
    panel_message_id = data.get("panel_message_id")

    if panel_message_id is not None:
        try:
            await message.bot.edit_message_text(
                text,
                chat_id=message.chat.id,
                message_id=panel_message_id,
                reply_markup=markup,
            )
        except TelegramBadRequest:
            await message.answer(text, reply_markup=markup)
    else:
        await message.answer(text, reply_markup=markup)

    await message.reply(f"✅ Интервал установлен: {format_interval(minutes)}")


@admin_router.callback_query(MenuCallback.filter(F.action == "stats"))
async def cb_stats(query: CallbackQuery) -> None:
    text, markup = build_stats_screen()
    await _render(query.message, text, markup)
    await query.answer()
