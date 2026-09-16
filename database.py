"""
database.py

Работа с SQLite-базой для Telegram-автопостера.

Таблица posts хранит скопированные посты канала:
- id                  — первичный ключ
- channel_message_id  — id сообщения в канале (уникален, используется для upsert)
- text                — текст поста
- media_type          — 'photo' / 'album' / 'none'
- file_ids            — JSON-список file_id медиафайлов
- brand_tag           — найденный хэштег бренда (#LouisVuitton, #Gucci и т.д.)
- last_posted_at      — когда пост последний раз публиковался ботом (NULL, если ни разу)
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

# На Render с подключённым persistent-диском задайте DB_PATH=/var/data/autoposter.db
# (путь внутри mountPath диска) — тогда база переживёт рестарты и деплои.
# Без диска (бесплатный тариф) файл создаётся локально и теряется при рестарте контейнера.
DB_PATH: Path = Path(os.environ.get("DB_PATH", "autoposter.db"))


# --------------------------------------------------------------------------- #
# Модель данных
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    target_channel_id: Optional[int]
    selected_brand: str
    interval_minutes: int
    is_active: bool

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Settings":
        return cls(
            target_channel_id=row["target_channel_id"],
            selected_brand=row["selected_brand"],
            interval_minutes=row["interval_minutes"],
            is_active=bool(row["is_active"]),
        )


@dataclass
class Post:
    id: int
    channel_message_id: int
    text: str
    media_type: str
    file_ids: list[str]
    brand_tag: Optional[str]
    last_posted_at: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Post":
        return cls(
            id=row["id"],
            channel_message_id=row["channel_message_id"],
            text=row["text"],
            media_type=row["media_type"],
            file_ids=json.loads(row["file_ids"]) if row["file_ids"] else [],
            brand_tag=row["brand_tag"],
            last_posted_at=row["last_posted_at"],
        )


# --------------------------------------------------------------------------- #
# Подключение
# --------------------------------------------------------------------------- #

def get_connection(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    """Создаёт соединение с базой; строки доступны по имени столбца."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    """Создаёт таблицу posts, если она ещё не существует."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    with get_connection(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS posts (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_message_id  INTEGER UNIQUE NOT NULL,
                text                TEXT,
                media_type          TEXT NOT NULL DEFAULT 'none'
                                        CHECK (media_type IN ('photo', 'album', 'none')),
                file_ids            TEXT,
                brand_tag           TEXT,
                last_posted_at      TIMESTAMP NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_posts_brand_tag ON posts (brand_tag)"
        )

        # Единственная строка настроек (id всегда = 1)
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settings (
                id                  INTEGER PRIMARY KEY CHECK (id = 1),
                target_channel_id   INTEGER,
                selected_brand      TEXT NOT NULL DEFAULT 'ALL',
                interval_minutes    INTEGER NOT NULL DEFAULT 60,
                is_active           INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO settings (id, selected_brand, interval_minutes, is_active)
            VALUES (1, 'ALL', 60, 0)
            """
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Запись
# --------------------------------------------------------------------------- #

def save_post(
    channel_message_id: int,
    text: str,
    media_type: str,
    file_ids: list[str],
    brand_tag: Optional[str],
    db_path: Path | str = DB_PATH,
) -> int:
    """
    Сохраняет пост из канала. Если пост с таким channel_message_id уже есть —
    обновляет его данные (upsert), не трогая last_posted_at.

    Возвращает id записи в таблице posts.
    """
    file_ids_json = json.dumps(file_ids, ensure_ascii=False)

    with get_connection(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO posts (channel_message_id, text, media_type, file_ids, brand_tag)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(channel_message_id) DO UPDATE SET
                text = excluded.text,
                media_type = excluded.media_type,
                file_ids = excluded.file_ids,
                brand_tag = excluded.brand_tag
            """,
            (channel_message_id, text, media_type, file_ids_json, brand_tag),
        )
        conn.commit()

        row = conn.execute(
            "SELECT id FROM posts WHERE channel_message_id = ?",
            (channel_message_id,),
        ).fetchone()
        return row["id"]


def mark_as_posted(
    post_id: int,
    posted_at: Optional[datetime] = None,
    db_path: Path | str = DB_PATH,
) -> None:
    """Проставляет last_posted_at для поста (по умолчанию — текущее время)."""
    timestamp = (posted_at or datetime.now()).isoformat(sep=" ", timespec="seconds")
    with get_connection(db_path) as conn:
        conn.execute(
            "UPDATE posts SET last_posted_at = ? WHERE id = ?",
            (timestamp, post_id),
        )
        conn.commit()


# --------------------------------------------------------------------------- #
# Настройки постинга
# --------------------------------------------------------------------------- #

def get_settings(db_path: Path | str = DB_PATH) -> Settings:
    """Возвращает текущие настройки автопостинга (строка id=1)."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT * FROM settings WHERE id = 1").fetchone()
        return Settings.from_row(row)


def update_settings(
    target_channel_id: Optional[int] = None,
    selected_brand: Optional[str] = None,
    interval_minutes: Optional[int] = None,
    is_active: Optional[bool] = None,
    db_path: Path | str = DB_PATH,
) -> Settings:
    """
    Частично обновляет настройки: передавайте только те поля, которые
    нужно изменить, остальные останутся прежними.
    """
    current = get_settings(db_path)

    new_target = target_channel_id if target_channel_id is not None else current.target_channel_id
    new_brand = selected_brand if selected_brand is not None else current.selected_brand
    new_interval = interval_minutes if interval_minutes is not None else current.interval_minutes
    new_active = is_active if is_active is not None else current.is_active

    with get_connection(db_path) as conn:
        conn.execute(
            """
            UPDATE settings
            SET target_channel_id = ?,
                selected_brand = ?,
                interval_minutes = ?,
                is_active = ?
            WHERE id = 1
            """,
            (new_target, new_brand, new_interval, int(new_active)),
        )
        conn.commit()

    return get_settings(db_path)


# --------------------------------------------------------------------------- #
# Выборка
# --------------------------------------------------------------------------- #

def get_post_by_channel_message_id(
    channel_message_id: int,
    db_path: Path | str = DB_PATH,
) -> Optional[Post]:
    with get_connection(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM posts WHERE channel_message_id = ?",
            (channel_message_id,),
        ).fetchone()
        return Post.from_row(row) if row else None


def get_posts_by_brand(
    brand_tag: str,
    only_unposted: bool = False,
    db_path: Path | str = DB_PATH,
) -> list[Post]:
    """
    Возвращает все посты с указанным brand_tag.
    Если only_unposted=True — только те, что ещё ни разу не публиковались.
    """
    query = "SELECT * FROM posts WHERE brand_tag = ?"
    params: tuple = (brand_tag,)

    if only_unposted:
        query += " AND last_posted_at IS NULL"

    query += " ORDER BY id ASC"

    with get_connection(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        return [Post.from_row(row) for row in rows]


def get_all_brand_tags(db_path: Path | str = DB_PATH) -> list[str]:
    """Возвращает список уникальных найденных брендов (без NULL)."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT brand_tag FROM posts "
            "WHERE brand_tag IS NOT NULL ORDER BY brand_tag ASC"
        ).fetchall()
        return [row["brand_tag"] for row in rows]


def get_random_post_by_brand(
    brand_tag: str,
    db_path: Path | str = DB_PATH,
) -> Optional[Post]:
    """Берёт наименее давно опубликованный (или ещё не опубликованный) пост бренда."""
    with get_connection(db_path) as conn:
        row = conn.execute(
            """
            SELECT * FROM posts
            WHERE brand_tag = ?
            ORDER BY last_posted_at IS NOT NULL, last_posted_at ASC, id ASC
            LIMIT 1
            """,
            (brand_tag,),
        ).fetchone()
        return Post.from_row(row) if row else None


def get_next_post(
    selected_brand: str,
    db_path: Path | str = DB_PATH,
) -> Optional[Post]:
    """
    Выбирает следующий пост для публикации "по кругу":
    - если selected_brand == 'ALL' — среди всех постов;
    - иначе — только среди постов с указанным brand_tag.
    Сначала берутся ещё ни разу не опубликованные (last_posted_at IS NULL),
    затем — опубликованные наиболее давно. Это гарантирует, что посты
    не будут повторяться подряд, пока не закончится весь пул.
    """
    if selected_brand == "ALL":
        query = """
            SELECT * FROM posts
            ORDER BY last_posted_at IS NOT NULL, last_posted_at ASC, id ASC
            LIMIT 1
        """
        params: tuple = ()
    else:
        query = """
            SELECT * FROM posts
            WHERE brand_tag = ?
            ORDER BY last_posted_at IS NOT NULL, last_posted_at ASC, id ASC
            LIMIT 1
        """
        params = (selected_brand,)

    with get_connection(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return Post.from_row(row) if row else None


# --------------------------------------------------------------------------- #
# Статистика
# --------------------------------------------------------------------------- #

def count_posts(db_path: Path | str = DB_PATH) -> int:
    """Общее количество постов в базе."""
    with get_connection(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) AS cnt FROM posts").fetchone()
        return int(row["cnt"])


def count_posts_by_brand(db_path: Path | str = DB_PATH) -> dict[str, int]:
    """Возвращает {brand_tag: количество постов} по всем брендам (без NULL), по алфавиту."""
    with get_connection(db_path) as conn:
        rows = conn.execute(
            """
            SELECT brand_tag, COUNT(*) AS cnt
            FROM posts
            WHERE brand_tag IS NOT NULL
            GROUP BY brand_tag
            ORDER BY brand_tag ASC
            """
        ).fetchall()
        return {row["brand_tag"]: int(row["cnt"]) for row in rows}
