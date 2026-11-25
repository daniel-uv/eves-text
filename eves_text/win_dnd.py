"""Настройка перетаскивания файлов в Windows.

Основной приём файлов — ``DragAcceptFiles``/``WM_DROPFILES`` через ``win_drop``.
GDK-механизм не используется: experimental OLE2 в Windows 11 работает ненадёжно,
а GTK-цели могут зарегистрировать второй ``IDropTarget`` на том же HWND. Если
нативный перехват недоступен, применяется запасной COM ``IDropTarget``.

GDK Win32 проверяет только **наличие** ``GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND``.
Значение ``0`` переменную не отключает, поэтому удаляем её до ``Gtk.init()``.
Там же поднимаем OLE через ``OleInitialize()``: GDK вызовет только
CoInitializeEx, а RegisterDragDrop без OLE возвращает E_OUTOFMEMORY.
"""

from __future__ import annotations

import os

OLE2_ENV = "GDK_WIN32_USE_EXPERIMENTAL_OLE2_DND"


def use_com_file_drop() -> bool:
    """True, если в Windows файлы принимает наш COM ``IDropTarget``."""
    return os.name == "nt"


def configure_dnd() -> None:
    """Подготовить OLE и отключить GDK OLE2 до инициализации GDK.

    Порядок важен: GDK вызывает CoInitializeEx при Gtk.init(), а
    RegisterDragDrop требует OLE-инициализации. Поэтому сначала поднимаем OLE,
    и только потом убираем переменную GDK. Безопасно на всех ОС.
    """
    if os.name != "nt":
        return
    os.environ.pop(OLE2_ENV, None)
    from . import win_com_drop

    try:
        win_com_drop.initialize_ole()
    except Exception:  # noqa: BLE001 - DnD не должен мешать запуску редактора
        # Ошибка проявится при установке IDropTarget: окно просто останется
        # без приёмника броска, но само приложение продолжит работать.
        pass
