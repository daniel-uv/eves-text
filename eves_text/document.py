"""Работа с файлами: чтение/запись и статистика текста.

Отдельный модуль без зависимостей от GTK — его же будет использовать
модуль проверки орфографии и ИИ-проверки на следующих шагах.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# Порядок важен: utf-8-sig корректно читает и обычный UTF-8, и файл с BOM.
_ENCODINGS = ("utf-8-sig", "cp1251")


def read_text(path: str | os.PathLike) -> tuple[str, str]:
    """Прочитать файл, вернув (текст, название кодировки).

    Пробуем UTF-8, затем CP1251 (частая кодировка старых русских файлов).
    Если не вышло — читаем как UTF-8 с заменой «битых» символов.
    """
    data = Path(path).read_bytes()
    for encoding in _ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text, "UTF-8" if encoding.startswith("utf") else "CP1251"
    return data.decode("utf-8", errors="replace"), "UTF-8 (с заменами)"


def write_text(path: str | os.PathLike, text: str) -> None:
    """Атомарно записать текст в UTF-8.

    Пишем во временный файл рядом и переименовываем: если запись прервётся,
    исходный файл останется целым.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def text_stats(text: str) -> tuple[int, int, int]:
    """Вернуть (слов, символов, символов без пробелов)."""
    words = len(text.split())
    chars = len(text)
    chars_no_spaces = sum(1 for ch in text if not ch.isspace())
    return words, chars, chars_no_spaces
