"""
utils.py

Вспомогательные функции для парсинга текста постов:
поиск хэштегов и определение хэштега бренда.
"""

from __future__ import annotations

import re
from typing import Optional

# Хэштег: # + буквы/цифры/подчёркивание (юникод, чтобы ловить кириллицу тоже)
HASHTAG_PATTERN = re.compile(r"#([\w]+)", re.UNICODE)


def extract_hashtags(text: Optional[str]) -> list[str]:
    """
    Извлекает из текста все хэштеги в порядке появления, без дублей.
    Возвращает их с символом '#', в исходном регистре.

    >>> extract_hashtags("Новая сумка #LouisVuitton #новинка #LouisVuitton")
    ['#LouisVuitton', '#новинка']
    """
    if not text:
        return []

    seen: set[str] = set()
    result: list[str] = []

    for match in HASHTAG_PATTERN.finditer(text):
        tag = f"#{match.group(1)}"
        key = tag.lower()
        if key not in seen:
            seen.add(key)
            result.append(tag)

    return result


def extract_brand_tag(
    text: Optional[str],
    known_brands: Optional[list[str]] = None,
) -> Optional[str]:
    """
    Определяет хэштег бренда в тексте.

    Если передан known_brands (например ['LouisVuitton', 'Gucci', 'Prada']),
    возвращается первый хэштег из текста, совпадающий с одним из них
    (без учёта регистра).

    Если known_brands не передан, возвращается первый хэштег вообще
    (простая эвристика для случая, когда весь пост размечен одним тегом бренда).
    """
    hashtags = extract_hashtags(text)
    if not hashtags:
        return None

    if known_brands is None:
        return hashtags[0]

    known_lower = {brand.lstrip("#").lower() for brand in known_brands}
    for tag in hashtags:
        if tag.lstrip("#").lower() in known_lower:
            return tag

    return None


def strip_hashtags(text: Optional[str]) -> str:
    """Возвращает текст без хэштегов (например, для чистого превью поста)."""
    if not text:
        return ""
    cleaned = HASHTAG_PATTERN.sub("", text)
    return re.sub(r"[ \t]{2,}", " ", cleaned).strip()
