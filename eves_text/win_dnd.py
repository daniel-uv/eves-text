"""Настройка перетаскивания файлов из Проводника Windows.

Файлы из Проводника Windows приходят как OLE2-бросок. Штатный путь GTK —
включить OLE2-приёмник GDK переменной окружения
``GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND`` **до** ``Gtk.init()`` и объявить
``text/uri-list`` целью через ``Gtk.Widget.drag_dest_set()``. Тогда GDK сам
зарегистрирует ``IDropTarget`` на HWND окна, а ``CFSTR_SHELLIDLIST``
Проводника превратит в ``text/uri-list``.

Переменную читает ``_gdk_dnd_init()`` при открытии дисплея, причём
проверяется только её наличие: значение ``"0"`` ничего не отключает.
Собственный COM-приёмник (``win_com_drop``) остался запасным путём — окно
ставит его, только если GDK почему-то не зарегистрировал свой.
"""

from __future__ import annotations

import os

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"


def is_windows() -> bool:
    """True, если работаем в Windows."""
    return os.name == "nt"


def configure_dnd() -> None:
    """Включить OLE2-перетаскивание GDK до инициализации GDK.

    Вызывается при импорте пакета и ещё раз из лаунчера и проверок рантайма,
    поэтому функция идемпотентна. На других ОС ничего не делает.
    """
    if not is_windows():
        return
    os.environ[OLE2_ENV] = "1"


def use_gdk_file_drop() -> bool:
    """True, если файлы принимает штатный OLE2-приёмник GDK."""
    return is_windows() and OLE2_ENV in os.environ


def ole2_dnd_enabled() -> bool:
    """Совместимое имя для проверок: OLE2 включён для GDK."""
    return use_gdk_file_drop()
