"""Нативный приём файлов из Проводника Windows через ``WM_DROPFILES``.

Это стандартный Win32-механизм ``DragAcceptFiles()``: Windows сам отправляет
окну ``WM_DROPFILES`` с ``HDROP``, откуда ``DragQueryFileW`` читает пути.
Перехват ставится прямо на HWND окна, поэтому GTK-обработчик броска не
конкурирует с ним и не может вставить путь в редактируемый текст.

Раньше установка обработчика всегда считалась неудачной: ``SetWindowLongPtrW``
при успехе возвращает **previous WNDPROC** (ненулевое значение), а код
проверял результат на ``True`` как ошибку. Здесь проверка исправлена, а
``RegisterDragDrop`` из ``win_com_drop`` оставлен как дополнительный путь.
"""

from __future__ import annotations

import ctypes
import os
from collections.abc import Callable

# GWLP_WNDPROC: -4 для 64-разрядного процесса, -21 для 32-разрядного.
_GWLP_WNDPROC = -4 if ctypes.sizeof(ctypes.c_void_p) == 8 else -21
_WM_DROPFILES = 0x0233
_INVALID_INDEX = 0xFFFFFFFF
_MSGFLT_ALLOW = 1

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

_user32 = None
_shell32 = None
_kernel32 = None
_get_window_long_ptr = None
_set_window_long_ptr = None
_get_last_error = None
_set_last_error = None
_subclass_proc = None
# Один WindowProc на процесс, но своя пара «исходный обработчик/колбэк»
# для каждого HWND: окон может быть несколько (например, в тестах).
_windows: dict[int, tuple[int, Callable[[list[str]], object]]] = {}


class NativeDropError(RuntimeError):
    """Не удалось установить нативный обработчик броска файлов."""


def _subclass_proc_type():
    """Тип WindowProc доступен только в Windows-версии ctypes."""
    return _FUNCTYPE(
        ctypes.c_ssize_t,  # LRESULT
        ctypes.c_void_p,  # HWND
        ctypes.c_uint,  # UINT
        ctypes.c_void_p,  # WPARAM
        ctypes.c_void_p,  # LPARAM
    )


def _libraries():
    """Загрузить user32/shell32 и один раз настроить сигнатуры функций."""
    global _user32, _shell32, _kernel32, _get_window_long_ptr, _set_window_long_ptr
    global _get_last_error, _set_last_error
    if _user32 is not None and _shell32 is not None:
        return _user32, _shell32
    if os.name != "nt":
        raise NativeDropError("Нативный DnD доступен только в Windows")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

    has_ptr = hasattr(user32, "GetWindowLongPtrW")
    get_ptr = user32.GetWindowLongPtrW if has_ptr else user32.GetWindowLongW
    set_ptr = user32.SetWindowLongPtrW if has_ptr else user32.SetWindowLongW
    result_type = ctypes.c_ssize_t if has_ptr else ctypes.c_long

    get_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int]
    get_ptr.restype = result_type
    # SetWindowLongPtrW возвращает предыдущий WNDPROC. Это не useState:
    # ненулевое значение означает успех, а не ошибку.
    set_ptr.argtypes = [ctypes.c_void_p, ctypes.c_int, result_type]
    set_ptr.restype = result_type

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

    # GetLastError/SetLastError живут в kernel32, а не в user32.
    get_last_error = kernel32.GetLastError
    get_last_error.argtypes = []
    get_last_error.restype = ctypes.c_uint
    set_last_error = kernel32.SetLastError
    set_last_error.argtypes = [ctypes.c_uint]
    set_last_error.restype = None

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

    # Если приложение запущено с более высокими правами, чем Проводник,
    # фильтр UIPI может заблокировать сообщение. Разрешение безвредно и
    # игнорируется на старых версиях Windows.
    if hasattr(user32, "ChangeWindowMessageFilterEx"):
        change_filter = user32.ChangeWindowMessageFilterEx
        change_filter.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_ulong,
            ctypes.c_void_p,
        ]
        change_filter.restype = ctypes.c_int

    _get_window_long_ptr = get_ptr
    _set_window_long_ptr = set_ptr
    _get_last_error = get_last_error
    _set_last_error = set_last_error
    _user32 = user32
    _shell32 = shell32
    _kernel32 = kernel32
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
    исходный WindowProc через ``CallWindowProcW``. GDK не получает этот
    бросок, поэтому файл не открывается дважды и не вставляется как текст.
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
        _libraries()
        global _subclass_proc
        if _subclass_proc is None:
            _subclass_proc = _subclass_proc_type()(_window_proc)
        assert _get_window_long_ptr is not None
        assert _set_window_long_ptr is not None
        assert _get_last_error is not None
        assert _set_last_error is not None
        previous = int(_get_window_long_ptr(hwnd, _GWLP_WNDPROC) or 0)
        if not previous:
            raise NativeDropError("GetWindowLongPtrW вернул 0")
        _set_last_error(0)
        callback_pointer = ctypes.cast(_subclass_proc, ctypes.c_void_p).value
        changed = int(_set_window_long_ptr(hwnd, _GWLP_WNDPROC, callback_pointer) or 0)
        if not changed and int(_get_last_error() or 0):
            raise NativeDropError("SetWindowLongPtrW не удался")
        _windows[hwnd] = (previous, self._callback)
        self._hwnd = hwnd
        self._installed = True
        # GDK вызывает DragAcceptFiles только в gdk_window_register_dnd(),
        # а GTK-цели в Windows отключены, поэтому включаем приём сами.
        assert _shell32 is not None
        _shell32.DragAcceptFiles(hwnd, True)
        user32, _ = _libraries()
        change_filter = getattr(user32, "ChangeWindowMessageFilterEx", None)
        if change_filter is not None:
            try:
                change_filter(hwnd, _WM_DROPFILES, _MSGFLT_ALLOW, None)
            except Exception:  # noqa: BLE001 - функция есть не везде
                pass
        return True

    def uninstall(self) -> None:
        """Снять подкласс окна, восстановив исходный WindowProc."""
        if not self._installed or self._hwnd is None:
            return
        user32, shell32 = _libraries()
        assert _set_window_long_ptr is not None
        entry = _windows.pop(self._hwnd, None)
        if entry is not None and entry[0]:
            _set_window_long_ptr(self._hwnd, _GWLP_WNDPROC, entry[0])
        shell32.DragAcceptFiles(self._hwnd, False)
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
