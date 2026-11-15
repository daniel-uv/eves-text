"""Проверка орфографии: Gspell.Checker + волнистое подчёркивание текстовым тегом.

Gspell.TextView (готовая обёртка) здесь намеренно не используется: она
конфликтует с обработкой клавиатуры и «съедает» ввод. Свой слой на тегах
заодно станет основой для подсветки замечаний ИИ.
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

try:
    gi.require_version("Gspell", "1")
    from gi.repository import Gspell  # noqa: E402
except (ValueError, ImportError):  # pragma: no cover
    Gspell = None

WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[-'][0-9A-Za-zА-Яа-яЁё]+)*")
TAG_NAME = "spell-error"
DEFAULT_LANGUAGE = "ru_RU"
SCAN_DELAY_MS = 350


def _byte_len(word: str) -> int:
    """Enchant ожидает длину в байтах UTF-8, а не в символах."""
    return len(word.encode("utf-8"))


class SpellCheck:
    """Проверка орфографии для GtkSource.Buffer."""

    def __init__(self, buffer: Gtk.TextBuffer, view: Gtk.TextView) -> None:
        self.buffer = buffer
        self.view = view
        self.checker = self._make_checker()
        self.enabled = self.checker is not None
        self._timeout = 0

        self.tag = buffer.create_tag(TAG_NAME, underline=Pango.Underline.ERROR)
        if Gtk.TextTag.find_property("underline-rgba") is not None:
            self.tag.set_property("underline-rgba", self._error_color())

        buffer.connect("changed", lambda *_: self.schedule())

    # ------------------------------------------------------------- создание

    @staticmethod
    def _make_checker():
        if Gspell is None:
            return None
        language = Gspell.Language.lookup(DEFAULT_LANGUAGE) or Gspell.Language.get_default()
        if language is None:
            return None
        try:
            return Gspell.Checker.new(language)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _error_color():
        from gi.repository import Gdk

        color = Gdk.RGBA()
        color.parse("#d64545")
        return color

    @property
    def available(self) -> bool:
        return self.checker is not None

    @property
    def language_code(self) -> str:
        return self.checker.get_language().get_code() if self.checker else "—"

    # ------------------------------------------------------------ включение

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled and self.available
        if self.enabled:
            self.refresh()
        else:
            self.clear()

    def clear(self) -> None:
        start, end = self.buffer.get_bounds()
        self.buffer.remove_tag(self.tag, start, end)

    def schedule(self) -> None:
        if not self.enabled or self._timeout:
            return
        self._timeout = GLib.timeout_add(SCAN_DELAY_MS, self._on_timeout)

    def _on_timeout(self) -> bool:
        self._timeout = 0
        if self.enabled:
            self.refresh()
        return False

    # -------------------------------------------------------------- проверка

    def refresh(self) -> None:
        start, end = self.buffer.get_bounds()
        self.buffer.remove_tag(self.tag, start, end)
        text = self.buffer.get_text(start, end, True)
        if not text:
            return
        for match in WORD_RE.finditer(text):
            word = match.group()
            if self.is_correct(word):
                continue
            word_start = self.buffer.get_iter_at_offset(match.start())
            word_end = self.buffer.get_iter_at_offset(match.end())
            self.buffer.apply_tag(self.tag, word_start, word_end)

    def is_correct(self, word: str) -> bool:
        if self.checker is None or len(word) < 2:
            return True
        if any(ch.isdigit() for ch in word):
            return True
        try:
            return bool(self.checker.check_word(word, _byte_len(word)))
        except Exception:  # noqa: BLE001
            return True

    # ------------------------------------------------------------ подсказки

    def word_at(self, position: int) -> tuple[str, int, int] | None:
        start, end = self.buffer.get_bounds()
        text = self.buffer.get_text(start, end, True)
        for match in WORD_RE.finditer(text):
            if match.start() <= position <= match.end():
                return match.group(), match.start(), match.end()
        return None

    def suggestions(self, word: str, limit: int = 6) -> list[str]:
        if self.checker is None or self.is_correct(word):
            return []
        try:
            found = list(self.checker.get_suggestions(word, _byte_len(word)))
        except Exception:  # noqa: BLE001
            return []
        return found[:limit]

    def add_to_session(self, word: str) -> None:
        if self.checker is None:
            return
        try:
            self.checker.add_word_to_session(word, _byte_len(word))
        except Exception:  # noqa: BLE001
            pass
        self.refresh()

    def replace(self, start: int, end: int, replacement: str) -> None:
        start_iter = self.buffer.get_iter_at_offset(start)
        end_iter = self.buffer.get_iter_at_offset(end)
        self.buffer.begin_user_action()
        self.buffer.delete(start_iter, end_iter)
        self.buffer.insert(start_iter, replacement)
        self.buffer.end_user_action()
        self.refresh()
