"""Собственный COM ``IDropTarget`` для файлов из Проводника Windows.

В Windows 11 файлы приходят как OLE-бросок. GDK-реализация работать отказалась,
поэтому окно регистрирует свой ``IDropTarget`` прямо на своём HWND и читает
данные у ``IDataObject`` источника. Основной формат — ``CFSTR_SHELLIDLIST``:
из него собираются PIDL, а ``SHGetPathFromIDListW`` возвращает реальные пути.
На всякий случай поддерживаются ``CF_HDROP`` и ``CFSTR_FILEDESCRIPTOR``.

Модуль не трогает Windows API на других ОС: разбор форматов можно проверять
в тестах на Linux. Окно получает только список путей, открывает их уже
существующий метод ``_open_dropped_uris()``.

``initialize_ole()`` вызывается до Gtk.init(): GDK инициализирует COM через
CoInitializeEx, а RegisterDragDrop требует предварительного OleInitialize.
"""

from __future__ import annotations

import ctypes
import os
import struct
import uuid
from collections.abc import Callable

# HRESULT — 32-битное знаковое число; константы ошибок записываем отрицательными.
def _hr(code: int) -> int:
    return code - 0x100000000 if code >= 0x80000000 else code


S_OK = 0
S_FALSE = 1
E_NOINTERFACE = _hr(0x80004002)
E_FAIL = _hr(0x80004005)
RPC_E_CHANGED_MODE = _hr(0x80010106)

CF_HDROP = 15
TYMED_HGLOBAL = 1
DVASPECT_CONTENT = 1
DATADIR_GET = 1
DROPEFFECT_NONE = 0
DROPEFFECT_COPY = 1

MAX_FILES = 512
MAX_PATH_BUFFER = 32768
FILEDESCRIPTORW_SIZE = 576
FILEDESCRIPTORW_NAME_OFFSET = 56
FILEDESCRIPTORW_NAME_SIZE = 520

_FUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)


class ComDropError(RuntimeError):
    """Не удалось зарегистрировать COM-приёмник файлов."""


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(value: str) -> GUID:
    parsed = uuid.UUID(value)
    return GUID(
        parsed.time_low,
        parsed.time_mid,
        parsed.time_hi_version,
        (ctypes.c_ubyte * 8)(*parsed.bytes_le[8:]),
    )


IID_IUNKNOWN = _guid("00000000-0000-0000-C000-000000000046")
IID_IDROPTARGET = _guid("00000122-0000-0000-C000-000000000046")


class POINTL(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class FORMATETC(ctypes.Structure):
    _fields_ = [
        ("cfFormat", ctypes.c_ushort),
        ("ptd", ctypes.c_void_p),
        ("dwAspect", ctypes.c_ulong),
        ("lindex", ctypes.c_long),
        ("tymed", ctypes.c_ulong),
    ]


class _STGMEDIUMUNION(ctypes.Union):
    _fields_ = [
        ("hBitmap", ctypes.c_void_p),
        ("hMetaPict", ctypes.c_void_p),
        ("hEnhMetaFile", ctypes.c_void_p),
        ("hGlobal", ctypes.c_void_p),
        ("lpszFileName", ctypes.c_void_p),
        ("pstm", ctypes.c_void_p),
        ("pStorage", ctypes.c_void_p),
    ]


class STGMEDIUM(ctypes.Structure):
    _fields_ = [
        ("tymed", ctypes.c_ulong),
        ("u", _STGMEDIUMUNION),
        ("pUnkForRelease", ctypes.c_void_p),
    ]


HRESULT = ctypes.c_long
ULONG = ctypes.c_ulong
DWORD = ctypes.c_ulong

_QUERY_INTERFACE = _FUNCTYPE(
    HRESULT, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)
)
_ADD_REF = _FUNCTYPE(ULONG, ctypes.c_void_p)
_RELEASE = _FUNCTYPE(ULONG, ctypes.c_void_p)
_DRAG = _FUNCTYPE(
    HRESULT,
    ctypes.c_void_p,
    ctypes.c_void_p,
    DWORD,
    POINTL,
    ctypes.POINTER(DWORD),
)
_DRAG_LEAVE = _FUNCTYPE(HRESULT, ctypes.c_void_p)


class IDropTargetVtbl(ctypes.Structure):
    _fields_ = [
        ("QueryInterface", _QUERY_INTERFACE),
        ("AddRef", _ADD_REF),
        ("Release", _RELEASE),
        ("DragEnter", _DRAG),
        ("DragOver", _DRAG),
        ("DragLeave", _DRAG_LEAVE),
        ("Drop", _DRAG),
    ]


class IDropTargetStruct(ctypes.Structure):
    _fields_ = [("lpVtbl", ctypes.POINTER(IDropTargetVtbl))]


_ole32 = None
_shell32 = None
_kernel32 = None
_cf_shell_idlist = 0
_cf_file_descriptor = 0
_ole_initialized = False
# COM хранит ссылку на IDropTarget между RegisterDragDrop и RevokeDragDrop;
# словарь не даёт Python-объекту исчезнуть вместе с окном.
_targets: dict[int, "ComFileDrop"] = {}


def _libraries():
    """Загрузить ole32/shell32 и один раз настроить сигнатуры и форматы."""
    global _ole32, _shell32, _kernel32, _cf_shell_idlist, _cf_file_descriptor
    if _ole32 is not None and _shell32 is not None:
        return _ole32, _shell32, _cf_shell_idlist, _cf_file_descriptor
    if os.name != "nt":
        raise ComDropError("COM DnD доступен только в Windows")

    ole32 = ctypes.windll.ole32  # type: ignore[attr-defined]
    shell32 = ctypes.windll.shell32  # type: ignore[attr-defined]
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    ole32.OleInitialize.argtypes = [ctypes.c_void_p]
    ole32.OleInitialize.restype = HRESULT
    ole32.OleUninitialize.argtypes = []
    ole32.OleUninitialize.restype = None
    ole32.RegisterDragDrop.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ole32.RegisterDragDrop.restype = HRESULT
    ole32.RevokeDragDrop.argtypes = [ctypes.c_void_p]
    ole32.RevokeDragDrop.restype = HRESULT
    ole32.ReleaseStgMedium.argtypes = [ctypes.POINTER(STGMEDIUM)]
    ole32.ReleaseStgMedium.restype = None
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t

    register_format = user32.RegisterClipboardFormatW
    register_format.argtypes = [ctypes.c_wchar_p]
    register_format.restype = ctypes.c_uint

    query = shell32.DragQueryFileW
    query.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_wchar_p,
        ctypes.c_uint,
    ]
    query.restype = ctypes.c_uint
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    shell32.SHGetPathFromIDListW.restype = ctypes.c_int

    _cf_shell_idlist = register_format("Shell IDList Array")
    _cf_file_descriptor = register_format("FileGroupDescriptorW")
    _ole32 = ole32
    _shell32 = shell32
    _kernel32 = kernel32
    return _ole32, _shell32, _cf_shell_idlist, _cf_file_descriptor


def initialize_ole() -> bool:
    """Инициализировать OLE до GDK, который вызывает только CoInitializeEx.

    Документация RegisterDragDrop требует, чтобы приложение с drag-and-drop
    вызывало именно OleInitialize. GDK Win32 делает CoInitializeEx при
    Gtk.init(); если OLE ещё не готов, RegisterDragDrop возвращает
    E_OUTOFMEMORY, и броски не доходят до нашего IDropTarget. Поэтому
    ``win_dnd.configure_dnd()`` вызывает эту функцию до Gtk.init().
    """
    global _ole_initialized
    if _ole_initialized:
        return True
    ole32, _shell32, _cf_shell, _cf_file = _libraries()
    result = ole32.OleInitialize(None)
    if result not in (S_OK, S_FALSE):
        raise ComDropError(f"OleInitialize вернул {result:#x}")
    _ole_initialized = True
    return True


def _vtable_address(pointer: int | ctypes.c_void_p) -> int:
    value = pointer.value if isinstance(pointer, ctypes.c_void_p) else pointer
    return ctypes.cast(ctypes.c_void_p(value), ctypes.POINTER(ctypes.c_void_p)).contents.value


def _com_call(pointer, index: int, restype, argtypes, *args):
    """Вызвать метод COM-интерфейса по индексу в vtable."""
    address = _vtable_address(pointer)
    if not address:
        raise ComDropError("пустая COM vtable")
    table = ctypes.cast(ctypes.c_void_p(address), ctypes.POINTER(ctypes.c_void_p))
    method = _FUNCTYPE(restype, ctypes.c_void_p, *argtypes)(table[index])
    value = pointer.value if isinstance(pointer, ctypes.c_void_p) else pointer
    return method(ctypes.c_void_p(value), *args)


def _trim_pidl(raw: bytes) -> bytes:
    """Отрезать хвост CIDA, оставив законченную цепочку SHITEMID."""
    result = bytearray()
    position = 0
    while position + 2 <= len(raw):
        size = struct.unpack_from("<H", raw, position)[0]
        if size == 0:
            result += raw[position : position + 2]
            break
        if size < 2 or position + size > len(raw):
            break
        result += raw[position : position + size]
        position += size
    return bytes(result)


def _combine_pidl(parent: bytes, child: bytes) -> bytes:
    """Соединить родительский PIDL и относительный в полный PIDL."""
    if not child:
        return b""
    if len(parent) < 2:
        return child
    if parent[-2:] == b"\x00\x00":
        parent = parent[:-2]
    return parent + child


def paths_from_shell_idlist(data: bytes, shell32=None) -> list[str]:
    """Достать пути из ``CFSTR_SHELLIDLIST`` (структура CIDA)."""
    if len(data) < 4:
        return []
    count = struct.unpack_from("<I", data, 0)[0]
    if not count or count > MAX_FILES or len(data) < 4 + (count + 1) * 4:
        return []
    if shell32 is None:
        shell32 = _libraries()[1]
    offsets = struct.unpack_from(f"<{count + 1}I", data, 4)

    def item(start: int) -> bytes:
        if start >= len(data):
            return b""
        end = min((value for value in offsets if value > start), default=len(data))
        return _trim_pidl(data[start:end])

    parent = item(offsets[0])
    paths: list[str] = []
    for index in range(1, count + 1):
        pidl = _combine_pidl(parent, item(offsets[index]))
        if not pidl:
            continue
        pidl_buffer = (ctypes.c_ubyte * len(pidl)).from_buffer_copy(pidl)
        path_buffer = ctypes.create_unicode_buffer(MAX_PATH_BUFFER)
        if shell32.SHGetPathFromIDListW(
            ctypes.cast(pidl_buffer, ctypes.c_void_p), path_buffer
        ):
            if path_buffer.value:
                paths.append(path_buffer.value)
    return paths


def paths_from_hdrop_handle(hdrop, shell32=None) -> list[str]:
    """Прочитать пути из ``CF_HDROP``, который уже лежит в STGMEDIUM."""
    if not hdrop:
        return []
    if shell32 is None:
        shell32 = _libraries()[1]
    count = shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
    if not count or count == 0xFFFFFFFF:
        return []
    paths: list[str] = []
    for index in range(min(count, MAX_FILES)):
        length = shell32.DragQueryFileW(hdrop, index, None, 0)
        if not length:
            continue
        buffer = ctypes.create_unicode_buffer(length + 1)
        received = shell32.DragQueryFileW(hdrop, index, buffer, len(buffer))
        if received and buffer.value:
            paths.append(buffer.value)
    return paths


def paths_from_file_descriptor(data: bytes) -> list[str]:
    """Прочитать имена из ``CFSTR_FILEDESCRIPTOR`` (FILEGROUPDESCRIPTORW)."""
    if len(data) < 4:
        return []
    count = struct.unpack_from("<I", data, 0)[0]
    if not count or count > MAX_FILES:
        return []
    paths: list[str] = []
    for index in range(count):
        start = 4 + index * FILEDESCRIPTORW_SIZE
        name_start = start + FILEDESCRIPTORW_NAME_OFFSET
        if name_start + FILEDESCRIPTORW_NAME_SIZE > len(data):
            break
        name = data[name_start : name_start + FILEDESCRIPTORW_NAME_SIZE]
        value = name.decode("utf-16-le", "replace").split("\x00", 1)[0]
        if value:
            paths.append(value)
    return paths


def _medium_bytes(stg: STGMEDIUM) -> bytes:
    """Прочитать HGLOBAL из STGMEDIUM.

    GlobalLock/GlobalSize/GlobalUnlock живут в kernel32, а не в ole32.
    Вызов ``ole32.GlobalSize`` заканчивается AttributeError, из-за чего
    Drop молча терял все пути из IDataObject.
    """
    if _kernel32 is None:
        _libraries()
    assert _kernel32 is not None
    handle = stg.u.hGlobal
    if not handle:
        return b""
    size = _kernel32.GlobalSize(handle)
    if not size:
        return b""
    pointer = _kernel32.GlobalLock(handle)
    if not pointer:
        return b""
    try:
        return ctypes.string_at(pointer, size)
    finally:
        _kernel32.GlobalUnlock(handle)


def _enum_data_formats(data_object) -> set[int]:
    """Спросить IDataObject, какие форматы он может отдать."""
    if not data_object:
        return set()
    enum = ctypes.c_void_p()
    try:
        result = _com_call(
            data_object,
            8,
            HRESULT,
            [DWORD, ctypes.POINTER(ctypes.c_void_p)],
            DATADIR_GET,
            ctypes.byref(enum),
        )
        if result != S_OK or not enum.value:
            return set()
        formats: set[int] = set()
        entries = (FORMATETC * 32)()
        fetched = ULONG(0)
        while True:
            # IEnumFORMATETC::Next — третий метод после IUnknown.
            result = _com_call(
                enum,
                3,
                HRESULT,
                [ULONG, ctypes.POINTER(FORMATETC), ctypes.POINTER(ULONG)],
                32,
                entries,
                ctypes.byref(fetched),
            )
            # IEnumFORMATETC::Next возвращает S_FALSE, когда форматов меньше,
            # чем запрошено (Проводник отдаёт 3–8, а мы просим 32). Такие
            # результаты нельзя выбрасывать: из-за этого DragEnter отвечал
            # DROPEFFECT_NONE, и Windows не вызывала Drop.
            if result not in (S_OK, S_FALSE) or not fetched.value:
                break
            formats.update(entries[index].cfFormat for index in range(fetched.value))
            if fetched.value < 32:
                break
        return formats
    except Exception:  # noqa: BLE001 - источник может быть чужим COM-объектом
        return set()
    finally:
        if enum.value:
            try:
                _com_call(enum, 2, ULONG, [])
            except Exception:  # noqa: BLE001
                pass


def _has_format(data_object, fmt: int) -> bool:
    entry = FORMATETC(fmt, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
    try:
        # IDataObject::QueryGetData — шестой метод, считая IUnknown.
        return (
            _com_call(
                data_object,
                5,
                HRESULT,
                [ctypes.POINTER(FORMATETC)],
                ctypes.byref(entry),
            )
            == S_OK
        )
    except Exception:  # noqa: BLE001
        return False


def _get_medium(data_object, fmt: int) -> STGMEDIUM | None:
    entry = FORMATETC(fmt, None, DVASPECT_CONTENT, -1, TYMED_HGLOBAL)
    medium = STGMEDIUM()
    try:
        # IDataObject::GetData — четвёртый метод, считая IUnknown.
        result = _com_call(
            data_object,
            3,
            HRESULT,
            [ctypes.POINTER(FORMATETC), ctypes.POINTER(STGMEDIUM)],
            ctypes.byref(entry),
            ctypes.byref(medium),
        )
    except Exception:  # noqa: BLE001
        return None
    if result != S_OK or medium.tymed != TYMED_HGLOBAL or not medium.u.hGlobal:
        return None
    return medium


def dropped_paths(data_object) -> list[str]:
    """Получить список путей из IDataObject броска."""
    _ole32, shell32, cf_shell, cf_file = _libraries()
    formats = _enum_data_formats(data_object)

    def available(fmt: int) -> bool:
        return fmt in formats or _has_format(data_object, fmt)

    for fmt in (cf_shell, CF_HDROP, cf_file):
        if not fmt or not available(fmt):
            continue
        medium = _get_medium(data_object, fmt)
        if medium is None:
            continue
        try:
            if fmt == cf_shell:
                paths = paths_from_shell_idlist(_medium_bytes(medium), shell32)
            elif fmt == CF_HDROP:
                paths = paths_from_hdrop_handle(medium.u.hGlobal, shell32)
            else:
                paths = paths_from_file_descriptor(_medium_bytes(medium))
        finally:
            _ole32.ReleaseStgMedium(ctypes.byref(medium))
        if paths:
            return paths
    return []


class ComFileDrop:
    """COM ``IDropTarget``, зарегистрированный на HWND окна верхнего уровня."""

    def __init__(self, callback: Callable[[list[str]], object]) -> None:
        self._callback = callback
        self._ref_count = 1
        self._hwnd: int | None = None
        self._installed = False
        self._vtbl = IDropTargetVtbl(
            _QUERY_INTERFACE(self._query_interface),
            _ADD_REF(self._add_ref),
            _RELEASE(self._release),
            _DRAG(self._drag_enter),
            _DRAG(self._drag_over),
            _DRAG_LEAVE(self._drag_leave),
            _DRAG(self._drop),
        )
        self._com = IDropTargetStruct(ctypes.pointer(self._vtbl))
        self._pointer = ctypes.cast(ctypes.byref(self._com), ctypes.c_void_p)

    @property
    def installed(self) -> bool:
        return self._installed

    @property
    def pointer(self) -> int:
        return int(self._pointer.value or 0)

    def _query_interface(self, _this, riid, ppv) -> int:
        identifier = ctypes.cast(riid, ctypes.POINTER(GUID)).contents
        output = ctypes.cast(ppv, ctypes.POINTER(ctypes.c_void_p))
        if bytes(identifier) in (bytes(IID_IUNKNOWN), bytes(IID_IDROPTARGET)):
            output[0] = self._pointer
            self._add_ref()
            return S_OK
        output[0] = None
        return E_NOINTERFACE

    def _add_ref(self, _this=None) -> int:
        self._ref_count += 1
        return self._ref_count

    def _release(self, _this=None) -> int:
        self._ref_count = max(0, self._ref_count - 1)
        return self._ref_count

    def _effect_for(self, data_object) -> int:
        """Разрешить бросок: Windows спросит эффект до вызова Drop.

        Проверять форматы здесь ненадёжно: источник может не ответить на
        QueryGetData до начала броска. Если IDataObject есть — принимаем
        копирование, а реальные пути разбираем уже в ``Drop``.
        """
        return DROPEFFECT_COPY if data_object else DROPEFFECT_NONE

    def _drag_enter(self, _this, data_object, _keys, _point, effect) -> int:
        effect[0] = self._effect_for(data_object)
        return S_OK

    def _drag_over(self, _this, data_object, _keys, _point, effect) -> int:
        effect[0] = self._effect_for(data_object)
        return S_OK

    def _drag_leave(self, _this) -> int:
        return S_OK

    def _drop(self, _this, data_object, _keys, _point, effect) -> int:
        try:
            paths = dropped_paths(data_object)
        except Exception:  # noqa: BLE001 - бросок не должен ломать Проводник
            paths = []
        effect[0] = DROPEFFECT_COPY if paths else DROPEFFECT_NONE
        if paths:
            self._callback(paths)
        return S_OK

    def install(self, hwnd: int) -> bool:
        """Зарегистрировать IDropTarget на HWND. Возвращает True при успехе."""
        if not hwnd or self._installed or hwnd in _targets:
            return False
        initialize_ole()
        ole32, _shell32, _cf_shell, _cf_file = _libraries()
        result = ole32.RegisterDragDrop(ctypes.c_void_p(hwnd), self._pointer)
        if result != S_OK:
            raise ComDropError(f"RegisterDragDrop вернул {result:#x}")
        _targets[hwnd] = self
        self._hwnd = hwnd
        self._installed = True
        return True

    def uninstall(self) -> None:
        """Снять регистрацию и освободить поток COM."""
        if not self._installed or self._hwnd is None:
            return
        ole32, _shell32, _cf_shell, _cf_file = _libraries()
        hwnd = self._hwnd
        if ole32.RevokeDragDrop(ctypes.c_void_p(hwnd)) != S_OK:
            # COM всё ещё держит указатель: оставляем объект живым и зарегистрированным.
            return
        self._ref_count = max(0, self._ref_count - 1)
        # OLE инициализирован один раз до Gtk.init() и живёт до выхода процесса.
        _targets.pop(hwnd, None)
        self._installed = False
        self._hwnd = None


def install_for_window(
    callback: Callable[[list[str]], object], gdk_window
) -> ComFileDrop | None:
    """Зарегистрировать IDropTarget на HWND, принадлежащем ``GdkWindow``."""
    if os.name != "nt" or gdk_window is None:
        return None
    from . import win_theme

    hwnd = win_theme.gdk_window_handle(gdk_window)
    if not hwnd:
        return None
    drop = ComFileDrop(callback)
    try:
        drop.install(hwnd)
    except ComDropError:
        return None
    return drop if drop.installed else None
