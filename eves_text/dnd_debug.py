"""Диагностика перетаскивания файлов.

В обычном режиме модуль не пишет на диск и не трогает Win32: лог включается
переменной ``EVES_TEXT_DND_DEBUG=1`` или пустым файлом ``dnd-debug.flag`` в
``%LOCALAPPDATA%\\Eve's Text``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import win_dnd


def log_dir() -> Path | None:
    """Каталог диагностического лога: ``%LOCALAPPDATA%\\Eve's Text``."""
    root = os.environ.get("LOCALAPPDATA")
    if root:
        return Path(root) / "Eve's Text"
    try:
        return Path.home() / "Eve's Text"
    except (OSError, RuntimeError):
        return None


def enabled() -> bool:
    """Диагностика бросков включается переменной или файлом-флажком."""
    if os.environ.get("EVES_TEXT_DND_DEBUG"):
        return True
    directory = log_dir()
    if directory is None:
        return False
    try:
        return (directory / "dnd-debug.flag").exists()
    except OSError:
        return False


def log(message: str) -> None:
    """Дописать строку в ``dnd.log``; в обычном режиме ничего не делать."""
    if not enabled():
        return
    directory = log_dir()
    if directory is None:
        return
    try:
        path = directory / "dnd.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(
                f"{time.strftime('%Y-%m-%d %H:%M:%S')} "
                f"[pid={os.getpid()}] {message}\n"
            )
    except OSError:
        pass


def atom_name(atom) -> str:
    """Имя атома без ``Gdk.atom_name``: в PyGObject его нет."""
    if atom is None:
        return "NONE"
    try:
        return atom.name() or "NONE"
    except (AttributeError, TypeError):
        return "?"


def atom_is_none(atom) -> bool:
    return atom is None or atom_name(atom) in ("NONE", "")


def integrity_info() -> tuple[bool, str]:
    """Вернуть (is_admin, integrity_level) на Windows для диагностики UIPI."""
    if not win_dnd.is_windows():
        return False, "non-windows"

    import ctypes
    from ctypes import wintypes

    is_admin = False
    try:
        is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        pass

    level = "unknown"
    try:
        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.GetCurrentProcess.argtypes = []
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.GetSidSubAuthorityCount.argtypes = [ctypes.c_void_p]
        advapi32.GetSidSubAuthorityCount.restype = ctypes.POINTER(ctypes.c_ubyte)
        advapi32.GetSidSubAuthority.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        advapi32.GetSidSubAuthority.restype = ctypes.POINTER(wintypes.DWORD)

        token = wintypes.HANDLE()
        token_query = 0x0008
        token_integrity_level = 25
        if advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(token)
        ):
            try:
                needed = wintypes.DWORD(0)
                advapi32.GetTokenInformation(
                    token, token_integrity_level, None, 0, ctypes.byref(needed)
                )
                if needed.value:
                    buf = ctypes.create_string_buffer(needed.value)
                    if advapi32.GetTokenInformation(
                        token,
                        token_integrity_level,
                        buf,
                        needed,
                        ctypes.byref(needed),
                    ):
                        # TOKEN_MANDATORY_LABEL = { SID_AND_ATTRIBUTES {
                        #   PSID Sid; DWORD Attributes; } }
                        sid_ptr = ctypes.cast(
                            buf, ctypes.POINTER(ctypes.c_void_p)
                        )[0]
                        count = advapi32.GetSidSubAuthorityCount(
                            sid_ptr
                        ).contents.value
                        if count:
                            rid = advapi32.GetSidSubAuthority(
                                sid_ptr, count - 1
                            ).contents.value
                            if rid < 0x2000:
                                level = f"Low({rid:#x})"
                            elif rid < 0x3000:
                                level = f"Medium({rid:#x})"
                            elif rid < 0x4000:
                                level = f"High({rid:#x})"
                            else:
                                level = f"System({rid:#x})"
            finally:
                kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001
        pass
    return is_admin, level
