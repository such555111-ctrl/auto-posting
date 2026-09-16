"""
publisher.py

Логика публикации одного поста в целевой канал согласно текущим
настройкам (settings) и обновления last_posted_at после успешной отправки.
"""

from __future__ import annotations

import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InputMediaPhoto

import database

logger = logging.getLogger(__name__)


async def publish_next_post(bot: Bot) -> bool:
    """
    Публикует очередной пост согласно текущим настройкам автопостинга.

    Возвращает True, если пост был успешно опубликован, иначе False
    (постинг выключен, не задан канал или нет подходящих постов).
    """
    settings = database.get_settings()

    if not settings.is_active:
        logger.debug("Автопостинг выключен — публикация пропущена.")
        return False

    if settings.target_channel_id is None:
        logger.warning("Не задан target_channel_id — публикация невозможна.")
        return False

    post = database.get_next_post(settings.selected_brand)
    if post is None:
        logger.warning(
            "Нет постов для публикации (бренд: %s).", settings.selected_brand
        )
        return False

    try:
        await _send_post(bot, settings.target_channel_id, post)
    except TelegramAPIError:
        logger.exception(
            "Ошибка при публикации поста id=%s в канал %s.",
            post.id,
            settings.target_channel_id,
        )
        return False

    database.mark_as_posted(post.id)
    logger.info(
        "Пост id=%s (бренд: %s) опубликован в канал %s.",
        post.id,
        post.brand_tag,
        settings.target_channel_id,
    )
    return True


async def _send_post(bot: Bot, chat_id: int, post: database.Post) -> None:
    """Отправляет пост в чат в зависимости от типа медиа."""
    caption = post.text or None

    if post.media_type == "photo":
        file_id = post.file_ids[0] if post.file_ids else None
        if file_id is None:
            raise ValueError(f"Пост id={post.id} помечен как photo, но file_ids пуст.")
        await bot.send_photo(chat_id=chat_id, photo=file_id, caption=caption)

    elif post.media_type == "album":
        if not post.file_ids:
            raise ValueError(f"Пост id={post.id} помечен как album, но file_ids пуст.")

        media = [
            InputMediaPhoto(
                media=file_id,
                caption=caption if index == 0 else None,
            )
            for index, file_id in enumerate(post.file_ids)
        ]
        await bot.send_media_group(chat_id=chat_id, media=media)

    else:  # 'none'
        if not caption:
            raise ValueError(f"Пост id={post.id} не содержит ни текста, ни медиа.")
        await bot.send_message(chat_id=chat_id, text=caption)
