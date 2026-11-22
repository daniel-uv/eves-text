"""Включение стандартного OLE2-перетаскивания файлов в Windows.

GDK Win32 в GTK3 по умолчанию использует устаревший ``WM_DROPFILES``.
Проводник Windows 11 надёжнее работает через OLE2 ``IDropTarget``: GDK
регистрирует его для HWND окна и сам превращает ``CFSTR_SHELLIDLIST``
(формат, который отдаёт Проводник) в ``text/uri-list``. Это штатный путь,
поэтому отдельный перехват сообщений больше не нужен.
"""

from __future__ import annotations

import os

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"


def enable_ole2_dnd() -> None:
    """Включить OLE2 DnD до вызова Gtk.init(). Безопасно на всех ОС."""
    if os.name == "nt":
        # setdefault оставляет возможность отключить диагностику вручную.
        os.environ.setdefault(OLE2_ENV, "1")


def ole2_dnd_enabled() -> bool:
    """True, если GDK будет использовать OLE2 для перетаскивания."""
    return os.environ.get(OLE2_ENV, "").strip() not in ("", "0", "false", "no")
