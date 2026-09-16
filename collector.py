"""
handlers/collector.py

Сбор постов в базу: админ пересылает боту сообщения из своего канала
(одиночные фото, альбомы или текст), бот сохраняет их в таблицу posts
и парсит хэштег бренда из текста. Реализация без User API — самый
простой и надёжный вариант для одного администратора.

Как работает сбор альбомов: Telegram присылает каждое фото альбома
отдельным сообщением с общим media_group_id. Бот буферизует сообщения
одной группы и, если в течение ALBUM_FLUSH_DELAY секунд не пришло новых
частей, сохраняет альбом целиком одним постом.
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from aiogram import Router
from aiogram.types import Message

import database
import utils
from admin import IsAdmin

logger = logging.getLogger(__name__)

ALBUM_FLUSH_DELAY: float = 1.5  # секунд ожидания остальных частей альбома

collector_router = Router(name="collector")
collector_router.message.filter(IsAdmin())

# media_group_id -> список сообщений одного альбома, ещё не сохранённых
_album_buffers: defaultdict[str, list[Message]] = defaultdict(list)
# media_group_id -> отложенная задача сохранения (чтобы не запускать дважды)
_album_flush_tasks: dict[str, asyncio.Task] = {}


# --------------------------------------------------------------------------- #
# Извлечение данных пересланного сообщения
# --------------------------------------------------------------------------- #

def _get_forwarded_message_id(message: Message) -> int | None:
    """
    Возвращает id сообщения в исходном канале.
    Поддерживает и новый forward_origin (Bot API 7.0+), и устаревший
    forward_from_chat/forward_from_message_id — на случай старых клиентов.
    """
    origin = message.forward_origin
    if origin is not None and getattr(origin, "type", None) == "channel":
        return origin.message_id  # type: ignore[attr-defined]

    if message.forward_from_chat is not None and message.forward_from_message_id is not None:
        return message.forward_from_message_id

    return None


def _is_forwarded_from_channel(message: Message) -> bool:
    return _get_forwarded_message_id(message) is not None


# --------------------------------------------------------------------------- #
# Сохранение одиночного поста (фото или просто текст)
# --------------------------------------------------------------------------- #

async def _save_single(message: Message) -> None:
    channel_message_id = _get_forwarded_message_id(message)
    if channel_message_id is None:
        return

    text = message.text or message.caption or ""
    brand_tag = utils.extract_brand_tag(text)

    if message.photo:
        media_type = "photo"
        file_ids = [message.photo[-1].file_id]
    else:
        media_type = "none"
        file_ids = []

    post_id = database.save_post(
        channel_message_id=channel_message_id,
        text=text,
        media_type=media_type,
        file_ids=file_ids,
        brand_tag=brand_tag,
    )

    await message.reply(_format_saved_reply(post_id, brand_tag, media_type, parts=1))


# --------------------------------------------------------------------------- #
# Сохранение альбома (несколько сообщений с одним media_group_id)
# --------------------------------------------------------------------------- #

async def _flush_album(media_group_id: str) -> None:
    await asyncio.sleep(ALBUM_FLUSH_DELAY)

    messages = _album_buffers.pop(media_group_id, [])
    _album_flush_tasks.pop(media_group_id, None)

    if not messages:
        return

    messages.sort(key=lambda m: m.message_id)

    first_with_id = next(
        (m for m in messages if _get_forwarded_message_id(m) is not None), None
    )
    if first_with_id is None:
        logger.warning("Альбом %s без определяемого id канала — пропущен.", media_group_id)
        return

    channel_message_id = _get_forwarded_message_id(first_with_id)
    text = next((m.caption for m in messages if m.caption), "") or ""
    brand_tag = utils.extract_brand_tag(text)

    file_ids = [m.photo[-1].file_id for m in messages if m.photo]

    post_id = database.save_post(
        channel_message_id=channel_message_id,  # type: ignore[arg-type]
        text=text,
        media_type="album",
        file_ids=file_ids,
        brand_tag=brand_tag,
    )

    await first_with_id.reply(
        _format_saved_reply(post_id, brand_tag, "album", parts=len(file_ids))
    )


def _schedule_album_flush(media_group_id: str) -> None:
    existing = _album_flush_tasks.get(media_group_id)
    if existing is not None and not existing.done():
        existing.cancel()

    _album_flush_tasks[media_group_id] = asyncio.create_task(_flush_album(media_group_id))


# --------------------------------------------------------------------------- #
# Хендлеры
# --------------------------------------------------------------------------- #

@collector_router.message(F.media_group_id.is_not(None))
async def on_forwarded_album_part(message: Message) -> None:
    """Часть альбома. Копится в буфере, сохраняется целиком после паузы."""
    if not _is_forwarded_from_channel(message):
        return

    media_group_id = message.media_group_id
    assert media_group_id is not None

    _album_buffers[media_group_id].append(message)
    _schedule_album_flush(media_group_id)


@collector_router.message(F.forward_origin.is_not(None) | F.forward_from_chat.is_not(None))
async def on_forwarded_single(message: Message) -> None:
    """Одиночный пересланный пост (фото с подписью или просто текст)."""
    if message.media_group_id:
        return  # часть альбома — обработается в on_forwarded_album_part

    await _save_single(message)


@collector_router.message()
async def on_other_message(message: Message) -> None:
    """Подсказка админу, если он прислал что-то, кроме пересланного поста."""
    await message.reply(
        "Перешлите мне пост из вашего канала — сохраню текст, медиа и хэштег бренда.\n"
        "Команда /start — открыть панель управления автопостером."
    )


# --------------------------------------------------------------------------- #
# Форматирование ответа
# --------------------------------------------------------------------------- #

def _format_saved_reply(
    post_id: int,
    brand_tag: str | None,
    media_type: str,
    parts: int,
) -> str:
    media_label = {
        "photo": "фото",
        "album": f"альбом из {parts} фото",
        "none": "текст",
    }[media_type]

    brand_line = f"Бренд: {brand_tag}" if brand_tag else "⚠️ Бренд-хэштег не найден в тексте."

    return f"✅ Пост #{post_id} сохранён ({media_label}).\n{brand_line}"
