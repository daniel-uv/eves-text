"""Работа с файлами: чтение/запись и статистика текста.

Отдельный модуль без зависимостей от GTK — его же будет использовать
модуль проверки орфографии и ИИ-проверки на следующих шагах.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

# Порядок важен: utf-8-sig корректно читает и обычный UTF-8, и файл с BOM.
_ENCODINGS = ("utf-8-sig", "cp1251")


def read_text(path: str | os.PathLike) -> tuple[str, str]:
    """Прочитать файл, вернув (текст, название кодировки).

    Пробуем UTF-8, затем CP1251 (частая кодировка старых русских файлов).
    Если не вышло — читаем как UTF-8 с заменой «битых» символов.
    """
    data = Path(path).read_bytes()
    for encoding in _ENCODINGS:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        return text, "UTF-8" if encoding.startswith("utf") else "CP1251"
    return data.decode("utf-8", errors="replace"), "UTF-8 (с заменами)"


def write_text(path: str | os.PathLike, text: str) -> None:
    """Атомарно записать текст в UTF-8.

    Пишем во временный файл рядом и переименовываем: если запись прервётся,
    исходный файл останется целым.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def text_stats(text: str) -> tuple[int, int, int]:
    """Вернуть (слов, символов, символов без пробелов)."""
    words = len(text.split())
    chars = len(text)
    chars_no_spaces = sum(1 for ch in text if not ch.isspace())
    return words, chars, chars_no_spaces


# ---------------------------------------------------------------------------
# Оформление: RTF
#
# Обычные .txt остаются чистым текстом. Как только в документе появляется
# начертание или размер для части текста, файл сохраняется как RTF — это
# один файл, который понимают Word, Writer и Windows Notepad. Поддерживается
# небольшое подмножество RTF: \b, \i, \ul, \fs, выравнивание и Unicode-escape-ы.

# (начало, конец, атрибуты) — атрибуты: bold, italic, underline, size, align.
Span = tuple[int, int, dict[str, object]]
_ALIGN_CONTROLS = {"left": r"\ql", "center": r"\qc", "right": r"\qr", "justify": r"\qj"}
_CONTROL_RE = re.compile(r"([a-zA-Z]+)(-?\d+)?")
_DEFAULT_ATTRS: dict[str, object] = {
    "bold": False,
    "italic": False,
    "underline": False,
    "size": None,
    "align": None,
}
# Служебные группы RTF: их содержимое не является текстом документа.
_IGNORED_DESTINATIONS = {
    "fonttbl",
    "colortbl",
    "stylesheet",
    "listtable",
    "listoverridetable",
    "generator",
    "info",
    "pict",
    "object",
    "header",
    "footer",
    "headerl",
    "headerr",
    "footerl",
    "footerr",
    "field",
    "themedata",
    "colorschememapping",
    "datastore",
    "xmlnstbl",
}


def is_rtf(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() == ".rtf"


_RTF_SIGNATURES = (b"{\\rtf", b"{\\urtf")  # \urtf — RTF 1.9 из Word 2007+
_UTF8_BOM = b"\xef\xbb\xbf"
# Кодировки для \'hh из RTF-файлов, пришедших из Word/Writer.
_RTF_CODECS = {
    1250: "cp1250",
    1251: "cp1251",
    1252: "cp1252",
    866: "cp866",
    65001: "latin-1",  # один байт не образует UTF-8-символ
}


def _decode_rtf(raw: bytes) -> str | None:
    """Вернуть исходник RTF, если содержимое файла — RTF, иначе None.

    Полагаться только на расширение нельзя: BOM, UTF-16 или оформление,
    сохранённое в .txt, заставляют редактор показать сырой код вместо текста.
    """
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        try:
            source = raw.decode("utf-16")
        except UnicodeDecodeError:
            return None
        stripped = source.lstrip()
        return source if stripped.startswith(("{\\rtf", "{\\urtf")) else None

    payload = raw[len(_UTF8_BOM) :] if raw.startswith(_UTF8_BOM) else raw
    if not payload.lstrip().startswith(_RTF_SIGNATURES):
        return None
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        return payload.decode("cp1251", errors="replace")


def read_document(path: str | os.PathLike) -> tuple[str, str, list[Span]]:
    """Прочитать документ вместе с оформлением.

    Возвращает (текст, кодировка, оформление). Для обычного текста список
    оформления пуст; для RTF он содержит диапазоны с начертанием/размером/выравниванием.
    """
    file = Path(path)
    source = _decode_rtf(file.read_bytes())
    if source is not None:
        text, spans = _parse_rtf(source)
        return text, "RTF", spans
    text, encoding = read_text(file)
    return text, encoding, []


def write_document(
    path: str | os.PathLike,
    text: str,
    spans: list[Span] | None = None,
    default_size: int = 12,
) -> None:
    """Записать документ. С оформлением — RTF, без оформления — обычный текст."""
    spans = spans or []
    file = Path(path)
    if is_rtf(file) or spans:
        payload = _build_rtf(text, spans, default_size)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(file.parent), prefix=f".{file.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
            os.replace(tmp_name, file)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    else:
        write_text(file, text)


def _parse_rtf(source: str) -> tuple[str, list[Span]]:
    """Разобрать поддерживаемое подмножество RTF в текст и диапазоны атрибутов."""
    state: dict[str, object] = dict(_DEFAULT_ATTRS)
    stack: list[tuple[dict[str, object], int, int, int]] = []
    chars: list[str] = []
    attrs: list[dict[str, object]] = []
    skip = 0
    uc = 1
    codepage = 1251
    ignored_level: int | None = None
    i = 0
    size = len(source)

    def append(value: str) -> None:
        if ignored_level is None:
            chars.append(value)
            attrs.append(state.copy())

    while i < size:
        ch = source[i]

        # Резервное представление после \uN: \'hh считается одной единицей.
        # Проверяем его до разбора команд, иначе hex-цифры утекут в текст.
        if skip > 0 and ignored_level is None:
            if ch == "\\" and i + 1 < size and source[i + 1] == "'":
                i += 4 if i + 3 < size else size - i
            else:
                i += 1
            skip -= 1
            continue

        if ch == "{":
            stack.append((state.copy(), skip, uc, codepage))
            state = state.copy()
            i += 1
            continue
        if ch == "}":
            if stack:
                state, skip, uc, codepage = stack.pop()
            if ignored_level is not None and len(stack) < ignored_level:
                ignored_level = None
            i += 1
            continue
        if ch == "\\":
            i += 1
            if i >= size:
                break
            match = _CONTROL_RE.match(source, i)
            if match:
                word = match.group(1)
                raw_param = match.group(2)
                param = int(raw_param) if raw_param is not None else None
                i = match.end()
                if i < size and source[i] == " ":
                    i += 1
                if word in _IGNORED_DESTINATIONS:
                    ignored_level = max(1, len(stack))
                elif word == "uc":
                    uc = param or 0
                elif word == "ansicpg" and param is not None:
                    codepage = param
                elif word == "u" and param is not None:
                    code = param & 0xFFFF
                    if code >= 0x8000:
                        code -= 0x10000
                    append(chr(code))
                    skip = uc
                elif word == "b":
                    state["bold"] = (param if param is not None else 1) != 0
                elif word == "i":
                    state["italic"] = (param if param is not None else 1) != 0
                elif word == "ul":
                    state["underline"] = (param if param is not None else 1) != 0
                elif word == "fs" and param is not None:
                    state["size"] = max(1, round(param / 2))
                elif word in _ALIGN_CONTROLS.values():
                    state["align"] = next(
                        key for key, value in _ALIGN_CONTROLS.items() if value == word
                    )
                elif word == "pard":
                    state["align"] = None
                elif word == "plain":
                    state = dict(_DEFAULT_ATTRS)
                elif word in ("par", "line"):
                    append("\n")
                elif word == "tab":
                    append("\t")
                continue
            # control symbol: \*, \\, \{, \}, \', \~
            symbol = source[i]
            if symbol == "*":
                # {\*...} — служебная группа (таблица шрифтов, цветов,
                # генератор и т.п.). Её содержимое не является текстом.
                ignored_level = max(1, len(stack))
            elif symbol == "'" and i + 2 < size:
                try:
                    byte = int(source[i + 1 : i + 3], 16)
                    append(
                        bytes((byte,)).decode(
                            _RTF_CODECS.get(codepage, "cp1251"), errors="replace"
                        )
                    )
                except ValueError:
                    pass
                i += 3
                continue
            elif ignored_level is None and symbol in "\\{} ~":
                # "\ " — явный пробел, "\~" — неразрывный пробел.
                append("\u00a0" if symbol == "~" else symbol)
            i += 1
            continue
        if ignored_level is not None or ch in "\r\n":
            i += 1
            continue
        append(ch)
        i += 1

    text = "".join(chars)
    spans: list[Span] = []
    if not attrs:
        return text, spans
    start = 0
    for index in range(1, len(attrs) + 1):
        if index == len(attrs) or attrs[index] != attrs[start]:
            value = attrs[start]
            if _is_meaningful(value):
                spans.append((start, index, dict(value)))
            start = index
    return text, spans


def has_visual_formatting(attrs: dict[str, object]) -> bool:
    """Есть ли в атрибутах оформление, которое нельзя потерять в .txt."""
    return bool(
        attrs.get("bold")
        or attrs.get("italic")
        or attrs.get("underline")
        or attrs.get("size")
        or attrs.get("align") not in (None, "left", "")
    )


# Обратная совместимость с внутренним названием, использованным в window.py.
_is_meaningful = has_visual_formatting


def _escape_rtf(value: str) -> str:
    out: list[str] = []
    for ch in value:
        if ch == "\n":
            # В RTF перевод строки в исходнике — пробел; \n как управляющее
            # слово не существует и мешает другим редакторам.
            out.append("\\par\n")
        elif ch in "\\{}":
            out.append("\\" + ch)
        elif ch == " ":
            out.append(r"\ ")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            code = ord(ch)
            if code > 0x7FFF:
                code -= 0x10000
            out.append(f"\\u{code}?")
    return "".join(out)


def _build_rtf(text: str, spans: list[Span], default_size: int) -> bytes:
    parts = [
        r"{\rtf1\ansi\ansicpg65001\deff0",
        r"{\fonttbl{\f0\fmodern Consolas;}}",
        r"\uc1",
    ]
    size_default = int(default_size)
    default: dict[str, object] = {
        "bold": False,
        "italic": False,
        "underline": False,
        "size": size_default,
        "align": "left",
    }
    current: dict[str, object] = dict(default)
    parts.append(f"\\fs{size_default * 2}")

    def set_format(target: dict[str, object]) -> None:
        bold = bool(target["bold"])
        size = int(target["size"] or size_default)
        align = str(target["align"] or "left")
        if bold != current["bold"]:
            parts.append(r"\b" if bold else r"\b0")
            current["bold"] = bold
        italic = bool(target["italic"])
        if italic != current["italic"]:
            parts.append(r"\i" if italic else r"\i0")
            current["italic"] = italic
        underline = bool(target["underline"])
        if underline != current["underline"]:
            parts.append(r"\ul" if underline else r"\ul0")
            current["underline"] = underline
        if size != current["size"]:
            parts.append(f"\\fs{size * 2}")
            current["size"] = size
        if align != current["align"]:
            parts.append(_ALIGN_CONTROLS.get(align, r"\ql"))
            current["align"] = align

    position = 0
    for start, end, attrs in sorted(spans, key=lambda item: item[0]):
        start = max(0, min(int(start), len(text)))
        end = max(start, min(int(end), len(text)))
        if start > position:
            # Текст между размеченными кусками не должен наследовать оформление.
            set_format(default)
            parts.append(_escape_rtf(text[position:start]))
            position = start
        target = {
            "bold": bool(attrs.get("bold")),
            "italic": bool(attrs.get("italic")),
            "underline": bool(attrs.get("underline")),
            "size": attrs.get("size") or size_default,
            "align": attrs.get("align") or "left",
        }
        set_format(target)
        parts.append(_escape_rtf(text[start:end]))
        position = end
    if position < len(text):
        set_format(default)
        parts.append(_escape_rtf(text[position:]))
    parts.append("}")
    return "".join(parts).encode("ascii")
