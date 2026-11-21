#!/usr/bin/env python3
"""Проверить, что в Eve's Text.exe встроена иконка (PE-ресурсы RT_GROUP_ICON).

Запускается в Windows CI и локально:

    python3 windows/check_icon.py "windows/dist/Eve's Text/Eve's Text.exe"

Код возврата 0 — иконка есть, 1 — нет. Внешние библиотеки не нужны.
"""

from __future__ import annotations

import struct
import sys
from pathlib import Path

RT_ICON = 3
RT_GROUP_ICON = 14


def u16(data: bytes, offset: int) -> int:
    return struct.unpack_from("<H", data, offset)[0]


def u32(data: bytes, offset: int) -> int:
    return struct.unpack_from("<I", data, offset)[0]


def rva_to_offset(data: bytes, rva: int) -> int:
    """Адрес RVA в смещение файла по таблице секций PE."""
    pe = u32(data, 0x3C)
    if data[pe : pe + 4] != b"PE\0\0":
        raise RuntimeError("не PE-файл")
    coff = pe + 4
    sections = u16(data, coff + 2)
    optional_size = u16(data, coff + 16)
    table = coff + 20 + optional_size
    for index in range(sections):
        entry = table + index * 40
        virtual_size = u32(data, entry + 8)
        virtual_address = u32(data, entry + 12)
        raw_size = u32(data, entry + 16)
        raw_address = u32(data, entry + 20)
        if virtual_address <= rva < virtual_address + max(virtual_size, raw_size):
            return raw_address + (rva - virtual_address)
    raise RuntimeError(f"RVA 0x{rva:X} не попал ни в одну секцию")


def entries(data: bytes, directory: int) -> list[tuple[int, int]]:
    """Все записи директории: (identity, offset записи)."""
    count = u16(data, directory + 12) + u16(data, directory + 14)
    return [
        (u32(data, directory + 16 + index * 8), directory + 16 + index * 8)
        for index in range(count)
    ]


def child_directory(data: bytes, root: int, directory: int, wanted: int) -> int:
    """Поддиректория по числовому ID. Смещения в PE — от начала ресурсов."""
    for value, offset in entries(data, directory):
        if value & 0x80000000 == 0 and value == wanted:
            relative = u32(data, offset + 4) & 0x7FFFFFFF
            if relative:
                return root + relative
    raise RuntimeError(f"ресурс с ID {wanted} не найден")


def resource_data(data: bytes, root: int, name_directory: int) -> tuple[int, int]:
    """RVA и размер данных для первой языковой записи ресурса."""
    for _value, offset in entries(data, name_directory):
        if u32(data, offset) & 0x80000000:
            target = root + (u32(data, offset) & 0x7FFFFFFF)
            return u32(data, target), u32(data, target + 4)
    raise RuntimeError("у ресурса нет языковых данных")


def language_directory(data: bytes, root: int, name_directory: int) -> int:
    for _value, offset in entries(data, name_directory):
        relative = u32(data, offset + 4) & 0x7FFFFFFF
        if relative:
            return root + relative
    raise RuntimeError("у ресурса нет языковой директории")


def check(exe: Path) -> str:
    data = exe.read_bytes()
    pe = u32(data, 0x3C)
    if data[pe : pe + 4] != b"PE\0\0":
        raise RuntimeError("не PE-файл")
    optional = pe + 24
    magic = u16(data, optional)
    if magic == 0x10B:
        resource_entry = optional + 96
    elif magic == 0x20B:
        resource_entry = optional + 112
    else:
        raise RuntimeError(f"неизвестный PE-заголовок 0x{magic:X}")
    resource_rva = u32(data, resource_entry)
    resource_size = u32(data, resource_entry + 4)
    if not resource_rva or not resource_size:
        raise RuntimeError("в PE нет ресурсов")

    root = rva_to_offset(data, resource_rva)

    group_type = child_directory(data, root, root, RT_GROUP_ICON)
    group_name = child_directory(data, root, group_type, 1)
    group_language = language_directory(data, root, group_name)
    group_rva, _ = resource_data(data, root, group_name)
    group_offset = rva_to_offset(data, group_rva)
    count = u16(data, group_offset + 4)
    if count < 1:
        raise RuntimeError("GRPICONDIR не содержит иконок")
    # GRPICONDIRENTRY: 6 байт GRPICONDIR, затем 14 байт записи, из них
    # nID (идентификатор RT_ICON) — последние 2 байта первой записи.
    icon_id = u16(data, group_offset + 6 + 14)

    icon_type = child_directory(data, root, root, RT_ICON)
    icon_name = child_directory(data, root, icon_type, icon_id)
    icon_language = language_directory(data, root, icon_name)
    _rva, size = resource_data(data, root, icon_name)
    if size < 16:
        raise RuntimeError(f"RT_ICON {icon_id} пуст или слишком мал")
    return f"встроено {count} иконок, основная RT_ICON {icon_id}, {size} байт"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 1
    exe = Path(sys.argv[1])
    try:
        detail = check(exe)
    except (OSError, ValueError, struct.error, RuntimeError) as exc:
        print(f"[ОШИБКА] {exe}: {exc}", file=sys.stderr)
        return 1
    print(f"[OK] {exe}: {detail}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
