"""Главное окно редактора."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk, GtkSource, Pango  # noqa: E402

from . import ai_check, document  # noqa: E402
from .spellcheck import SpellCheck  # noqa: E402

APP_NAME = "Eve's Text"

# Ключ выравнивания -> свойство текстового тега.
ALIGNMENTS: tuple[tuple[str, str, Gtk.Justification, str], ...] = (
    ("left", "format-justify-left", Gtk.Justification.LEFT, "По левому краю"),
    ("center", "format-justify-center", Gtk.Justification.CENTER, "По центру"),
    ("right", "format-justify-right", Gtk.Justification.RIGHT, "По правому краю"),
    ("justify", "format-justify-fill", Gtk.Justification.FILL, "По ширине"),
)

DEFAULT_FONT_SIZE = 12


def default_font_family() -> str:
    """Моноширинный шрифт, который точно есть в системе."""
    if sys.platform == "win32":
        return "Consolas"
    return "Monospace"

# Поля вокруг текста внутри редактора (в пикселях).
TEXT_MARGIN_X = 64
TEXT_MARGIN_Y = 24


def _fmt(number: int) -> str:
    """1234 -> '1 234' (узкий неразрывный пробел)."""
    return f"{number:,}".replace(",", "\u202f")


def _fmt_bytes(value: int) -> str:
    """Байты -> '4,4 ГБ' для прогресса скачивания модели."""
    size = float(max(value, 0))
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if size < 1024 or unit == "ГБ":
            if unit == "Б":
                return f"{size:.0f} {unit}"
            return f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return f"{size:.1f} ГБ"


# Статусы Ollama при скачивании модели -> подписи для человека.
PULL_STATUSES = {
    "pulling manifest": "Получаю описание модели…",
    "verifying sha256 digest": "Проверяю контрольную сумму…",
    "writing manifest": "Завершаю установку…",
    "removing any unused layers": "Удаляю неиспользуемые слои…",
    "success": "Готово",
}


def pull_status_text(status: str) -> str:
    """Переводит статус /api/pull на русский."""
    if status in PULL_STATUSES:
        return PULL_STATUSES[status]
    if status.startswith("pulling "):
        return "Скачиваю модель…"
    if status.startswith("verifying "):
        return "Проверяю контрольную сумму…"
    return status or "Скачиваю модель…"


def app_icon_name() -> str:
    """Иконка приложения, если она установлена, иначе системная заглушка."""
    theme = Gtk.IconTheme.get_default()
    if theme is not None and theme.has_icon("eves-text"):
        return "eves-text"
    return "accessories-text-editor"


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)

        self.path: Path | None = None
        self.encoding = "UTF-8"
        self._counts_timeout = 0
        self._counts_cache = (-1, -1, -1)
        self._ai_busy = False
        self._ai_enabled = True
        self._ai_unloading = False
        self._ai_pulling = False
        self._ai_pull_cancel: threading.Event | None = None
        self._ai_pull_dialog = None
        self._ai_pull_progress = None
        self._ai_pull_title = None
        self._ai_pull_detail = None
        self._ai_pull_status = ""
        self._ai_pull_last_update = 0.0
        self._message_timeout = 0

        self.set_default_size(1000, 700)
        self._icon_name = app_icon_name()
        self.set_icon_name(self._icon_name)

        self._build_view()
        self._build_ui()
        self._install_actions(application)
        self._connect_signals()
        self._install_focus_handling()
        self._apply_style_scheme()
        self._update_title()
        self._update_status()

    # ------------------------------------------------------------------ UI

    def _build_view(self) -> None:
        self.buffer = GtkSource.Buffer()
        self.buffer.set_highlight_syntax(False)
        font = Pango.FontDescription.from_string(
            f"{default_font_family()} {DEFAULT_FONT_SIZE}"
        )

        self.view = GtkSource.View.new_with_buffer(self.buffer)
        self.view.modify_font(font)
        # Номера строк по умолчанию выключены (включаются в меню «Вид»).
        self.view.set_show_line_numbers(False)
        self.view.set_highlight_current_line(True)
        self.view.set_auto_indent(True)
        self.view.set_tab_width(4)
        # Настоящая табуляция: Tab вставляет символ \t, а не четыре пробела.
        self.view.set_insert_spaces_instead_of_tabs(False)
        self.view.set_accepts_tab(True)
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.view.set_right_margin_position(80)
        self.view.set_show_right_margin(False)
        # Поля вокруг текста: документ не прилипает к краям окна,
        # текстовый блок стоит по центру.
        self.view.set_left_margin(TEXT_MARGIN_X)
        self.view.set_right_margin(TEXT_MARGIN_X)
        self.view.set_top_margin(TEXT_MARGIN_Y)
        self.view.set_bottom_margin(TEXT_MARGIN_Y)
        try:
            self.view.set_smart_home_end(GtkSource.SmartHomeEndType.AFTER_FIRST_NON_WHITESPACE)
        except AttributeError:
            pass

        # Теги выравнивания абзацев (действуют на выделенные строки
        # или на строку с курсором).
        self._align_tags = {
            key: self.buffer.create_tag(f"alignment:{key}", justification=alignment)
            for key, _icon, alignment, _tip in ALIGNMENTS
        }
        self._current_align = "left"
        self._align_guard = False

        # Проверка орфографии делается своим слоем на текстовых тегах.
        # Gspell.TextView здесь не используется: он ломает обработку клавиатуры.
        self.spellcheck = SpellCheck(self.buffer, self.view)
        self._popup_iter = None

    def _build_ui(self) -> None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        box.pack_start(self._build_menubar(), False, False, 0)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.add(self.view)
        box.pack_start(scrolled, True, True, 0)

        box.pack_start(self._build_bottom_bar(), False, False, 0)
        box.pack_start(self._build_statusbar(), False, False, 0)

    def _build_bottom_bar(self) -> Gtk.Box:
        # Внешняя полоса занимает всю ширину окна и несёт рамку и фон.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.get_style_context().add_class("bottom-bar")

        # Содержимое панели стоит по центру полосы.
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_halign(Gtk.Align.CENTER)
        bar.pack_start(content, True, True, 0)

        content.pack_start(Gtk.Label(label="Шрифт:"), False, False, 0)

        self.font_smaller = self._tool_button("zoom-out-symbolic", "Уменьшить шрифт")
        self.font_smaller.connect("clicked", lambda *_: self._change_font_size(-1))
        content.pack_start(self.font_smaller, False, False, 0)

        self.font_size_spin = Gtk.SpinButton.new_with_range(8, 48, 1)
        self.font_size_spin.set_value(DEFAULT_FONT_SIZE)
        self.font_size_spin.set_numeric(True)
        self.font_size_spin.set_width_chars(3)
        self.font_size_spin.set_tooltip_text("Размер шрифта")
        self.font_size_spin.connect("value-changed", self._on_font_size_changed)
        content.pack_start(self.font_size_spin, False, False, 0)

        self.font_larger = self._tool_button("zoom-in-symbolic", "Увеличить шрифт")
        self.font_larger.connect("clicked", lambda *_: self._change_font_size(1))
        content.pack_start(self.font_larger, False, False, 0)

        content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4
        )
        content.pack_start(Gtk.Label(label="Выравнивание:"), False, False, 0)

        self.align_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, icon, _alignment, tooltip in ALIGNMENTS:
            button = Gtk.ToggleButton()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_image(
                Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR)
            )
            button.set_tooltip_text(tooltip)
            button.set_active(key == "left")
            button.connect("toggled", self._on_alignment_toggled, key)
            content.pack_start(button, False, False, 0)
            self.align_buttons[key] = button

        content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4
        )

        self.ai_button = Gtk.Button(label="Выключить ИИ")
        self.ai_button.set_relief(Gtk.ReliefStyle.NONE)
        self.ai_button.connect("clicked", lambda *_: self._toggle_ai())
        content.pack_start(self.ai_button, False, False, 0)
        self._update_ai_button()

        self.bottom_bar_content = content
        return bar

    @staticmethod
    def _tool_button(icon: str, tooltip: str) -> Gtk.Button:
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR))
        button.set_tooltip_text(tooltip)
        return button

    def _build_menubar(self) -> Gtk.MenuBar:
        menubar = Gtk.MenuBar()

        file_item = Gtk.MenuItem.new_with_mnemonic("_Файл")
        file_menu = Gtk.Menu()
        file_item.set_submenu(file_menu)
        file_menu.append(self._item("_Новый", "win.new"))
        file_menu.append(self._item("_Открыть…", "win.open"))
        file_menu.append(Gtk.SeparatorMenuItem())
        file_menu.append(self._item("_Сохранить", "win.save"))
        file_menu.append(self._item("Сохранить _как…", "win.save-as"))
        file_menu.append(Gtk.SeparatorMenuItem())
        file_menu.append(self._item("_Выход", "win.quit"))
        menubar.append(file_item)

        edit_item = Gtk.MenuItem.new_with_mnemonic("_Правка")
        edit_menu = Gtk.Menu()
        edit_item.set_submenu(edit_menu)
        self.undo_item = self._item("_Отменить", "win.undo")
        self.redo_item = self._item("Вер_нуть", "win.redo")
        edit_menu.append(self.undo_item)
        edit_menu.append(self.redo_item)
        edit_menu.append(Gtk.SeparatorMenuItem())
        edit_menu.append(self._item("_Вырезать", "win.cut"))
        edit_menu.append(self._item("_Копировать", "win.copy"))
        edit_menu.append(self._item("_Вставить", "win.paste"))
        edit_menu.append(self._item("_Удалить", "win.delete"))
        edit_menu.append(Gtk.SeparatorMenuItem())
        edit_menu.append(self._item("Выделить _всё", "win.select-all"))
        menubar.append(edit_item)

        view_item = Gtk.MenuItem.new_with_mnemonic("_Вид")
        view_menu = Gtk.Menu()
        view_item.set_submenu(view_menu)
        view_menu.append(self._check_item("_Перенос строк", "win.wrap"))
        view_menu.append(self._check_item("_Номера строк", "win.line-numbers"))
        view_menu.append(
            self._check_item("Подсветка _текущей строки", "win.highlight-line")
        )
        view_menu.append(
            self._check_item("Проверка орфографии", "win.spell-check")
        )
        menubar.append(view_item)

        help_item = Gtk.MenuItem.new_with_mnemonic("_Справка")
        help_menu = Gtk.Menu()
        help_item.set_submenu(help_menu)
        about = Gtk.MenuItem.new_with_mnemonic("О _программе")
        about.connect("activate", self._on_about)
        help_menu.append(about)
        menubar.append(help_item)

        return menubar

    @staticmethod
    def _item(label: str, action: str) -> Gtk.MenuItem:
        item = Gtk.MenuItem.new_with_mnemonic(label)
        item.set_action_name(action)
        return item

    @staticmethod
    def _check_item(label: str, action: str) -> Gtk.CheckMenuItem:
        item = Gtk.CheckMenuItem.new_with_mnemonic(label)
        item.set_action_name(action)
        return item

    def _build_statusbar(self) -> Gtk.Box:
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        bar.get_style_context().add_class("statusbar")

        self.pos_label = Gtk.Label(label="Стр 1, Стлб 1")
        self.enc_label = Gtk.Label(label="UTF-8")
        self.message_label = Gtk.Label(label="")
        self.message_label.set_xalign(0.0)
        self.message_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.counts_label = Gtk.Label(label="")
        for label in (self.pos_label, self.enc_label, self.counts_label):
            label.set_xalign(0.0)

        bar.pack_start(self.pos_label, False, False, 0)
        bar.pack_start(self.enc_label, False, False, 0)
        bar.pack_start(self.message_label, True, True, 0)
        bar.pack_end(self.counts_label, False, False, 0)
        return bar

    # -------------------------------------------------------------- actions

    def _install_actions(self, application: Gtk.Application) -> None:
        entries: list[tuple[str, object, list[str] | None]] = [
            ("new", self.action_new, ["<Primary>n"]),
            ("open", self.action_open, ["<Primary>o"]),
            ("save", self.action_save, ["<Primary>s"]),
            ("save-as", self.action_save_as, ["<Primary><Shift>s"]),
            ("undo", self.action_undo, ["<Primary>z"]),
            ("redo", self.action_redo, ["<Primary><Shift>z", "<Primary>y"]),
            ("cut", self.action_cut, ["<Primary>x"]),
            ("copy", self.action_copy, ["<Primary>c"]),
            ("paste", self.action_paste, ["<Primary>v"]),
            ("delete", self.action_delete, []),
            ("select-all", self.action_select_all, ["<Primary>a"]),
            ("quit", self.action_quit, ["<Primary>q"]),
        ]
        for name, handler, accels in entries:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", handler)
            self.add_action(action)
            if accels:
                application.set_accels_for_action(f"win.{name}", accels)
        self.undo_action = self.lookup_action("undo")
        self.redo_action = self.lookup_action("redo")

        toggles = [
            ("wrap", self.view.get_wrap_mode() != Gtk.WrapMode.NONE, self._set_wrap),
            ("line-numbers", self.view.get_show_line_numbers(), self._set_line_numbers),
            (
                "highlight-line",
                self.view.get_highlight_current_line(),
                self._set_highlight_line,
            ),
            ("spell-check", self._spell_enabled(), self._set_spell_check),
        ]
        for name, initial, setter in toggles:
            action = Gio.SimpleAction.new_stateful(
                name, None, GLib.Variant.new_boolean(bool(initial))
            )
            action.connect(
                "change-state",
                lambda act, value, setter=setter: (
                    act.set_state(value),
                    setter(value.get_boolean()),
                ),
            )
            self.add_action(action)

        if not self.spellcheck.available:
            self.lookup_action("spell-check").set_enabled(False)

    def _spell_enabled(self) -> bool:
        return self.spellcheck.enabled

    def _set_wrap(self, enabled: bool) -> None:
        self.view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if enabled else Gtk.WrapMode.NONE)

    def _set_line_numbers(self, enabled: bool) -> None:
        self.view.set_show_line_numbers(enabled)

    def _set_highlight_line(self, enabled: bool) -> None:
        self.view.set_highlight_current_line(enabled)

    def _set_spell_check(self, enabled: bool) -> None:
        self.spellcheck.set_enabled(enabled)

    # ------------------------------------------------------------- formatting

    def _change_font_size(self, delta: int) -> None:
        self.font_size_spin.set_value(self.font_size_spin.get_value() + delta)
        self.view.grab_focus()

    def _on_font_size_changed(self, spin: Gtk.SpinButton) -> None:
        size = int(spin.get_value())
        self.view.modify_font(
            Pango.FontDescription.from_string(f"{default_font_family()} {size}")
        )

    def _on_alignment_toggled(self, button: Gtk.ToggleButton, key: str) -> None:
        if self._align_guard:
            return
        if not button.get_active():
            # Не позволяем снять последнее выравнивание.
            if self._current_align == key:
                self._align_guard = True
                button.set_active(True)
                self._align_guard = False
            return

        self._align_guard = True
        for other_key, other in self.align_buttons.items():
            if other_key != key:
                other.set_active(False)
        self._align_guard = False

        self._current_align = key
        self._apply_alignment(key)
        self.view.grab_focus()

    def _apply_alignment(self, key: str) -> None:
        start, end = self._selected_line_bounds()
        if start is None:
            return
        self.buffer.begin_user_action()
        try:
            for tag in self._align_tags.values():
                self.buffer.remove_tag(tag, start, end)
            self.buffer.apply_tag(self._align_tags[key], start, end)
        finally:
            self.buffer.end_user_action()

    def _selected_line_bounds(self) -> tuple[Gtk.TextIter | None, Gtk.TextIter | None]:
        """Границы строк: выделенных либо той, где стоит курсор."""
        bounds = self.buffer.get_selection_bounds()
        if bounds:
            first, last = bounds
            # Выделение, заканчивающееся в начале строки, не захватывает её.
            if last.get_line_offset() == 0 and last.get_line() > first.get_line():
                last = last.copy()
                last.backward_char()
        else:
            first = self.buffer.get_iter_at_mark(self.buffer.get_insert())
            last = first

        start = self.buffer.get_iter_at_line(first.get_line())
        end = self.buffer.get_iter_at_line(last.get_line())
        # Захватываем и перевод строки: тогда текст, набранный в конце
        # абзаца, наследует выравнивание.
        end.forward_line()
        return start, end

    def _alignment_at(self, offset: int) -> str | None:
        if offset < 0:
            return None
        iterator = self.buffer.get_iter_at_offset(offset)
        for key, tag in self._align_tags.items():
            if iterator.has_tag(tag):
                return key
        return None

    def _on_insert_text(self, _buffer, location, text, _length) -> None:
        """Продлевает выравнивание на новый текст и новые абзацы."""
        end_offset = location.get_offset()
        start_offset = end_offset - len(text)

        if "\n" in text:
            # Enter: помечаем сам перевод строки, чтобы следующая пустая
            # строка унаследовала выравнивание предыдущей.
            key = self._alignment_at(start_offset - 1)
            if key is not None:
                start = self.buffer.get_iter_at_offset(start_offset)
                end = self.buffer.get_iter_at_offset(end_offset)
                self.buffer.apply_tag(self._align_tags[key], start, end)
            return

        # Первый символ в пустой строке наследует выравнивание предыдущей;
        # текст в конце выровненной строки тоже нужно пометить.
        line_start = self.buffer.get_iter_at_offset(start_offset)
        line_start.set_line_offset(0)
        at_line_start = line_start.get_offset() == start_offset

        line_end = self.buffer.get_iter_at_offset(end_offset)
        line_end.forward_to_line_end()
        line_was_empty = at_line_start and line_end.get_offset() == end_offset

        if at_line_start and not line_was_empty:
            # Начало обычной строки не должно наследовать чужое выравнивание.
            return

        key = self._alignment_at(start_offset - 1)
        if key is None:
            return

        if line_was_empty:
            line_end = self.buffer.get_iter_at_offset(start_offset)
            line_end.forward_line()
            self.buffer.apply_tag(self._align_tags[key], line_start, line_end)
        else:
            start = self.buffer.get_iter_at_offset(start_offset)
            end = self.buffer.get_iter_at_offset(end_offset)
            self.buffer.apply_tag(self._align_tags[key], start, end)

    def _sync_alignment_buttons(self) -> None:
        iterator = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        current = "left"
        for key, tag in self._align_tags.items():
            if iterator.has_tag(tag):
                current = key
                break
        if current == self._current_align:
            return

        self._current_align = current
        self._align_guard = True
        for key, button in self.align_buttons.items():
            button.set_active(key == current)
        self._align_guard = False

    # -------------------------------------------------------------- signals

    def _connect_signals(self) -> None:
        self.buffer.connect("notify::cursor-position", lambda *_: self._update_status())
        self.buffer.connect("modified-changed", lambda *_: self._update_title())
        self.buffer.connect("changed", lambda *_: self._schedule_counts())
        try:
            self.buffer.connect("can-undo-changed", lambda *_: self._update_undo())
            self.buffer.connect("can-redo-changed", lambda *_: self._update_undo())
        except TypeError:
            pass

        self.connect("delete-event", self._on_delete_event)

        # Перетаскивание файла в окно.
        self.drag_dest_set(Gtk.DestDefaults.ALL, [], Gdk.DragAction.COPY)
        self.drag_dest_add_uri_targets()
        self.connect("drag-data-received", self._on_drag_data)

        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.connect("notify::gtk-theme-name", lambda *_: self._apply_style_scheme())

        # Контекстное меню: подсказки правописания.
        self.view.connect("button-press-event", self._on_view_button_press)
        self.view.connect("populate-popup", self._on_populate_popup)

        # Выравнивание должно переходить на набранный текст.
        self.buffer.connect_after("insert-text", self._on_insert_text)

    def _update_undo(self) -> None:
        self.undo_action.set_enabled(self.buffer.can_undo())
        self.redo_action.set_enabled(self.buffer.can_redo())

    def _install_focus_handling(self) -> None:
        """Клавиатурный фокус должен всегда попадать в текст, а не в меню."""
        self.set_focus(self.view)
        self.connect("map", lambda *_: self.view.grab_focus())
        self.connect("window-state-event", self._on_window_state)

    def _on_window_state(self, _widget, event) -> bool:
        if event.changed_mask & Gdk.WindowState.FOCUSED and event.new_window_state & Gdk.WindowState.FOCUSED:
            if self.get_focus() is None:
                self.view.grab_focus()
        return False

    # ------------------------------------------------------------ popup menu

    def _on_view_button_press(self, widget, event) -> bool:
        """Запоминает слово под курсором при правом клике."""
        if event.button == 3:
            x, y = widget.window_to_buffer_coords(
                Gtk.TextWindowType.WIDGET, int(event.x), int(event.y)
            )
            found, iterator = widget.get_iter_at_location(x, y)
            self._popup_iter = iterator if found else None
        return False

    def _on_populate_popup(self, _view, popup: Gtk.Menu) -> None:
        if self._popup_iter is not None and self.spellcheck.enabled:
            self._add_spell_items(popup)

        # «Проверить с ИИ» появляется только при выделенном фрагменте
        # и только когда ИИ включён.
        bounds = self.buffer.get_selection_bounds()
        if bounds and self._ai_enabled:
            popup.append(Gtk.SeparatorMenuItem())
            item = Gtk.MenuItem(label="Проверить с ИИ")
            item.set_sensitive(not self._ai_busy)
            item.connect("activate", lambda *_: self._ai_check_selection())
            popup.append(item)
        popup.show_all()

    def _add_spell_items(self, popup: Gtk.Menu) -> None:
        found = self.spellcheck.word_at(self._popup_iter.get_offset())
        if found is None:
            return
        word, start, end = found
        if self.spellcheck.is_correct(word):
            return

        items: list[Gtk.Widget] = []
        header = Gtk.MenuItem(label=f"«{word}»")
        header.set_sensitive(False)
        items.append(header)

        suggestions = self.spellcheck.suggestions(word)
        for suggestion in suggestions:
            item = Gtk.MenuItem(label=suggestion)
            item.connect(
                "activate",
                lambda _item, replacement=suggestion: self.spellcheck.replace(
                    start, end, replacement
                ),
            )
            items.append(item)
        if suggestions:
            items.append(Gtk.SeparatorMenuItem())

        add_item = Gtk.MenuItem(label="Добавить в словарь")
        add_item.connect("activate", lambda *_: self.spellcheck.add_to_session(word))
        items.append(add_item)
        items.append(Gtk.SeparatorMenuItem())

        for item in reversed(items):
            popup.prepend(item)

    # ------------------------------------------------------------- AI check

    def _show_message(self, text: str, seconds: int = 8) -> None:
        if self._message_timeout:
            GLib.source_remove(self._message_timeout)
            self._message_timeout = 0
        self.message_label.set_text(text)
        if seconds:
            self._message_timeout = GLib.timeout_add_seconds(seconds, self._clear_message)

    def _clear_message(self) -> bool:
        self._message_timeout = 0
        self.message_label.set_text("")
        return False

    def _update_ai_button(self) -> None:
        if self._ai_enabled:
            self.ai_button.set_label("Выключить ИИ")
            self.ai_button.set_tooltip_text(
                "Выключить проверку с ИИ и выгрузить модель из памяти"
            )
        else:
            self.ai_button.set_label("Включить ИИ")
            self.ai_button.set_tooltip_text(
                "Разрешить проверку с ИИ. Модель загрузится при первой проверке"
            )

    def _toggle_ai(self) -> None:
        if self._ai_unloading:
            self._show_message("Модель ещё выгружается…")
            return

        if self._ai_enabled:
            if self._ai_busy:
                self._show_message("Дождитесь окончания проверки")
                return
            self._ai_enabled = False
            self._update_ai_button()
            self._ai_unloading = True
            self.ai_button.set_sensitive(False)
            self._show_message(
                f"Выгружаю модель {ai_check.MODEL_NAME} из памяти…", seconds=0
            )
            threading.Thread(target=self._ai_unload_worker, daemon=True).start()
        else:
            self._ai_enabled = True
            self._update_ai_button()
            self._show_message(
                "ИИ включён. Если модели нет, приложение предложит её скачать."
            )

    def _ai_unload_worker(self) -> None:
        freed = 0
        error: str | None = None
        try:
            freed = ai_check.unload_model()
        except ai_check.AIError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_unload_finished, freed, error)

    def _ai_unload_finished(self, freed: int, error: str | None) -> bool:
        self._ai_unloading = False
        if not self.get_realized():
            return False
        self.ai_button.set_sensitive(True)

        if error is not None:
            short = " ".join(error.split())
            self._show_message(f"ИИ выключен, но модель не выгружена: {short}", seconds=12)
            return False

        if freed >= 100_000_000:
            self._show_message(
                "ИИ выключен. Модель выгружена, освобождено "
                f"~{freed / 1_000_000_000:.1f} ГБ памяти."
            )
        else:
            self._show_message("ИИ выключен. Модель не была загружена.")
        return False

    def _ai_check_selection(self) -> None:
        if not self._ai_enabled:
            self._show_message("ИИ выключен — включите кнопкой внизу панели")
            return
        if self._ai_busy:
            self._show_message("Проверка уже идёт…")
            return

        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            self._show_message("Сначала выделите текст для проверки")
            return

        start, end = bounds
        text = self.buffer.get_text(start, end, True)
        if not text.strip():
            self._show_message("Выделенный фрагмент пуст")
            return

        self._ai_busy = True
        self._show_message("Проверяю готовность ИИ…", seconds=0)
        threading.Thread(
            target=self._ai_probe_worker,
            args=(text, start.get_offset()),
            daemon=True,
        ).start()

    # Модель может быть ещё не скачана. Перед проверкой узнаём её состояние
    # в фоне, чтобы не подвешивать окно, и при необходимости предлагаем
    # скачать модель прямо из приложения.

    def _ai_probe_worker(self, text: str, offset: int) -> None:
        state = "offline"
        error: str | None = None
        try:
            state = ai_check.model_state()
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_probe_finished, text, offset, state, error)

    def _ai_probe_finished(
        self, text: str, offset: int, state: str, error: str | None
    ) -> bool:
        if not self.get_realized():
            return False

        if error is not None:
            self._ai_busy = False
            self._show_message("ИИ не смог проверить текст", seconds=10)
            self._show_ai_error(error)
            return False

        if state == "offline":
            self._ai_busy = False
            self._show_message("Ollama не запущена", seconds=10)
            self._show_ai_error(
                "Не удалось подключиться к Ollama.\n"
                "Проверьте, что служба запущена: systemctl status ollama"
            )
            return False

        if state == "missing":
            self._ask_download_model(text, offset)
            return False

        self._show_message(
            f"Проверяю текст с ИИ (модель {ai_check.MODEL_NAME})…", seconds=0
        )
        threading.Thread(
            target=self._ai_worker, args=(text, offset), daemon=True
        ).start()
        return False

    def _ask_download_model(self, text: str, offset: int) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="Модель для ИИ не найдена",
        )
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Скачать", Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)
        dialog.format_secondary_text(
            f"Для проверки текста нужна модель «{ai_check.MODEL_NAME}», "
            "но её ещё нет на этом компьютере.\n\n"
            "Скачать сейчас? Это может занять несколько минут и потребовать "
            "несколько гигабайт свободного места. Скачивание можно отменить."
        )
        dialog.connect(
            "response", self._on_download_response, text, offset
        )
        dialog.show()

    def _on_download_response(
        self, dialog: Gtk.MessageDialog, response: int, text: str, offset: int
    ) -> None:
        dialog.destroy()
        if response != Gtk.ResponseType.YES:
            self._ai_busy = False
            self._show_message(
                "Проверка отменена: модель не скачана", seconds=10
            )
            return
        self._start_model_pull(text, offset)

    def _start_model_pull(self, text: str, offset: int) -> None:
        if self._ai_pulling:
            return
        self._ai_pulling = True
        self._ai_pull_cancel = threading.Event()
        self._ai_pull_status = ""
        self._ai_pull_last_update = 0.0
        self._build_pull_dialog()
        threading.Thread(
            target=self._ai_pull_worker, args=(text, offset), daemon=True
        ).start()

    def _build_pull_dialog(self) -> None:
        dialog = Gtk.Dialog(
            title="Скачивание модели",
            transient_for=self,
            modal=True,
        )
        dialog.set_default_size(520, -1)
        dialog.set_resizable(False)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.set_default_response(Gtk.ResponseType.CANCEL)
        dialog.connect("response", self._on_pull_cancel)

        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)

        header = Gtk.Label()
        header.set_markup(
            "Скачиваю модель <b>"
            f"{GLib.markup_escape_text(ai_check.MODEL_NAME)}</b>"
        )
        header.set_xalign(0.0)
        header.set_line_wrap(True)
        content.pack_start(header, False, False, 0)

        self._ai_pull_title = Gtk.Label(label="Подключаюсь к Ollama…")
        self._ai_pull_title.set_xalign(0.0)
        self._ai_pull_title.set_line_wrap(True)
        content.pack_start(self._ai_pull_title, False, False, 0)

        self._ai_pull_progress = Gtk.ProgressBar()
        self._ai_pull_progress.set_show_text(True)
        content.pack_start(self._ai_pull_progress, False, False, 0)

        self._ai_pull_detail = Gtk.Label(
            label="Скачивание можно отменить — загруженные части сохранятся."
        )
        self._ai_pull_detail.set_xalign(0.0)
        self._ai_pull_detail.set_line_wrap(True)
        self._ai_pull_detail.get_style_context().add_class("dim-label")
        content.pack_start(self._ai_pull_detail, False, False, 0)

        self._ai_pull_dialog = dialog
        dialog.show_all()

    def _on_pull_cancel(
        self, dialog: Gtk.Dialog, response: int
    ) -> None:
        if response not in (Gtk.ResponseType.CANCEL, Gtk.ResponseType.DELETE_EVENT):
            return
        if self._ai_pull_cancel is not None:
            self._ai_pull_cancel.set()
        area = dialog.get_action_area()
        if area is not None:
            for child in area.get_children():
                child.set_sensitive(False)
        if self._ai_pull_title is not None:
            self._ai_pull_title.set_text("Отменяю скачивание…")

    def _ai_pull_progress_cb(
        self, status: str, completed: int, total: int
    ) -> None:
        """Вызывается из фонового потока: не чаще 4 раз в секунду."""
        now = time.monotonic()
        if status == self._ai_pull_status and now - self._ai_pull_last_update < 0.25:
            return
        self._ai_pull_status = status
        self._ai_pull_last_update = now
        GLib.idle_add(self._ai_pull_progress_update, status, completed, total)

    def _ai_pull_progress_update(
        self, status: str, completed: int, total: int
    ) -> bool:
        if not self.get_realized() or self._ai_pull_progress is None:
            return False

        if self._ai_pull_title is not None:
            self._ai_pull_title.set_text(pull_status_text(status))

        if total > 0:
            fraction = min(completed / total, 1.0)
            self._ai_pull_progress.set_fraction(fraction)
            self._ai_pull_progress.set_text(
                f"{_fmt_bytes(completed)} из {_fmt_bytes(total)} · "
                f"{fraction * 100:.0f} %"
            )
        else:
            self._ai_pull_progress.pulse()
            self._ai_pull_progress.set_text("")
        return False

    def _ai_pull_worker(self, text: str, offset: int) -> None:
        downloaded = False
        cancelled = False
        error: str | None = None
        try:
            downloaded = ai_check.pull_model(
                on_progress=self._ai_pull_progress_cb,
                cancel=self._ai_pull_cancel,
            )
        except ai_check.AICancelled as exc:
            cancelled = True
            error = str(exc)
        except ai_check.AIError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(
            self._ai_pull_finished, downloaded, cancelled, error, text, offset
        )

    def _ai_pull_finished(
        self,
        downloaded: bool,
        cancelled: bool,
        error: str | None,
        text: str,
        offset: int,
    ) -> bool:
        self._ai_pulling = False
        self._ai_pull_cancel = None

        dialog = self._ai_pull_dialog
        self._ai_pull_dialog = None
        self._ai_pull_progress = None
        self._ai_pull_title = None
        self._ai_pull_detail = None
        if dialog is not None:
            dialog.destroy()

        if not self.get_realized():
            return False

        if cancelled:
            self._ai_busy = False
            self._show_message("Скачивание модели отменено", seconds=10)
            return False

        if error is not None:
            self._ai_busy = False
            self._show_message("Не удалось скачать модель", seconds=10)
            self._show_ai_error(error)
            return False

        if downloaded:
            self._show_message(
                f"Модель {ai_check.MODEL_NAME} скачана. Проверяю текст…",
                seconds=0,
            )
        else:
            self._show_message(
                f"Модель {ai_check.MODEL_NAME} уже скачана. Проверяю текст…",
                seconds=0,
            )
        threading.Thread(
            target=self._ai_worker, args=(text, offset), daemon=True
        ).start()
        return False

    def _ai_worker(self, text: str, offset: int) -> None:
        corrected: str | None = None
        error: str | None = None
        try:
            corrected = ai_check.check_text(text)
        except ai_check.AIError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_finished, text, offset, corrected, error)

    def _ai_finished(
        self,
        original: str,
        offset: int,
        corrected: str | None,
        error: str | None,
    ) -> bool:
        self._ai_busy = False
        if not self.get_realized():
            return False

        if error is not None:
            self._show_message("ИИ не смог проверить текст", seconds=10)
            self._show_ai_error(error)
            return False

        end_offset = offset + len(original)
        start = self.buffer.get_iter_at_offset(offset)
        end = self.buffer.get_iter_at_offset(end_offset)
        if self.buffer.get_text(start, end, True) != original:
            self._show_message("Текст изменился во время проверки — результат не применён")
            return False

        assert corrected is not None
        if corrected == original or corrected.strip() == original.strip():
            self._show_message("ИИ не нашёл ошибок")
            return False

        self.buffer.begin_user_action()
        try:
            self.buffer.delete(start, end)
            insert_at = self.buffer.get_iter_at_offset(offset)
            self.buffer.insert(insert_at, corrected)
            self.buffer.select_range(
                self.buffer.get_iter_at_offset(offset),
                self.buffer.get_iter_at_offset(offset + len(corrected)),
            )
        finally:
            self.buffer.end_user_action()

        self._show_message(
            f"ИИ исправил текст ({len(original)} → {len(corrected)} символов). "
            "Отменить — Ctrl+Z"
        )
        return False

    def _show_ai_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text="Не удалось проверить текст",
        )
        dialog.format_secondary_text(message)
        dialog.connect("response", lambda dlg, _response: dlg.destroy())
        dialog.show()

    # ---------------------------------------------------------------- theme

    def _apply_style_scheme(self) -> None:
        settings = Gtk.Settings.get_default()
        dark = False
        if settings is not None:
            theme = (settings.get_property("gtk-theme-name") or "").lower()
            dark = bool(settings.get_property("gtk-application-prefer-dark-theme")) or (
                "dark" in theme
            )
        scheme_id = "oblivion" if dark else "kate"
        manager = GtkSource.StyleSchemeManager.get_default()
        scheme = manager.get_scheme(scheme_id) or manager.get_scheme("classic")
        if scheme is not None:
            self.buffer.set_style_scheme(scheme)

    # ------------------------------------------------------------------ text

    def get_text(self) -> str:
        start, end = self.buffer.get_bounds()
        return self.buffer.get_text(start, end, True)

    def set_text(self, text: str, path: Path | None = None) -> None:
        try:
            self.buffer.begin_not_undoable_action()
            self.buffer.set_text(text)
        finally:
            try:
                self.buffer.end_not_undoable_action()
            except Exception:  # noqa: BLE001
                pass
        self.buffer.set_modified(False)
        self.path = Path(path) if path is not None else None
        self.buffer.place_cursor(self.buffer.get_start_iter())
        self.view.scroll_to_iter(self.buffer.get_start_iter(), 0.0, False, 0.0, 0.0)
        self._update_title()
        self._update_status()
        self._update_counts()
        self._update_undo()
        self.spellcheck.schedule()
        self.view.grab_focus()

    # ------------------------------------------------------------------ files

    def open_path(self, path: str | Path) -> None:
        path = Path(path)
        try:
            text, encoding = document.read_text(path)
        except OSError as exc:
            self._show_error("Не удалось открыть файл", str(exc))
            return
        self.set_text(text, path)
        self.encoding = encoding
        self.enc_label.set_text(encoding)
        self._update_title()

    def action_new(self, *_args) -> None:
        if not self._confirm_discard():
            return
        self.encoding = "UTF-8"
        self.enc_label.set_text(self.encoding)
        self.set_text("", None)

    def action_open(self, *_args) -> None:
        if not self._confirm_discard():
            return

        dialog = Gtk.FileChooserNative.new(
            "Открыть файл", self, Gtk.FileChooserAction.OPEN, "_Открыть", "_Отмена"
        )
        self._add_filters(dialog)
        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and path:
            self.open_path(path)

    def action_save(self, *_args) -> bool:
        if self.path is None:
            return self.action_save_as()
        return self._save_to(self.path)

    def action_save_as(self, *_args) -> bool:
        dialog = Gtk.FileChooserNative.new(
            "Сохранить как", self, Gtk.FileChooserAction.SAVE, "_Сохранить", "_Отмена"
        )
        dialog.set_do_overwrite_confirmation(True)
        self._add_filters(dialog)
        if self.path is not None:
            dialog.set_current_name(self.path.name)
            dialog.set_current_folder(str(self.path.parent))
        else:
            dialog.set_current_name("Без имени.txt")

        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not path:
            return False
        return self._save_to(Path(path))

    def _save_to(self, path: Path) -> bool:
        try:
            document.write_text(path, self.get_text())
        except OSError as exc:
            self._show_error("Не удалось сохранить файл", str(exc))
            return False
        self.path = Path(path)
        self.buffer.set_modified(False)
        self._update_title()
        return True

    @staticmethod
    def _add_filters(dialog: Gtk.FileChooserNative) -> None:
        text_filter = Gtk.FileFilter()
        text_filter.set_name("Текстовые файлы")
        text_filter.add_mime_type("text/plain")
        text_filter.add_pattern("*.txt")
        text_filter.add_pattern("*.md")
        dialog.add_filter(text_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name("Все файлы")
        all_filter.add_pattern("*")
        dialog.add_filter(all_filter)

    # ------------------------------------------------------------- edit ops

    def action_undo(self, *_args) -> None:
        if self.buffer.can_undo():
            self.buffer.undo()

    def action_redo(self, *_args) -> None:
        if self.buffer.can_redo():
            self.buffer.redo()

    def action_cut(self, *_args) -> None:
        self.buffer.cut_clipboard(self._clipboard(), self.view.get_editable())

    def action_copy(self, *_args) -> None:
        self.buffer.copy_clipboard(self._clipboard())

    def action_paste(self, *_args) -> None:
        self.buffer.paste_clipboard(
            self._clipboard(), None, self.view.get_editable()
        )

    @staticmethod
    def _clipboard() -> Gtk.Clipboard:
        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    def action_delete(self, *_args) -> None:
        if self.buffer.get_has_selection():
            self.buffer.delete_selection(True, True)

    def action_select_all(self, *_args) -> None:
        start, end = self.buffer.get_bounds()
        self.buffer.select_range(end, start)

    def action_quit(self, *_args) -> None:
        self.close()

    # --------------------------------------------------------------- status

    def _update_title(self) -> None:
        name = self.path.name if self.path is not None else "Без имени"
        mark = "• " if self.buffer.get_modified() else ""
        self.set_title(f"{mark}{name} — {APP_NAME}")

    def _update_status(self) -> None:
        iterator = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        self.pos_label.set_text(
            f"Стр {iterator.get_line() + 1}, Стлб {iterator.get_line_offset() + 1}"
        )
        self._sync_alignment_buttons()

    def _schedule_counts(self) -> None:
        if self._counts_timeout:
            return
        self._counts_timeout = GLib.timeout_add(200, self._update_counts)

    def _update_counts(self) -> bool:
        self._counts_timeout = 0
        stats = document.text_stats(self.get_text())
        if stats != self._counts_cache:
            self._counts_cache = stats
            words, chars, _ = stats
            self.counts_label.set_text(f"{_fmt(words)} сл. · {_fmt(chars)} симв.")
        return False

    def _show_error(self, title: str, details: str) -> None:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(details)
        dialog.run()
        dialog.destroy()

    def _confirm_discard(self) -> bool:
        """True — можно продолжать; False — пользователь отменил."""
        if not self.buffer.get_modified():
            return True

        name = self.path.name if self.path is not None else "Без имени"
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.WARNING,
            buttons=Gtk.ButtonsType.NONE,
            text="Сохранить изменения?",
        )
        dialog.format_secondary_text(f"Файл «{name}» изменён.")
        dialog.add_button("_Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("_Не сохранять", Gtk.ResponseType.NO)
        dialog.add_button("_Сохранить", Gtk.ResponseType.YES)
        dialog.set_default_response(Gtk.ResponseType.YES)
        response = dialog.run()
        dialog.destroy()

        if response == Gtk.ResponseType.YES:
            return self.action_save()
        return response == Gtk.ResponseType.NO

    def _on_delete_event(self, *_args) -> bool:
        return not self._confirm_discard()

    def _on_drag_data(self, widget, context, x, y, data, info, time) -> None:
        uris = data.get_uris() or []
        path = Gio.File.new_for_uri(uris[0]).get_path() if uris else None
        if path and self._confirm_discard():
            self.open_path(path)
        Gtk.drag_finish(context, bool(path), False, time)

    def _on_about(self, *_args) -> None:
        from . import __version__

        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(__version__)
        dialog.set_comments(
            "Простой текстовый редактор с проверкой орфографии.\n"
            "Следующий шаг — ИИ-проверка грамматики и пунктуации."
        )
        dialog.set_icon_name(self._icon_name)
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.run()
        dialog.destroy()
