"""Пользовательские настройки приложения в JSON-файле.

Хранится только то, что не относится к конкретному документу: выбранная
модель Ollama и папка для её хранения. Модуль не зависит от GTK.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_DIR_NAME = "Eve's Text"
APP_DIR_SLUG = "eves-text"


def config_dir() -> Path:
    """Каталог настроек: %APPDATA% на Windows, ~/.config на Linux."""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_DIR_NAME
        return Path.home() / "AppData" / "Roaming" / APP_DIR_NAME

    base = os.environ.get("XDG_CONFIG_HOME")
    if base:
        return Path(base) / APP_DIR_SLUG
    return Path.home() / ".config" / APP_DIR_SLUG


def config_path() -> Path:
    return config_dir() / "settings.json"


def load() -> dict:
    """Читает настройки. Битый или отсутствующий файл — пустой словарь."""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Атомарно записывает настройки."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)
