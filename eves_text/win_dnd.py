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
ставит его, только если GDK почему-то не зарегистрировал свой. Для
диагностики запасной путь можно включить принудительно: см.
``NO_OLE2_ENV``/``dnd-no-ole2.flag``.
"""

from __future__ import annotations

import os
from pathlib import Path

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"
NO_OLE2_ENV = "EVES_TEXT_DND_NO_OLE2"


def _no_ole2_flag() -> Path | None:
    """Файл-переключатель на запасной COM-приёмник вместо OLE2 GDK."""
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
    if root:
        return Path(root) / "Eve's Text" / "dnd-no-ole2.flag"
    try:
        return Path.home() / "Eve's Text" / "dnd-no-ole2.flag"
    except (OSError, RuntimeError):
        return None


def no_ole2_requested() -> bool:
    """True, если диагностика просит отключить OLE2-приёмник GDK."""
    if os.environ.get(NO_OLE2_ENV):
        return True
    flag = _no_ole2_flag()
    if flag is None:
        return False
    try:
        return flag.exists()
    except OSError:
        return False


def is_windows() -> bool:
    """True, если работаем в Windows."""
    return os.name == "nt"


def configure_dnd() -> None:
    """Включить OLE2-перетаскивание GDK до инициализации GDK.

    Вызывается при импорте пакета и ещё раз из лаунчера и проверок рантайма,
    поэтому функция идемпотентна. На других ОС ничего не делает.

    Для диагностики запасного пути (собственный ``IDropTarget`` в
    ``win_com_drop``) OLE2 можно отключить переменной ``EVES_TEXT_DND_NO_OLE2``
    или пустым файлом ``dnd-no-ole2.flag`` в ``%LOCALAPPDATA%\\Eve's Text``.
    Переменную нужно именно удалить: значение ``"0"`` GDK считает включением.
    """
    if not is_windows():
        return
    if no_ole2_requested():
        os.environ.pop(OLE2_ENV, None)
        return
    os.environ[OLE2_ENV] = "1"


def use_gdk_file_drop() -> bool:
    """True, если файлы принимает штатный OLE2-приёмник GDK."""
    return is_windows() and OLE2_ENV in os.environ


def ole2_dnd_enabled() -> bool:
    """Совместимое имя для проверок: OLE2 включён для GDK."""
    return use_gdk_file_drop()
