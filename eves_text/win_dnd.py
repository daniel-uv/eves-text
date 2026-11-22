"""Выбор механизма перетаскивания файлов в Windows.

По умолчанию файлы из Проводника принимаются нативным ``WM_DROPFILES``:
это короткий и предсказуемый путь, который не зависит от experimental OLE2
реализации GDK. GTK-цели перетаскивания продолжают работать на других ОС и в
Windows в режиме OLE2.

Важно: GDK Win32 проверяет только **наличие** ``GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND``.
Значение ``0`` переменную не отключает, поэтому в нативном режиме её нужно
удалить из окружения до вызова ``Gtk.init()``.
"""

from __future__ import annotations

import os

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"
OLE2_OPT_IN_ENV = "EVESTEXT_USE_OLE2_DND"


def _env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() not in ("", "0", "false", "no")


def use_native_file_drop() -> bool:
    """True, если в Windows файлы принимаются через ``WM_DROPFILES``."""
    if os.name != "nt":
        return False
    # OLE2 оставляем только явным opted-in режимом для диагностики.
    return not _env_flag(OLE2_OPT_IN_ENV)


def configure_dnd() -> None:
    """Настроить окружение DnD до инициализации GDK. Безопасно на всех ОС."""
    if os.name != "nt":
        return
    if use_native_file_drop():
        os.environ.pop(OLE2_ENV, None)
    else:
        os.environ[OLE2_ENV] = "1"


def enable_ole2_dnd() -> None:
    """Принудительно включить experimental OLE2 DnD (для отладки)."""
    if os.name == "nt":
        os.environ[OLE2_ENV] = "1"


def ole2_dnd_enabled() -> bool:
    """True, если GDK Win32 включит OLE2 (важно само наличие переменной)."""
    return os.name == "nt" and OLE2_ENV in os.environ
