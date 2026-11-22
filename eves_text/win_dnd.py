"""Настройка перетаскивания файлов в Windows.

Файлы из Проводника принимает собственный COM ``IDropTarget`` окна. GDK-механизм
не используется: experimental OLE2 в Windows 11 работает ненадёжно, а GTK-цели
могут зарегистрировать второй ``IDropTarget`` на том же HWND.

GDK Win32 проверяет только **наличие** ``GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND``.
Значение ``0`` переменную не отключает, поэтому удаляем её до ``Gtk.init()``.
"""

from __future__ import annotations

import os

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"


def use_com_file_drop() -> bool:
    """True, если в Windows файлы принимает наш COM ``IDropTarget``."""
    return os.name == "nt"


def configure_dnd() -> None:
    """Отключить GDK OLE2 до инициализации GDK. Безопасно на всех ОС."""
    if os.name == "nt":
        os.environ.pop(OLE2_ENV, None)
