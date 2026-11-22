"""Нативный приём файлов из Проводника Windows.

GDK Win32 умеет превращать ``WM_DROPFILES`` в ``text/uri-list``, но в Windows 11
этот путь зависел от experimental OLE2-флага и на практике работал ненадёжно.
Здесь сообщение перехватывается прямо на HWND окна: пути берутся из ``HDROP``
через ``DragQueryFileW``, после чего ``DragFinish`` освобождает память, а GDK
уже не видит событие и не может обработать тот же бросок второй раз.

Модуль не вызывает Windows API на других ОС, поэтому разбор ``HDROP`` можно
проверять в тестах на Linux. Окно получает только список путей; открывают их
уже существующие методы ``MainWindow``.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable

# GWLP_WNDPROC: -4 для 64-разрядного процесса, -21 для 32-разрядного.
_GWLP_WNDPROC = -4 if ctypes.sizeof(ctypes.c_void_p) == 8 else -21
_WM_DROPFILES = 0x0233
_INVALID_INDEX = 0xFFFFFFFF

_user32 = None
_shell32 = None
_get_window_long_ptr = None
_set_window_long_ptr = None
_subclass_proc = None
# Один WindowProc на процесс, но своя пара «исходный обработчик/колбэк»
# для каждого HWND: окон может быть несколько (например, в тестах).
_windows: dict[int, tuple[int, Callable[[list[str]], object]]] = {}


class NativeDropError(RuntimeError):
    """Не удалось установить нативный обработчик броска файлов."""


def _subclass_proc_type():
    """Тип WindowProc доступен только в Windows-версии ctypes."""
    return ctypes.WINFUNCTYPE(  # type: ignore[attr-defined]
        ctypes.c_ssize_t,  # LRESULT
        ctypes.c_void_p,  # HWND
        ctypes.c_uint,  # UINT
        ctypes.c_void_p,  # WPARAM
        ctypes.c_void_p,  # LPARAM
    )


def _libraries():
    """Загрузить user32/shell32 и настроить сигнатуры функций."""
    global _user32, _shell32, _get_window_long_ptr, _set_window_long_ptr
    if _user32 is not None and _shell32 is not None:
        return _user32, _shell32
    if os.name != "nt":
        raise NativeDropError("Нативный DnD доступен только в Windows")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]

    has_ptr = hasattr(user32, "GetWindowLongPtrW")
    get_ptr = user32.GetWindowLongPtrW if has_ptr else user32.GetWindowLongW
    set_ptr = user32.SetWindowLongPtrW if has_ptr else user32.SetWindowLongW
    result_type = ctypes.c_ssize_t if has_ptr else ctypes.c_long
    _get_window_long_ptr = get_ptr
    _set_window_long_ptr = set_ptr

    get_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_ptr.restype = result_type
    set_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, result_type]
    set_ptr.restype = None

    call_proc = user32.CallWindowProcW
    call_proc.argtypes = [
        result_type,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    call_proc.restype = ctypes.c_ssize_t

    def_window_proc = user32.DefWindowProcW
    def_window_proc.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    def_window_proc.restype = ctypes.c_ssize_t

    # DragAcceptFiles живёт в shell32, а не в user32.
    drag_accept = shell32.DragAcceptFiles
    drag_accept.argtypes = [ctypes.c_void_p, ctypes.c_bool]
    drag_accept.restype = None

    query = shell32.DragQueryFileW
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    query.restype = ctypes.c_uint

    finish = shell32.DragFinish
    finish.argtypes = [ctypes.c_void_p]
    finish.restype = None

    _user32 = user32
    _shell32 = shell32
    return user32, shell32


def dropped_paths_from_hdrop(
    hdrop: int, shell32=None, max_files: int = 512
) -> list[str]:
    """Прочитать все пути из ``HDROP``.

    ``DragQueryFileW(hdrop, 0xFFFFFFFF, ...)`` возвращает количество файлов.
    Вызов с нулевым буфером даёт длину строки вместе с завершающим ``\\0``.
    Функция не вызывает ``DragFinish`` — это делает владелец сообщения.
    """
    if shell32 is None:
        shell32 = _libraries()[1]
    count = shell32.DragQueryFileW(hdrop, _INVALID_INDEX, None, 0)
    if not count or count == _INVALID_INDEX:
        return []
    paths: list[str] = []
    for index in range(min(count, max_files)):
        length = shell32.DragQueryFileW(hdrop, index, None, 0)
        if not length:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        received = shell32.DragQueryFileW(hdrop, index, buffer, len(buffer))
        if received and buffer.value:
            paths.append(buffer.value)
    return paths


def _window_proc(hwnd, message: int, wparam, lparam) -> int:
    """Единый WindowProc: перехватить WM_DROPFILES, остальное отдать GDK."""
    user32, shell32 = _libraries()
    entry = _windows.get(hwnd)
    if entry is None:
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)
    previous, callback = entry
    if message == _WM_DROPFILES:
        paths = dropped_paths_from_hdrop(wparam, shell32)
        shell32.DragFinish(wparam)
        if paths:
            callback(paths)
        return 0
    return user32.CallWindowProcW(previous, hwnd, message, wparam, lparam)


class NativeFileDrop:
    """Перехватывает ``WM_DROPFILES`` на окне верхнего уровня.

    Пути попадают в ``callback`` списком строк. Остальные сообщения идут в
    исходный WindowProc через ``CallWindowProcW``. GTK-обработчик броска при
    этом не вызывается, поэтому файл не открывается дважды.
    """

    def __init__(self, callback: Callable[[list[str]], object]) -> None:
        self._callback = callback
        self._hwnd: int | None = None
        self._installed = False

    @property
    def installed(self) -> bool:
        return self._installed

    def install(self, hwnd: int) -> bool:
        """Установить подкласс окна. Возвращает True при успехе."""
        if not hwnd or self._installed or hwnd in _windows:
            return False
        _libraries()  # инициализация сигнатур Windows API
        global _subclass_proc
        if _subclass_proc is None:
            _subclass_proc = _subclass_proc_type()(_window_proc)
        assert _get_window_long_ptr is not None
        assert _set_window_long_ptr is not None
        previous = _get_window_long_ptr(hwnd, _GWLP_WNDPROC)
        if not previous:
            raise NativeDropError("GetWindowLongPtrW вернул 0")
        if _set_window_long_ptr(hwnd, _GWLP_WNDPROC, _subclass_proc):
            raise NativeDropError("SetWindowLongPtrW не удался")
        _windows[hwnd] = (previous, self._callback)
        self._hwnd = hwnd
        self._installed = True
        # GDK вызывает DragAcceptFiles только в gdk_window_register_dnd().
        # В нативном режиме мы не регистрируем GTK-цели, поэтому включаем приём
        # файлов сами.
        assert _shell32 is not None
        _shell32.DragAcceptFiles(hwnd, True)
        return True

    def uninstall(self) -> None:
        """Снять подкласс окна, восстановив исходный WindowProc."""
        if not self._installed or self._hwnd is None:
            return
        _libraries()
        assert _set_window_long_ptr is not None
        previous = _windows.pop(self._hwnd, (None, None))[0]
        if previous is not None:
            _set_window_long_ptr(self._hwnd, _GWLP_WNDPROC, previous)
        assert _shell32 is not None
        _shell32.DragAcceptFiles(self._hwnd, False)
        self._installed = False
        self._hwnd = None


def install_for_window(
    callback: Callable[[list[str]], object], gdk_window
) -> NativeFileDrop | None:
    """Установить хук для HWND, принадлежащего ``GdkWindow``."""
    if os.name != "nt" or gdk_window is None:
        return None
    from . import win_theme

    hwnd = win_theme.gdk_window_handle(gdk_window)
    if not hwnd:
        return None
    drop = NativeFileDrop(callback)
    try:
        drop.install(hwnd)
    except NativeDropError:
        return None
    return drop if drop.installed else None
