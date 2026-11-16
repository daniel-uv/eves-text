"""Тёмная шапка окна в Windows 10/11 (без GTK)."""

from __future__ import annotations

import ctypes
import os

# DWMWA_USE_IMMERSIVE_DARK_MODE: 20 в Windows 11 и новых сборках Windows 10,
# 19 — в ранних сборках Windows 10.
_DARK_MODE_ATTRIBUTES = (20, 19)
_SWP_NOSIZE = 0x0001
_SWP_NOMOVE = 0x0002
_SWP_NOZORDER = 0x0004
_SWP_NOACTIVATE = 0x0010
_SWP_FRAMECHANGED = 0x0020
_DWMAPI = None
_USER32 = None


def _libraries():
    global _DWMAPI, _USER32
    if _DWMAPI is None or _USER32 is None:
        dwmapi = ctypes.windll.dwmapi
        user32 = ctypes.windll.user32
        dwmapi.DwmSetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_uint,
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        user32.SetWindowPos.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        ]
        user32.SetWindowPos.restype = ctypes.c_int
        _DWMAPI = dwmapi
        _USER32 = user32
    return _DWMAPI, _USER32


def _gdk_pointer(gdk_window) -> int:
    """Указатель на GdkWindow из PyCapsule."""
    capsule = getattr(gdk_window, "__gpointer__", None)
    if capsule is None:
        return 0
    get_pointer = ctypes.pythonapi.PyCapsule_GetPointer
    get_pointer.restype = ctypes.c_void_p
    get_pointer.argtypes = [ctypes.py_object, ctypes.c_char_p]
    return int(get_pointer(capsule, None) or 0)


def gdk_window_handle(gdk_window) -> int:
    """HWND для GdkWindow на Windows; 0, если получить не удалось."""
    if os.name != "nt" or gdk_window is None:
        return 0

    # В новых PyGObject gdk_win32_window_get_handle доступен через GdkWin32.
    try:
        from gi.repository import GdkWin32

        handle = GdkWin32.Win32Window.get_handle(gdk_window)
        if handle:
            return int(handle)
    except (ImportError, ValueError, TypeError, AttributeError):
        pass

    # В остальных случаях вызываем функцию GDK напрямую: в GI её может не быть.
    try:
        pointer = _gdk_pointer(gdk_window)
        if not pointer:
            return 0
        gdk = ctypes.CDLL("libgdk-3-0.dll")
        function = gdk.gdk_win32_window_get_handle
        function.restype = ctypes.c_void_p
        function.argtypes = [ctypes.c_void_p]
        return int(function(pointer) or 0)
    except (OSError, AttributeError, TypeError, ValueError):
        return 0


def apply_dark_titlebar(hwnd: int, dark: bool) -> None:
    """Переключает тёмное оформление заголовка окна Windows."""
    if os.name != "nt" or not hwnd:
        return
    try:
        dwmapi, user32 = _libraries()
    except (AttributeError, OSError):
        return

    value = ctypes.c_int(1 if dark else 0)
    applied = False
    for attribute in _DARK_MODE_ATTRIBUTES:
        try:
            result = dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                attribute,
                ctypes.byref(value),
                ctypes.sizeof(value),
            )
        except (AttributeError, OSError):
            return
        if result == 0:
            applied = True
            break
    if not applied:
        return

    # Попросить Windows перерисовать рамку с новым оформлением.
    try:
        user32.SetWindowPos(
            ctypes.c_void_p(hwnd),
            None,
            0,
            0,
            0,
            0,
            _SWP_NOSIZE
            | _SWP_NOMOVE
            | _SWP_NOZORDER
            | _SWP_NOACTIVATE
            | _SWP_FRAMECHANGED,
        )
    except (AttributeError, OSError):
        pass
