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
    """Все записи IMAGE_RESOURCE_DIRECTORY: (ID/имя, смещение записи)."""
    count = u16(data, directory + 12) + u16(data, directory + 14)
    return [
        (u32(data, directory + 16 + index * 8), directory + 16 + index * 8)
        for index in range(count)
    ]


def subdirectory(data: bytes, root: int, directory: int, wanted: int) -> int:
    """Поддиректория следующего уровня по числовому ID.

    Смещения в записях resources считаются от начала блока ресурсов, а не
    от текущей директории. Старший бит OffsetToData означает поддиректорию.
    """
    for value, offset in entries(data, directory):
        if value & 0x80000000 == 0 and value == wanted:
            relative = u32(data, offset + 4) & 0x7FFFFFFF
            if relative:
                return root + relative
    raise RuntimeError(f"ресурс с ID {wanted} не найден")


def first_subdirectory(data: bytes, root: int, directory: int) -> int:
    """Первая поддиректория (уровень языка) внутри директории имени."""
    for _value, offset in entries(data, directory):
        relative = u32(data, offset + 4)
        if relative & 0x80000000 and (relative & 0x7FFFFFFF):
            return root + (relative & 0x7FFFFFFF)
    raise RuntimeError("у ресурса нет языковой директории")


def data_entry(data: bytes, root: int, directory: int) -> tuple[int, int]:
    """RVA и размер данных из языковой директории (уровень 3)."""
    for _value, offset in entries(data, directory):
        relative = u32(data, offset + 4)
        if relative & 0x80000000 == 0 and relative:
            entry = root + relative
            return u32(data, entry), u32(data, entry + 4)
    raise RuntimeError("у ресурса нет языковых данных")


def resource(data: bytes, root: int, type_id: int, name_id: int = 1) -> tuple[int, int]:
    """Найти ресурс type_id/name_id: тип → имя → язык → данные."""
    node = subdirectory(data, root, root, type_id)
    node = subdirectory(data, root, node, name_id)
    node = first_subdirectory(data, root, node)
    return data_entry(data, root, node)


def check(exe: Path) -> str:
    data = exe.read_bytes()
    pe = u32(data, 0x3C)
    if data[pe : pe + 4] != b"PE\0\0":
        raise RuntimeError("не PE-файл")
    optional = pe + 24
    magic = u16(data, optional)
    if magic == 0x10B:
        directories = optional + 96
        count_field = optional + 92
    elif magic == 0x20B:
        directories = optional + 112
        count_field = optional + 108
    else:
        raise RuntimeError(f"неизвестный PE-заголовок 0x{magic:X}")
    # IMAGE_DIRECTORY_ENTRY_RESOURCE — третья запись data directories.
    if u32(data, count_field) <= 2:
        raise RuntimeError("в PE нет таблицы ресурсов")
    resource_entry = directories + 2 * 8
    resource_rva = u32(data, resource_entry)
    resource_size = u32(data, resource_entry + 4)
    if not resource_rva or not resource_size:
        raise RuntimeError("в PE нет ресурсов")

    root = rva_to_offset(data, resource_rva)

    group_rva, _ = resource(data, root, RT_GROUP_ICON)
    group_offset = rva_to_offset(data, group_rva)
    count = u16(data, group_offset + 4)
    if count < 1:
        raise RuntimeError("GRPICONDIR не содержит иконок")
    # GRPICONDIRENTRY: 4 байта размера/цветов, wPlanes, wBitCount,
    # dwBytesInRes и nID — 12-й байт записи. Сама запись идёт после
    # 6-байтового GRPICONDIR.
    icon_id = u16(data, group_offset + 6 + 12)

    _rva, size = resource(data, root, RT_ICON, icon_id)
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
