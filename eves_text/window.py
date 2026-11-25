"""Главное окно редактора."""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("Gdk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import (  # noqa: E402
    Gdk,
    GdkPixbuf,
    Gio,
    GLib,
    GObject,
    Gtk,
    GtkSource,
    Pango,
)

from . import (  # noqa: E402
    __version__,
    ai_check,
    document,
    updates,
    win_com_drop,
    win_dnd,
    win_drop,
    win_theme,
)
from . import settings as user_settings  # noqa: E402
from .caret import CaretBlinker  # noqa: E402
from .ollama_server import (  # noqa: E402
    ManagedOllama,
    OllamaStartError,
    default_models_dir,
)
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

# Теги начертаний и префикс тегов размера для отдельных кусков текста.
BOLD_TAG = "format-bold"
ITALIC_TAG = "format-italic"
UNDERLINE_TAG = "format-underline"
SIZE_TAG_PREFIX = "format-size:"
TEXT_FORMAT_KEYS = ("bold", "italic", "underline")

# Явные цели для перетаскивания файлов. Разные info позволяют понять,
# что именно прислал источник: URI-список или обычный текст с путём.
# Значения выше внутренних TARGET_* у GtkTextView, чтобы не пересечься
# с его собственным обработчиком вставки текста.
DND_URI_LIST = 100
DND_UTF8_STRING = 101
DND_TEXT_PLAIN = 102
# "Shell IDList Array" намеренно не объявляем: GDK не должен регистрировать
# свой IDropTarget на том же HWND. В Windows файлы принимает наш COM-приёмник.
DND_TARGETS: tuple[tuple[str, int], ...] = (
    ("text/uri-list", DND_URI_LIST),
    ("UTF8_STRING", DND_UTF8_STRING),
    ("text/plain", DND_TEXT_PLAIN),
)

# Класс, который ставим на кнопку вручную: в GTK3 псевдокласс :hover
# не всегда применяется (например, в Windows-бандле), поэтому подсветку
# задаём аварийным классом и не полагаемся только на CSS.
HOVER_CLASS = "force-hover"


def _on_hover_enter(widget: Gtk.Widget, _event: Gdk.EventButton) -> bool:
    widget.get_style_context().add_class(HOVER_CLASS)
    return False


def _on_hover_leave(widget: Gtk.Widget, _event: Gdk.EventButton) -> bool:
    widget.get_style_context().remove_class(HOVER_CLASS)
    return False


def install_hover(widget: Gtk.Widget) -> Gtk.Widget:
    """Надёжно подсвечивать кнопку, пока под ней курсор."""
    widget.add_events(
        Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
    )
    widget.connect("enter-notify-event", _on_hover_enter)
    widget.connect("leave-notify-event", _on_hover_leave)
    return widget


# Порог в пикселях, после которого нажатие считается перетаскиванием.
TAB_DRAG_THRESHOLD = 6

# Плавное проезжание вкладок при смене порядка.
TAB_ANIMATION_MS = 180
TAB_ANIMATION_STEP_MS = 16

# Готовые варианты для меню «ИИ» → «Скачать модель».
MODEL_CHOICES: tuple[tuple[str, str], ...] = (
    ("qwen2.5:7b-instruct", "qwen2.5 7B — лучшее качество (~4,7 ГБ)"),
    ("qwen2.5:3b-instruct", "qwen2.5 3B — быстрее (~1,9 ГБ)"),
    ("qwen2.5:1.5b-instruct", "qwen2.5 1.5B — для слабых ПК (~1 ГБ)"),
    ("qwen2.5:0.5b", "qwen2.5 0.5B — самая лёгкая (~400 МБ)"),
    ("gemma3:4b", "Gemma 3 4B — альтернатива (~3,3 ГБ)"),
    ("llama3.2:3b", "Llama 3.2 3B — альтернатива (~2 ГБ)"),
)


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


def app_logo() -> GdkPixbuf.Pixbuf | None:
    """Иконка приложения из ресурсов для диалога «О программе»."""
    resources = Path(__file__).parent / "resources"
    for name in ("eves-text.png", "eves-text.svg"):
        path = resources / name
        if not path.is_file():
            continue
        try:
            return GdkPixbuf.Pixbuf.new_from_file(str(path))
        except (GLib.Error, OSError):
            continue
    return None


class DocumentTab:
    """Одна вкладка: свой буфер, вид, путь, кодировка и орфография."""

    def __init__(self, window: "MainWindow", number: int) -> None:
        self.window = window
        self.default_name = f"Документ {number}"
        self.path: Path | None = None
        self.encoding = "UTF-8"
        self.counts_cache = (-1, -1, -1)
        self.current_align = window._default_align
        self.align_guard = False
        self.popup_iter: Gtk.TextIter | None = None
        self.caret: CaretBlinker | None = None
        self.search_context: GtkSource.SearchContext | None = None
        self._pointer_in_tab = False
        self._pointer_in_close = False

        self.buffer = GtkSource.Buffer()
        self.buffer.set_highlight_syntax(False)

        self.view = GtkSource.View.new_with_buffer(self.buffer)
        window._configure_view(self.view)

        # Теги выравнивания абзацев у каждого документа свои.
        self.align_tags = {
            key: self.buffer.create_tag(f"alignment:{key}", justification=alignment)
            for key, _icon, alignment, _tip in ALIGNMENTS
        }

        # Оформление отдельного текста: начертания и размер для выделения.
        self.bold_tag = self.buffer.create_tag(
            BOLD_TAG, weight=Pango.Weight.BOLD, weight_set=True
        )
        self.italic_tag = self.buffer.create_tag(
            ITALIC_TAG, style=Pango.Style.ITALIC, style_set=True
        )
        self.underline_tag = self.buffer.create_tag(
            UNDERLINE_TAG, underline=Pango.Underline.SINGLE
        )
        self.size_tags: dict[int, Gtk.TextTag] = {}
        self.formatted = False
        self.bold_typing = False
        self.italic_typing = False
        self.underline_typing = False
        self.active_size: int | None = None

        # Проверка орфографии делается своим слоем на текстовых тегах.
        self.spellcheck = SpellCheck(self.buffer, self.view)
        # На Windows штатная каретка GTK оставляет светлый пиксель на конце,
        # поэтому там рисуется и мигает своя каретка.
        if os.name == "nt":
            self.caret = CaretBlinker(
                self.view, dark=getattr(window, "_dark_theme", False)
            )

        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.scroller.add(self.view)

        # Заголовок вкладки с кнопкой закрытия.
        self.name_label = Gtk.Label(label=self.default_name)
        self.name_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.name_label.set_max_width_chars(28)
        close = Gtk.Button.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU
        )
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_focus_on_click(False)
        close.set_tooltip_text("Закрыть вкладку")
        close.get_style_context().add_class("tab-close")
        close.connect("clicked", lambda *_: window.close_tab(self))
        self.close_button = close

        # Отступы внутри вкладки задаём полям содержимого: Gtk.EventBox
        # не применяет CSS-padding к дочернему виджету.
        self.label_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self.label_box.set_margin_start(20)
        self.label_box.set_margin_end(18)
        self.label_box.set_margin_top(1)
        self.label_box.set_margin_bottom(1)
        self.label_box.pack_start(self.name_label, False, False, 0)
        self.label_box.pack_start(close, False, False, 0)

        # Кликабельная плашка вкладки. Gtk.EventBox, а не Gtk.Button:
        # внутри уже есть кнопка закрытия, и она должна получать свой клик.
        self.tab = Gtk.EventBox()
        self.tab.get_style_context().add_class("doc-tab")
        self.tab.add(self.label_box)
        self.tab.connect("button-press-event", self._on_clicked)
        self.tab.connect("button-release-event", self._on_tab_release)
        self.tab.connect("motion-notify-event", self._on_tab_motion)
        self.tab.connect("enter-notify-event", self._on_tab_enter)
        self.tab.connect("leave-notify-event", self._on_tab_leave)
        self.tab.connect("draw", self._draw_tab)
        self.tab.set_events(
            self.tab.get_events()
            | Gdk.EventMask.BUTTON1_MOTION_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        close.connect("enter-notify-event", self._on_close_enter)
        close.connect("leave-notify-event", self._on_close_leave)
        self.tab.set_tooltip_text("Новый документ")
        self.tab.show_all()
        self._update_close_visibility()

        self.scroller.show_all()

    # ------------------------------------------------------------- оформление

    # Коэффициент для четверти окружности Безье.
    _KAPPA = 0.5522847498

    def _tab_colors(
        self,
    ) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
        """Цвет активной вкладки (как поле ввода) и цвет наведения."""
        if getattr(self.window, "_dark_theme", False):
            return (
                (0x2B / 255, 0x2B / 255, 0x2B / 255),
                (0x30 / 255, 0x31 / 255, 0x34 / 255),
            )
        return (
            (0xFF / 255, 0xFF / 255, 0xFF / 255),
            (0xE8 / 255, 0xEA / 255, 0xED / 255),
        )

    @property
    def hovered(self) -> bool:
        return self._pointer_in_tab or self._pointer_in_close

    def _tab_accent(self) -> tuple[float, float, float]:
        """Цвет обводки перетаскиваемой вкладки."""
        if getattr(self.window, "_dark_theme", False):
            return 0x8A / 255, 0xB4 / 255, 0xF8 / 255
        return 0x1A / 255, 0x73 / 255, 0xE8 / 255

    def _tab_shape(self, cr, width: float, height: float) -> None:
        """Форма вкладки Chrome: скруглённый верх и расходящиеся книзу лапки."""
        foot = height * 0.2
        radius = min(height * 0.3, width / 2 - foot - 1)
        radius = max(radius, 0.0)
        kf = foot * self._KAPPA
        kr = radius * self._KAPPA
        right = width - foot

        cr.new_path()
        cr.move_to(foot + radius, 0)
        cr.line_to(width - foot - radius, 0)
        cr.curve_to(
            width - foot - radius + kr, 0,
            right, radius - kr,
            right, radius,
        )
        cr.line_to(right, height - foot)
        cr.curve_to(right, height - foot + kf, width - kf, height, width, height)
        cr.line_to(0, height)
        # Левая сторона — зеркало правой: вертикальная стенка и «лапка» внизу,
        # а не скошенная линия на всю высоту.
        cr.curve_to(kf, height, foot, height - foot + kf, foot, height - foot)
        cr.line_to(foot, radius)
        cr.curve_to(foot, radius - kr, foot + radius - kr, 0, foot + radius, 0)
        cr.close_path()

    def _draw_tab(self, widget, cr) -> bool:
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        if width < 8 or height < 8:
            return False

        active = widget.get_style_context().has_class("active")
        dragging = self.window._drag_tab is self
        input_color, hover_color = self._tab_colors()
        if active:
            # Активная вкладка — цвет поля ввода и сливается с документом.
            self._tab_shape(cr, width, height)
            cr.set_source_rgb(*input_color)
            cr.fill()
        elif self.hovered or dragging:
            # Неактивная вкладка в покое не рисуется вовсе: видны только текст
            # и крестик. Подсвечивается она лишь под курсором, как в Chrome.
            self._tab_shape(cr, width, height)
            cr.set_source_rgb(*hover_color)
            cr.fill()
            if dragging:
                cr.set_line_width(2.0)
                cr.set_source_rgb(*self._tab_accent())
                cr.stroke()
        return False

    def _update_close_visibility(self) -> None:
        active = self.tab.get_style_context().has_class("active")
        self.close_button.set_opacity(
            0.9 if active or self.hovered else 0.0
        )

    def refresh_visuals(self) -> None:
        """Перерисовать форму и крестик после смены активной вкладки/темы."""
        self._update_close_visibility()
        self.tab.queue_draw()
        # Разделители у соседей зависят от активности и наведения этой вкладки.
        tabs = self.window._tabs
        try:
            index = tabs.index(self)
        except ValueError:
            return
        for neighbor in (index - 1, index + 1):
            if 0 <= neighbor < len(tabs):
                tabs[neighbor].tab.queue_draw()

    def _on_tab_enter(self, *_args) -> bool:
        self._pointer_in_tab = True
        self.refresh_visuals()
        return False

    def _on_tab_leave(self, *_args) -> bool:
        self._pointer_in_tab = False
        self.refresh_visuals()
        return False

    def _on_close_enter(self, *_args) -> bool:
        self._pointer_in_close = True
        self.refresh_visuals()
        return False

    def _on_close_leave(self, *_args) -> bool:
        self._pointer_in_close = False
        self.refresh_visuals()
        return False

    def _on_clicked(self, _widget, event) -> bool:
        if event.button == 1:
            self.window._select_tab(self)
            self.window._start_tab_drag(self, event.x_root, event.y_root)
            return True
        if event.button == 2:
            # Как в браузере: средний клик по вкладке закрывает её.
            self.window.close_tab(self)
            return True
        return False

    # --------------------------------------------------------- перетаскивание

    def _on_tab_motion(self, widget, event) -> bool:
        return self.window._update_tab_drag(self, widget, event)

    def _on_tab_release(self, widget, event) -> bool:
        if event.button != 1:
            return False
        return self.window._finish_tab_drag(self, widget)

    def is_pristine(self) -> bool:
        """Пустой лист без пути и изменений — его можно занять файлом."""
        if self.path is not None or self.buffer.get_modified():
            return False
        start, end = self.buffer.get_bounds()
        return not self.buffer.get_text(start, end, True)


class FontSizeEntry(Gtk.Entry):
    """Поле размера шрифта без встроенных стрелок Gtk.SpinButton.

    Стрелки спинора остаются в HIS-дереве невидимыми кнопками и проявляются
    при нажатии, дублируя кнопки «−» и «+». Обычное поле их не содержит.
    """

    __gsignals__ = {
        "value-changed": (GObject.SignalFlags.RUN_FIRST, None, ()),
    }

    MIN_SIZE = 8
    MAX_SIZE = 48

    def __init__(self, value: int) -> None:
        super().__init__()
        self._value = value
        self._guard = False
        self.set_width_chars(3)
        self.set_alignment(0.5)
        self.set_text(str(value))
        self.get_style_context().add_class("font-size-entry")
        self.connect("changed", self._on_text_changed)
        self.connect("focus-out-event", self._on_focus_out)
        self.connect("key-press-event", self._on_key_press)

    def get_value(self) -> int:
        return self._value

    def set_value(self, value: int | float) -> None:
        number = max(self.MIN_SIZE, min(self.MAX_SIZE, int(value)))
        text = str(number)
        if number == self._value and self.get_text() == text:
            return
        self._value = number
        self._guard = True
        self.set_text(text)
        self._guard = False
        self.emit("value-changed")

    def _on_text_changed(self, *_args) -> None:
        if self._guard:
            return
        try:
            number = int(self.get_text().strip())
        except ValueError:
            return
        if not self.MIN_SIZE <= number <= self.MAX_SIZE:
            return
        self._value = number
        self.emit("value-changed")

    def _on_focus_out(self, *_args) -> bool:
        self.set_value(self._value)
        return False

    def _on_key_press(self, _widget, event) -> bool:
        if event.keyval == Gdk.KEY_Up:
            self.set_value(self._value + 1)
            return True
        if event.keyval == Gdk.KEY_Down:
            self.set_value(self._value - 1)
            return True
        if event.keyval == Gdk.KEY_Return:
            self.set_value(self._value)
            return True
        return False


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application) -> None:
        super().__init__(application=application, title=APP_NAME)

        self._tabs: list[DocumentTab] = []
        self._active_tab: DocumentTab | None = None
        self._doc_counter = 0
        self._counts_timeout = 0
        self.undo_action = None
        self.redo_action = None
        self._font_size = DEFAULT_FONT_SIZE
        self._default_align = "left"
        self._format_guard = False
        self._font_size_guard = False
        self._settings_dialog: Gtk.Dialog | None = None
        self._settings_pending = False
        self._settings_size_spin: FontSizeEntry | None = None
        self._settings_align_combo: Gtk.ComboBoxText | None = None
        self._wrap_enabled = True
        self._line_numbers = False
        self._highlight_line = True
        self._spell_check = True
        self._com_drop: win_com_drop.ComFileDrop | None = None
        self._native_drop: win_drop.NativeFileDrop | None = None
        self._recent_drops: dict[tuple[str, ...], float] = {}

        config = user_settings.load()
        saved_size = config.get("font_size")
        if isinstance(saved_size, int) and (
            FontSizeEntry.MIN_SIZE <= saved_size <= FontSizeEntry.MAX_SIZE
        ):
            self._font_size = saved_size
        saved_align = str(config.get("default_align") or "").strip()
        if saved_align in {key for key, *_rest in ALIGNMENTS}:
            self._default_align = saved_align
        saved_theme = str(config.get("theme") or "").strip().lower()
        self._theme_preference = (
            saved_theme if saved_theme in ("light", "dark") else None
        )
        self._managed_ollama = ManagedOllama()
        self._backend_lock = threading.Lock()
        self._ollama_log_path = user_settings.config_dir() / "ollama-server.log"
        self._models_dir = str(config.get("models_dir") or "").strip()
        env_model = os.environ.get("RU_EDITOR_AI_MODEL")
        saved_model = str(config.get("model") or "").strip()
        self._model_name = env_model or saved_model or ai_check.DEFAULT_MODEL
        ai_check.configure(
            base_url=ai_check.DEFAULT_BASE_URL, model=self._model_name
        )

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
        self._update_busy = False
        self._update_downloaded: Path | None = None
        self._update_version = ""
        self._update_progress_time = 0.0
        self._destroyed = False
        self._header_bar: Gtk.HeaderBar | None = None
        self._drag_candidate: DocumentTab | None = None
        self._drag_tab: DocumentTab | None = None
        self._drag_start_x = 0.0
        self._drag_start_y = 0.0
        self._tab_animation: dict[str, object] | None = None
        self._dark_theme = (
            self._theme_preference == "dark"
            if self._theme_preference
            else self._detect_system_dark_theme()
        )

        self._window_geometry = self._validated_window_geometry(config.get("window"))
        self._geometry_restored = False
        if self._window_geometry is not None:
            _x, _y, width, height = self._window_geometry
            self.set_default_size(width, height)
        else:
            self.set_default_size(1000, 700)
        self._icon_name = app_icon_name()
        self.set_icon_name(self._icon_name)

        self._build_ui()
        self._create_tab()
        self._install_actions(application)
        self._connect_signals()
        self._install_focus_handling()
        self.connect("destroy", self._on_destroy)
        # В Windows системная GTK-тема не знает о тёмном варианте, поэтому
        # синхронизируем её и при следовании системной теме.
        if self._theme_preference is not None or os.name == "nt":
            self._sync_gtk_theme()
        self._apply_style_scheme()
        self._update_title()
        self._update_status()

    @staticmethod
    def _validated_window_geometry(value: object) -> tuple[int, int, int, int] | None:
        """Проверить сохранённые размеры и координаты окна."""
        if not isinstance(value, dict):
            return None
        try:
            x, y = int(value["x"]), int(value["y"])
            width, height = int(value["width"]), int(value["height"])
        except (KeyError, TypeError, ValueError):
            return None
        screen = Gdk.Screen.get_default()
        max_width = screen.get_width() if screen is not None else 3840
        max_height = screen.get_height() if screen is not None else 2160
        # Не ограничиваем x/y начальным экраном: на Windows и Linux второй
        # монитор слева или выше основного имеет отрицательные координаты.
        if not (-10000 <= x <= 10000 and -10000 <= y <= 10000):
            return None
        width = max(400, min(width, max_width))
        height = max(300, min(height, max_height))
        return x, y, width, height

    def _restore_window_geometry(self) -> None:
        """Вернуть размер и положение после первого показа окна."""
        if self._geometry_restored or self._window_geometry is None:
            return
        self._geometry_restored = True
        x, y, width, height = self._window_geometry
        self.resize(width, height)
        self.move(x, y)

    def _on_window_configure(self, _widget, event) -> bool:
        """Запомнить актуальные размеры и положение окна."""
        try:
            width, height = int(event.width), int(event.height)
            x, y = self.get_position()
            if width > 0 and height > 0:
                self._window_geometry = (x, y, width, height)
        except Exception:  # noqa: BLE001 - конфигурация не должна ломать окно
            pass
        return False

    def _save_window_geometry(self) -> None:
        """Записать геометрию в настройки, сохранив остальные ключи."""
        if self._window_geometry is None:
            try:
                width, height = self.get_size()
                x, y = self.get_position()
                self._window_geometry = (x, y, width, height)
            except Exception:  # noqa: BLE001
                return
        x, y, width, height = self._window_geometry
        config = user_settings.load()
        saved = config.get("window")
        window_state = dict(saved) if isinstance(saved, dict) else {}
        window_state.update({"x": x, "y": y, "width": width, "height": height})
        config["window"] = window_state
        user_settings.save(config)

    # ------------------------------------------------------------- вкладки

    @property
    def buffer(self) -> GtkSource.Buffer:
        assert self._active_tab is not None
        return self._active_tab.buffer

    @property
    def view(self) -> GtkSource.View:
        assert self._active_tab is not None
        return self._active_tab.view

    @property
    def path(self) -> Path | None:
        return self._active_tab.path if self._active_tab is not None else None

    @path.setter
    def path(self, value: Path | None) -> None:
        if self._active_tab is not None:
            self._active_tab.path = Path(value) if value is not None else None

    @property
    def encoding(self) -> str:
        return self._active_tab.encoding if self._active_tab is not None else "UTF-8"

    @encoding.setter
    def encoding(self, value: str) -> None:
        if self._active_tab is not None:
            self._active_tab.encoding = value

    @property
    def _align_tags(self) -> dict[str, Gtk.TextTag]:
        assert self._active_tab is not None
        return self._active_tab.align_tags

    @property
    def _current_align(self) -> str:
        return self._active_tab.current_align if self._active_tab is not None else "left"

    @_current_align.setter
    def _current_align(self, value: str) -> None:
        if self._active_tab is not None:
            self._active_tab.current_align = value

    @property
    def _align_guard(self) -> bool:
        return self._active_tab.align_guard if self._active_tab is not None else False

    @_align_guard.setter
    def _align_guard(self, value: bool) -> None:
        if self._active_tab is not None:
            self._active_tab.align_guard = value

    @property
    def spellcheck(self) -> SpellCheck:
        assert self._active_tab is not None
        return self._active_tab.spellcheck

    @property
    def _caret(self) -> CaretBlinker | None:
        return self._active_tab.caret if self._active_tab is not None else None

    @property
    def _popup_iter(self) -> Gtk.TextIter | None:
        return self._active_tab.popup_iter if self._active_tab is not None else None

    @_popup_iter.setter
    def _popup_iter(self, value: Gtk.TextIter | None) -> None:
        if self._active_tab is not None:
            self._active_tab.popup_iter = value

    @property
    def _counts_cache(self) -> tuple[int, int, int]:
        if self._active_tab is None:
            return (-1, -1, -1)
        return self._active_tab.counts_cache

    @_counts_cache.setter
    def _counts_cache(self, value: tuple[int, int, int]) -> None:
        if self._active_tab is not None:
            self._active_tab.counts_cache = value

    def _configure_view(self, view: GtkSource.View) -> None:
        """Общие настройки вида для новой вкладки."""
        font = Pango.FontDescription.from_string(
            f"{default_font_family()} {self._font_size}"
        )
        view.modify_font(font)
        # Номера строк по умолчанию выключены (включаются в меню «Вид»).
        view.set_show_line_numbers(self._line_numbers)
        view.set_highlight_current_line(self._highlight_line)
        view.set_auto_indent(True)
        view.set_tab_width(4)
        # Настоящая табуляция: Tab вставляет символ \t, а не четыре пробела.
        view.set_insert_spaces_instead_of_tabs(False)
        view.set_accepts_tab(True)
        view.set_wrap_mode(
            Gtk.WrapMode.WORD_CHAR if self._wrap_enabled else Gtk.WrapMode.NONE
        )
        view.set_right_margin_position(80)
        view.set_show_right_margin(False)
        # Поля вокруг текста: документ не прилипает к краям окна.
        view.set_left_margin(TEXT_MARGIN_X)
        view.set_right_margin(TEXT_MARGIN_X)
        view.set_top_margin(TEXT_MARGIN_Y)
        view.set_bottom_margin(TEXT_MARGIN_Y)
        try:
            view.set_smart_home_end(
                GtkSource.SmartHomeEndType.AFTER_FIRST_NON_WHITESPACE
            )
        except AttributeError:
            pass

    def _create_tab(self) -> DocumentTab:
        """Создаёт новую пустую вкладку и делает её активной."""
        # Нумерация относится к открытым документам: когда вкладок не осталось,
        # следующий документ снова становится «Документ 1».
        if not self._tabs:
            self._doc_counter = 0
        self._doc_counter += 1
        tab = DocumentTab(self, self._doc_counter)
        tab.spellcheck.set_enabled(self._spell_check)
        self._tabs.append(tab)
        self.tab_strip_row.show_all()
        self.bottom_bar.show_all()
        self.empty_state.hide()
        self._set_document_actions_enabled(True)
        self.tab_bar.pack_start(tab.tab, False, True, 0)
        # Все дети полосы упакованы через pack_start: так reorder_child
        # считает позиции обычным порядком, а «+» остаётся последним.
        self.tab_bar.reorder_child(tab.tab, len(self._tabs) - 1)
        self.stack.add(tab.scroller)
        self._connect_tab(tab)
        self._update_tab_label(tab)
        self._apply_style_scheme()
        self._select_tab(tab)
        self._set_alignment_buttons(tab.current_align)
        return tab

    # ------------------------------------------- порядок и перетаскивание вкладок

    def _start_tab_drag(self, tab: DocumentTab, x_root: float, y_root: float) -> None:
        """Запомнить нажатие: перетаскивание начнётся после сдвига мыши."""
        self._stop_tab_animation()
        self._drag_candidate = tab
        self._drag_start_x = float(x_root)
        self._drag_start_y = float(y_root)
        self._drag_tab = None

    def _update_tab_drag(self, tab: DocumentTab, widget: Gtk.Widget, event) -> bool:
        if self._drag_candidate is not tab:
            return False
        if not event.state & Gdk.ModifierType.BUTTON1_MASK:
            self._finish_tab_drag(tab, widget)
            return False

        x_root, y_root = float(event.x_root), float(event.y_root)
        if self._drag_tab is None:
            moved = max(
                abs(x_root - self._drag_start_x),
                abs(y_root - self._drag_start_y),
            )
            if moved < TAB_DRAG_THRESHOLD:
                return False
            self._drag_tab = tab
            # Grab удерживает события на плашке, даже когда курсор вышел за неё.
            if widget.get_realized() and not widget.has_grab():
                widget.grab_add()
            tab.tab.get_style_context().add_class("dragging")
            tab.tab.queue_draw()

        gdk_window = self.tab_bar.get_window()
        if gdk_window is None:
            return True
        root_x, _root_y = gdk_window.get_root_coords(0, 0)
        position = self._drop_position_at(x_root - root_x)
        old_index = self._tabs.index(tab)
        new_index = position if position <= old_index else position - 1
        self._move_tab(tab, new_index)
        return True

    def _finish_tab_drag(self, tab: DocumentTab, widget: Gtk.Widget) -> bool:
        if self._drag_candidate is not tab:
            return False
        if self._drag_tab is not None:
            if widget.get_realized() and widget.has_grab():
                widget.grab_remove()
            tab.tab.get_style_context().remove_class("dragging")
            tab.tab.queue_draw()
        self._drag_candidate = None
        self._drag_tab = None
        return True

    def _cancel_tab_drag(self, tab: DocumentTab | None = None) -> None:
        """Сбросить состояние перетаскивания при закрытии вкладки или окна."""
        dragged = self._drag_tab or self._drag_candidate
        if dragged is not None and (tab is None or dragged is tab):
            if dragged.tab.get_realized() and dragged.tab.has_grab():
                dragged.tab.grab_remove()
            dragged.tab.get_style_context().remove_class("dragging")
            dragged.tab.queue_draw()
        if tab is None or self._drag_candidate is tab:
            self._drag_candidate = None
        if tab is None or self._drag_tab is tab:
            self._drag_tab = None

    def _drop_position_at(self, x: float) -> int:
        """Позиция вставки: 0..len(tabs), где len — после последней вкладки."""
        for index, tab in enumerate(self._tabs):
            allocation = tab.tab.get_allocation()
            if x < allocation.x + allocation.width / 2:
                return index
        return len(self._tabs)

    def _move_tab(self, tab: DocumentTab, new_index: int) -> bool:
        """Переставить вкладку в списке и в полосе, сохранив активную."""
        if tab not in self._tabs:
            return False
        old_index = self._tabs.index(tab)
        new_index = max(0, min(new_index, len(self._tabs) - 1))
        if new_index == old_index:
            return False

        previous_order = list(self._tabs)
        self._tabs.remove(tab)
        self._tabs.insert(new_index, tab)
        self.tab_bar.reorder_child(tab.tab, new_index)
        self._animate_tab_reorder(previous_order)
        # Разделители зависят от соседей, поэтому перерисовываем все вкладки.
        for candidate in self._tabs:
            candidate.refresh_visuals()
        return True

    def _stop_tab_animation(self) -> None:
        """Прервать скольжение вкладок, вернув их в состояния по раскладке."""
        animation = self._tab_animation
        if animation is None:
            return
        source = animation["source"]
        if source is not None:
            GLib.source_remove(int(source))
        self._tab_animation = None
        origin = self._tab_layout_origin()
        if origin is not None:
            for tab, target_x in self._tab_layout_x(origin[0]).items():
                # Сбрасываем временное смещение, оставленное анимацией.
                self._place_tab_window(tab, target_x)

    def _tab_layout_origin(self) -> tuple[float, float] | None:
        """Корневая координата первой вкладки в текущей раскладке."""
        bar_window = self.tab_bar.get_window()
        if bar_window is None or not self._tabs:
            return None
        root_x, root_y = bar_window.get_root_coords(0, 0)
        # После reorder_child GTK ещё не пересчитал раскладку: у первой вкладки
        # в списке может остаться старый x. Минимальный x и есть начало полосы.
        start_x = min(tab.tab.get_allocation().x for tab in self._tabs)
        return float(root_x + start_x), float(root_y)

    def _tab_layout_x(self, origin: float) -> dict[DocumentTab, float]:
        """Куда вкладки должны встать после завершения перестановки."""
        positions: dict[DocumentTab, float] = {}
        x = origin
        for candidate in self._tabs:
            positions[candidate] = x
            x += candidate.tab.get_allocated_width()
        return positions

    def _place_tab_window(self, tab: DocumentTab, desired_x: float | None) -> None:
        """Сместить GDK-окно вкладки относительно её места в раскладке."""
        window = tab.tab.get_window()
        if window is None:
            return
        current_x, _ = window.get_root_coords(0, 0)
        rel_x, rel_y = window.get_position()
        if desired_x is None:
            offset = 0.0
        else:
            offset = desired_x - current_x
        if abs(offset) >= 1.0:
            window.move(rel_x + int(offset), rel_y)

    def _animate_tab_reorder(self, previous_order: list[DocumentTab]) -> None:
        """Плавно сдвинуть вкладки из старых позиций в новые."""
        # Сначала фиксируем то, где вкладки видны сейчас: при прерванной
        # анимации это промежуточные позиции, а не позиции из раскладки.
        start_positions: dict[DocumentTab, float] = {}
        for tab in previous_order:
            window = tab.tab.get_window()
            if window is not None:
                start_positions[tab] = float(window.get_root_coords(0, 0)[0])

        self._stop_tab_animation()
        origin = self._tab_layout_origin()
        if origin is None:
            return
        targets = self._tab_layout_x(origin[0])
        frames: list[tuple[DocumentTab, float, float]] = []
        for tab, start_x in start_positions.items():
            if tab in targets and abs(start_x - targets[tab]) >= 1.0:
                frames.append((tab, start_x, targets[tab]))
        if not frames:
            return
        self._tab_animation = {
            "source": None,
            "start": time.monotonic(),
            "frames": frames,
            "positions": {},
        }
        source = GLib.timeout_add(TAB_ANIMATION_STEP_MS, self._animate_tab_step)
        self._tab_animation["source"] = source
        # GTK переложит вкладки в новые места уже в следующем кадре. Сразу
        # вернём их на старые позиции, чтобы не было рывка перед анимацией.
        self._animate_tab_step()

    def _animate_tab_step(self) -> bool:
        animation = self._tab_animation
        if animation is None:
            return False

        elapsed_ms = (time.monotonic() - float(animation["start"])) * 1000
        progress = min(1.0, elapsed_ms / TAB_ANIMATION_MS)
        eased = progress * progress * (3.0 - 2.0 * progress)
        positions = animation["positions"]  # type: ignore[assignment]
        for tab, start_x, target_x in animation["frames"]:  # type: ignore[misc]
            desired = target_x + (start_x - target_x) * (1.0 - eased)
            positions[tab] = desired  # type: ignore[index]
            self._place_tab_window(tab, desired)
        if progress >= 1.0:
            self._tab_animation = None
            return False
        return True

    def _on_tab_size_allocate(self, tab: DocumentTab) -> None:
        """Не дать GTK переложить вкладку в новый ряд посреди анимации."""
        animation = self._tab_animation
        if animation is None:
            return
        positions = animation["positions"]  # type: ignore[assignment]
        desired = positions.get(tab)  # type: ignore[union-attr]
        if desired is not None:
            self._place_tab_window(tab, float(desired))

    def _tab_for_buffer(self, buffer: Gtk.TextBuffer) -> DocumentTab | None:
        for tab in self._tabs:
            if tab.buffer is buffer:
                return tab
        return None

    def _connect_tab(self, tab: DocumentTab) -> None:
        """Сигналы буфера и вида одной вкладки."""
        tab.tab.connect("size-allocate", lambda *_: self._on_tab_size_allocate(tab))
        tab.buffer.connect(
            "notify::cursor-position",
            lambda *_: self._on_tab_cursor(tab),
        )
        tab.buffer.connect("modified-changed", lambda *_: self._on_tab_modified(tab))
        tab.buffer.connect("changed", lambda *_: self._on_tab_changed(tab))
        try:
            tab.buffer.connect("can-undo-changed", lambda *_: self._on_tab_undo(tab))
            tab.buffer.connect("can-redo-changed", lambda *_: self._on_tab_undo(tab))
        except TypeError:
            pass

        # Файл можно бросить и прямо в область ввода, а не только в шапку.
        self._setup_drop_target(tab.view)
        self._setup_drop_target(tab.scroller)

        tab.view.connect("button-press-event", self._on_view_button_press)
        tab.view.connect("populate-popup", self._on_populate_popup)
        tab.buffer.connect_after("insert-text", self._on_insert_text)

    def _on_tab_strip_clicked(self, _widget, event) -> bool:
        """Средний клик по свободному месту полосы открывает вкладку."""
        if event.button == 2:
            self.action_new()
            return True
        return False

    def _on_tab_cursor(self, tab: DocumentTab) -> None:
        if tab is self._active_tab:
            self._update_status()
            self._update_format_controls()

    def _on_tab_modified(self, tab: DocumentTab) -> None:
        self._update_tab_label(tab)
        if tab is self._active_tab:
            self._update_title()

    def _on_tab_changed(self, tab: DocumentTab) -> None:
        if tab is self._active_tab:
            self._schedule_counts()

    def _on_tab_undo(self, tab: DocumentTab) -> None:
        if tab is self._active_tab:
            self._update_undo()

    def _activate_tab(self, tab: DocumentTab | None) -> None:
        if tab is None or tab not in self._tabs:
            return
        self._active_tab = tab
        self.stack.set_visible_child(tab.scroller)
        for candidate in self._tabs:
            context = candidate.tab.get_style_context()
            if candidate is tab:
                context.add_class("active")
            else:
                context.remove_class("active")
            candidate.refresh_visuals()
        self._update_title()
        self.enc_label.set_text(tab.encoding)
        self._update_status()
        # Счётчики у каждой вкладки свои: пересчитываем при переключении.
        tab.counts_cache = (-1, -1, -1)
        self._update_counts()
        self._update_undo()
        self._update_format_controls()
        tab.view.grab_focus()

    def _select_tab(self, tab: DocumentTab) -> None:
        self._activate_tab(tab)

    def _update_tab_label(self, tab: DocumentTab) -> None:
        name = tab.path.name if tab.path is not None else tab.default_name
        if tab.buffer.get_modified():
            name = f"• {name}"
        tab.name_label.set_text(name)
        tab.tab.set_tooltip_text(
            str(tab.path) if tab.path is not None else tab.default_name
        )

    def close_tab(self, tab: DocumentTab | None = None) -> bool:
        """Закрывает вкладку, спрашивая о несохранённых изменениях."""
        tab = tab or self._active_tab
        if tab is None or tab not in self._tabs:
            return False
        if not self._confirm_discard(tab):
            return False

        index = self._tabs.index(tab)
        self._stop_tab_animation()
        self._cancel_tab_drag(tab)
        if tab.caret is not None:
            tab.caret.stop()
        self._tabs.remove(tab)
        self.tab_bar.remove(tab.tab)
        self.stack.remove(tab.scroller)

        if not self._tabs:
            # Вместо нового чистого листа показываем экран с действиями:
            # пользователь сам решит, создать документ или открыть файл.
            self._show_empty_state()
            return True

        if self._active_tab is tab:
            self._active_tab = None
            self._select_tab(self._tabs[min(index, len(self._tabs) - 1)])
        return True

    def _set_document_actions_enabled(self, enabled: bool) -> None:
        """Не давать команды работы с текстом, когда вкладок нет."""
        names = (
            "close-tab",
            "next-tab",
            "prev-tab",
            "save",
            "save-as",
            "undo",
            "redo",
            "cut",
            "copy",
            "paste",
            "delete",
            "select-all",
            "wrap",
            "line-numbers",
            "highlight-line",
            "spell-check",
        )
        for name in names:
            action = self.lookup_action(name)
            if action is not None:
                action.set_enabled(enabled)
        if not enabled:
            return
        spell_action = self.lookup_action("spell-check")
        if spell_action is not None and self._tabs:
            spell_action.set_enabled(
                self._spell_check and self._tabs[0].spellcheck.available
            )
        self._update_undo()

    def _show_empty_state(self) -> None:
        """Показать приглашение создать или открыть документ."""
        self._active_tab = None
        self.empty_state.show_all()
        self.stack.set_visible_child_name("empty")
        self.tab_strip_row.hide()
        self.bottom_bar.hide()
        self._set_document_actions_enabled(False)
        self._update_title()
        self._update_status()
        self._update_counts()
        self.enc_label.set_text("")
        self.empty_new_button.grab_focus()

    def _pristine_tab(self) -> DocumentTab | None:
        if self._active_tab is not None and self._active_tab.is_pristine():
            return self._active_tab
        return None

    # ------------------------------------------------------------------ UI

    def _setup_titlebar(self) -> None:
        """Своя строка заголовка в Linux.

        Cinnamon игнорирует _GTK_THEME_VARIANT и рисует рамку в цвете
        системной темы, поэтому цвет шапки нельзя поменять через X11.
        Собственный заголовок GTK окрашивается вместе с приложением.
        В Windows сохраняем нативную шапку — там её цвет задаёт DWM.
        """
        if os.name == "nt":
            return
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-decoration-layout", ":minimize,maximize,close")
        bar = Gtk.HeaderBar()
        bar.set_show_close_button(True)
        bar.set_title(APP_NAME)
        bar.get_style_context().add_class("app-titlebar")
        self.set_titlebar(bar)
        self._header_bar = bar

    def _build_ui(self) -> None:
        self._setup_titlebar()

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.add(box)

        box.pack_start(self._build_menubar(), False, False, 0)

        # Полоса вкладок сразу под меню. «+» стоит вплотную за последней
        # вкладкой, поэтому сама полоса занимает ровно свою ширину,
        # а фон тянется на всю ширину окна.
        strip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        strip_row.get_style_context().add_class("tab-strip-row")
        self.tab_strip_row = strip_row

        # У самого Box нет своего GDK-окна, поэтому средний клик по свободному
        # месту полосы ловит EventBox, растянутый на всю ширину строки.
        self.tab_strip_event = Gtk.EventBox()
        self.tab_strip_event.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.tab_strip_event.connect(
            "button-press-event", self._on_tab_strip_clicked
        )
        # У EventBox своё GDK-окно: без явной цели бросок на полосу вкладок
        # может не дойти до главного окна.
        self._setup_drop_target(self.tab_strip_event)

        strip = Gtk.ScrolledWindow()
        strip.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        strip.set_propagate_natural_height(True)
        strip.set_propagate_natural_width(True)
        strip.get_style_context().add_class("tab-strip-scroller")

        self.tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        self.tab_bar.get_style_context().add_class("tab-strip")

        plus_icon = Gtk.Image.new_from_icon_name(
            "list-add-symbolic", Gtk.IconSize.MENU
        )
        # Видимый крест высотой 11 px — как заглавная буква в тексте вкладки.
        plus_icon.set_pixel_size(13)
        self.new_tab_button = Gtk.Button()
        self.new_tab_button.set_image(plus_icon)
        self.new_tab_button.set_relief(Gtk.ReliefStyle.NONE)
        self.new_tab_button.set_tooltip_text("Новая вкладка (Ctrl+N)")
        self.new_tab_button.get_style_context().add_class("new-tab")
        self.new_tab_button.connect("clicked", lambda *_: self.action_new())
        self.tab_bar.pack_start(self.new_tab_button, False, False, 0)

        strip.add(self.tab_bar)
        self.tab_strip_event.add(strip)
        strip_row.pack_start(self.tab_strip_event, True, True, 0)
        box.pack_start(strip_row, False, False, 0)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.NONE)
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        self.empty_state = self._build_empty_state()
        self.stack.add_named(self.empty_state, "empty")
        box.pack_start(self.stack, True, True, 0)

        self.search_bar = self._build_search_bar()
        box.pack_start(self.search_bar, False, False, 0)

        self.bottom_bar = self._build_bottom_bar()
        box.pack_start(self.bottom_bar, False, False, 0)
        box.pack_start(self._build_statusbar(), False, False, 0)

    def _build_search_bar(self) -> Gtk.Box:
        """Панель поиска, появляющаяся по Ctrl+F."""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("search-bar")
        # Иначе window.show_all() при запуске показывает скрытую панель.
        bar.set_no_show_all(True)
        bar.set_margin_start(6)
        bar.set_margin_end(6)
        bar.set_margin_top(2)
        bar.set_margin_bottom(2)

        entry = Gtk.SearchEntry()
        entry.get_style_context().add_class("search-field")
        entry.set_placeholder_text("Поиск")
        entry.set_width_chars(24)
        entry.connect("changed", self._on_search_changed)
        entry.connect("key-press-event", self._on_search_key)
        entry.connect("next-match", lambda *_: self._find_next())
        entry.connect("previous-match", lambda *_: self._find_previous())
        entry.connect("stop-search", lambda *_: self._close_search_bar())
        self.search_entry = entry
        bar.pack_start(entry, False, False, 0)

        self.search_count = Gtk.Label(label="")
        self.search_count.set_width_chars(9)
        self.search_count.set_xalign(0.0)
        bar.pack_start(self.search_count, False, False, 0)

        def button(icon: str, tooltip: str, handler) -> Gtk.Button:
            item = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.MENU)
            item.set_relief(Gtk.ReliefStyle.NONE)
            item.set_tooltip_text(tooltip)
            item.connect("clicked", handler)
            return item

        bar.pack_start(
            button("go-up-symbolic", "Предыдущее совпадение (Shift+Enter)", self._on_search_previous),
            False,
            False,
            0,
        )
        bar.pack_start(
            button("go-down-symbolic", "Следующее совпадение (Enter)", self._on_search_next),
            False,
            False,
            0,
        )
        bar.pack_start(
            button("window-close-symbolic", "Закрыть поиск (Esc)", self._on_search_close),
            False,
            False,
            0,
        )

        bar.show_all()
        bar.hide()
        return bar

    def _build_empty_state(self) -> Gtk.Box:
        """Экран, который показывается, когда закрыты все вкладки."""
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("empty-state")
        outer.set_hexpand(True)
        outer.set_vexpand(True)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_halign(Gtk.Align.CENTER)
        content.set_valign(Gtk.Align.CENTER)
        outer.pack_start(content, True, True, 0)

        logo = app_logo()
        if logo is not None:
            image = Gtk.Image.new_from_pixbuf(
                logo.scale_simple(
                    96, 96, GdkPixbuf.InterpType.BILINEAR
                )
            )
        else:
            image = Gtk.Image.new_from_icon_name(
                app_icon_name(), Gtk.IconSize.DIALOG
            )
        image.set_margin_bottom(8)
        content.pack_start(image, False, False, 0)

        title = Gtk.Label(label="Нет открытых документов")
        title.get_style_context().add_class("empty-state-title")
        content.pack_start(title, False, False, 0)

        hint = Gtk.Label(
            label="Создайте новый документ или откройте существующий файл."
        )
        hint.get_style_context().add_class("empty-state-hint")
        content.pack_start(hint, False, False, 0)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        buttons.set_halign(Gtk.Align.CENTER)
        buttons.set_margin_top(10)

        self.empty_new_button = Gtk.Button(label="Новый документ")
        self.empty_new_button.set_image(
            Gtk.Image.new_from_icon_name(
                "document-new-symbolic", Gtk.IconSize.BUTTON
            )
        )
        self.empty_new_button.set_always_show_image(True)
        self.empty_new_button.get_style_context().add_class("empty-state-button")
        self.empty_new_button.connect("clicked", lambda *_: self.action_new())

        self.empty_open_button = Gtk.Button(label="Открыть существующий")
        self.empty_open_button.set_image(
            Gtk.Image.new_from_icon_name(
                "document-open-symbolic", Gtk.IconSize.BUTTON
            )
        )
        self.empty_open_button.set_always_show_image(True)
        self.empty_open_button.get_style_context().add_class("empty-state-button")
        self.empty_open_button.connect("clicked", lambda *_: self.action_open())

        buttons.pack_start(self.empty_new_button, False, False, 0)
        buttons.pack_start(self.empty_open_button, False, False, 0)
        content.pack_start(buttons, False, False, 0)
        return outer

    def _build_bottom_bar(self) -> Gtk.Box:
        # Внешняя полоса занимает всю ширину окна и несёт рамку и фон.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        bar.get_style_context().add_class("bottom-bar")

        # Содержимое панели стоит по центру полосы.
        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.set_halign(Gtk.Align.CENTER)
        bar.pack_start(content, True, True, 0)

        content.pack_start(Gtk.Label(label="Шрифт:"), False, False, 0)

        # Плюс и минус стоят рядом, сразу за ними — поле с размером шрифта.
        font_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.font_larger = self._tool_button(
            "zoom-in-symbolic", "Увеличить шрифт выделенного текста"
        )
        self.font_larger.connect("clicked", lambda *_: self._change_font_size(1))
        font_buttons.pack_start(self.font_larger, False, False, 0)

        self.font_smaller = self._tool_button(
            "zoom-out-symbolic", "Уменьшить шрифт выделенного текста"
        )
        self.font_smaller.connect("clicked", lambda *_: self._change_font_size(-1))
        font_buttons.pack_start(self.font_smaller, False, False, 0)

        content.pack_start(font_buttons, False, False, 0)

        self.font_size_spin = FontSizeEntry(self._font_size)
        self.font_size_spin.set_tooltip_text("Размер шрифта (меняет выделенный текст)")
        self.font_size_spin.connect("value-changed", self._on_font_size_changed)
        content.pack_start(self.font_size_spin, False, False, 0)

        content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4
        )

        # Три начертания — отдельные кнопки; буква сама показывает действие.
        format_buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        self.bold_button = self._format_toggle(
            "B", "<b>B</b>", "Жирный (Ctrl+B)", "bold"
        )
        self.italic_button = self._format_toggle(
            "I", "<i>I</i>", "Наклонный (Ctrl+I)", "italic"
        )
        self.underline_button = self._format_toggle(
            "U", "<u>U</u>", "Подчёркнутый (Ctrl+U)", "underline"
        )
        for button in (self.bold_button, self.italic_button, self.underline_button):
            format_buttons.pack_start(button, False, False, 0)
        content.pack_start(format_buttons, False, False, 0)

        content.pack_start(
            Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 4
        )

        self.align_buttons: dict[str, Gtk.ToggleButton] = {}
        for key, icon, _alignment, tooltip in ALIGNMENTS:
            button = Gtk.ToggleButton()
            button.set_relief(Gtk.ReliefStyle.NONE)
            button.set_image(
                Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR)
            )
            button.set_tooltip_text(tooltip)
            button.set_active(key == self._default_align)
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

    def _format_toggle(
        self, letter: str, markup: str, tooltip: str, key: str
    ) -> Gtk.ToggleButton:
        """Кнопка начертания: буква сразу показывает, что она делает."""
        button = Gtk.ToggleButton(label=letter)
        child = button.get_child()
        if isinstance(child, Gtk.Label):
            child.set_markup(markup)
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.get_style_context().add_class("format-button")
        button.set_tooltip_text(tooltip)
        button.connect("toggled", self._on_format_toggled, key)
        return button

    def _build_menubar(self) -> Gtk.MenuBar:
        menubar = Gtk.MenuBar()
        self.menubar = menubar

        file_item = Gtk.MenuItem.new_with_mnemonic("_Файл")
        file_menu = Gtk.Menu()
        file_item.set_submenu(file_menu)
        file_menu.append(self._item("_Новый", "win.new"))
        file_menu.append(self._item("_Открыть…", "win.open"))
        file_menu.append(Gtk.SeparatorMenuItem())
        file_menu.append(self._item("_Закрыть вкладку", "win.close-tab"))
        file_menu.append(self._item("С_ледующая вкладка", "win.next-tab"))
        file_menu.append(self._item("Пре_дыдущая вкладка", "win.prev-tab"))
        file_menu.append(Gtk.SeparatorMenuItem())
        file_menu.append(self._item("_Сохранить", "win.save"))
        file_menu.append(self._item("Сохранить _как…", "win.save-as"))
        file_menu.append(Gtk.SeparatorMenuItem())
        file_menu.append(self._item("_Выход", "win.quit"))
        install_hover(file_item)
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
        install_hover(edit_item)
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
        view_menu.append(Gtk.SeparatorMenuItem())
        view_menu.append(self._check_item("_Тёмная тема", "win.dark-theme"))
        install_hover(view_item)
        menubar.append(view_item)

        ai_item = Gtk.MenuItem.new_with_mnemonic("_ИИ")
        ai_menu = Gtk.Menu()
        ai_item.set_submenu(ai_menu)
        ai_menu.append(self._item("_Скачать модель", "win.ai-download"))
        ai_menu.append(
            self._item("Указать _папку для моделей", "win.ai-models-dir")
        )
        ai_menu.append(
            self._item("Открыть папку _с моделями", "win.ai-open-folder")
        )
        ai_menu.append(Gtk.SeparatorMenuItem())
        ai_menu.append(
            self._item("Использовать системную Ollama", "win.ai-system")
        )
        install_hover(ai_item)
        menubar.append(ai_item)

        # Пункт верхнего уровня без подменю в GTK активируется только со
        # второго клика: обрабатываем первое нажатие сами.
        settings_item = self._item("_Настройки", "win.settings")
        settings_item.connect("button-press-event", self._on_settings_pressed)
        install_hover(settings_item)
        menubar.append(settings_item)

        help_item = Gtk.MenuItem.new_with_mnemonic("_Справка")
        help_menu = Gtk.Menu()
        help_item.set_submenu(help_menu)
        about = Gtk.MenuItem.new_with_mnemonic("О _программе")
        about.connect("activate", self._on_about)
        help_menu.append(about)
        install_hover(help_item)
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

    @staticmethod
    def _on_settings_pressed(item: Gtk.MenuItem, event: Gdk.EventButton) -> bool:
        if event.button != 1:
            return False
        item.activate()
        return True

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

        self.version_button = Gtk.Button(label="Версия актуальна")
        self.version_button.set_relief(Gtk.ReliefStyle.NONE)
        self.version_button.set_tooltip_text("Проверить обновления")
        self.version_button.get_style_context().add_class("version-button")
        self.version_button.connect("clicked", self.action_check_updates)
        bar.pack_end(self.version_button, False, False, 0)
        return bar

    # -------------------------------------------------------------- actions

    def _install_actions(self, application: Gtk.Application) -> None:
        entries: list[tuple[str, object, list[str] | None]] = [
            ("new", self.action_new, ["<Primary>n"]),
            ("open", self.action_open, ["<Primary>o"]),
            ("settings", self.action_settings, []),
            ("close-tab", self.action_close_tab, ["<Primary>w"]),
            ("next-tab", self.action_next_tab, ["<Primary>Tab"]),
            ("prev-tab", self.action_prev_tab, ["<Primary><Shift>Tab"]),
            ("save", self.action_save, ["<Primary>s"]),
            ("save-as", self.action_save_as, ["<Primary><Shift>s"]),
            ("bold", self.action_bold, ["<Primary>b"]),
            ("italic", self.action_italic, ["<Primary>i"]),
            ("underline", self.action_underline, ["<Primary>u"]),
            ("undo", self.action_undo, ["<Primary>z"]),
            ("redo", self.action_redo, ["<Primary><Shift>z", "<Primary>y"]),
            ("cut", self.action_cut, ["<Primary>x"]),
            ("copy", self.action_copy, ["<Primary>c"]),
            ("paste", self.action_paste, ["<Primary>v"]),
            ("delete", self.action_delete, []),
            # Ctrl+F ставится через AccelGroup окна, а не через акселератор
            # приложения: на части Windows-сборок win-action не срабатывает.
            ("find", self.action_find, []),
            ("select-all", self.action_select_all, ["<Primary>a"]),
            ("quit", self.action_quit, ["<Primary>q"]),
            ("ai-download", self.action_ai_download, []),
            ("ai-models-dir", self.action_ai_models_dir, []),
            ("ai-open-folder", self.action_ai_open_folder, []),
            ("ai-system", self.action_ai_system, []),
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
            ("wrap", self._wrap_enabled, self._set_wrap),
            ("line-numbers", self._line_numbers, self._set_line_numbers),
            (
                "highlight-line",
                self._highlight_line,
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

        theme_action = Gio.SimpleAction.new_stateful(
            "dark-theme", None, GLib.Variant.new_boolean(self._dark_theme)
        )
        theme_action.connect("change-state", self._on_theme_state_changed)
        self.add_action(theme_action)

        if not self.spellcheck.available:
            self.lookup_action("spell-check").set_enabled(False)

    def _install_find_accelerator(self) -> None:
        """Ctrl+F через AccelGroup окна — надёжнее win-action в Windows."""
        group = Gtk.AccelGroup()
        # GTK сам приводит F при Caps Lock к `f`, отдельная привязка KEY_F
        # создаёт лишний акселератор, который срабатывает без Ctrl.
        group.connect(
            Gdk.KEY_f,
            Gdk.ModifierType.CONTROL_MASK,
            Gtk.AccelFlags.VISIBLE,
            self._on_find_accel,
        )
        self.add_accel_group(group)
        self._find_accel_group = group

    def _on_find_accel(self, *_args) -> bool:
        self.action_find()
        return True

    def _spell_enabled(self) -> bool:
        if not self._spell_check:
            return False
        if self._active_tab is None:
            return True
        return self.spellcheck.available

    def _set_wrap(self, enabled: bool) -> None:
        self._wrap_enabled = enabled
        mode = Gtk.WrapMode.WORD_CHAR if enabled else Gtk.WrapMode.NONE
        for tab in self._tabs:
            tab.view.set_wrap_mode(mode)

    def _set_line_numbers(self, enabled: bool) -> None:
        self._line_numbers = enabled
        for tab in self._tabs:
            tab.view.set_show_line_numbers(enabled)

    def _set_highlight_line(self, enabled: bool) -> None:
        self._highlight_line = enabled
        for tab in self._tabs:
            tab.view.set_highlight_current_line(enabled)

    def _set_spell_check(self, enabled: bool) -> None:
        self._spell_check = enabled
        for tab in self._tabs:
            tab.spellcheck.set_enabled(enabled)

    # ------------------------------------------------------------- formatting

    def _change_font_size(self, delta: int) -> None:
        # Кнопки меняют только значение поля; само оформление применяется
        # в _on_font_size_changed и только к выделенному тексту.
        self.font_size_spin.set_value(self.font_size_spin.get_value() + delta)
        self.view.grab_focus()

    def _apply_size_to_selection(self, size: int) -> None:
        """Применить размер только к выделенному тексту."""
        tab = self._active_tab
        if tab is None or not tab.buffer.get_has_selection():
            return
        buffer = tab.buffer
        start, end = buffer.get_selection_bounds()
        buffer.begin_user_action()
        try:
            for old_size, old_tag in list(tab.size_tags.items()):
                if old_size != size:
                    buffer.remove_tag(old_tag, start, end)
            buffer.apply_tag(self._get_size_tag(tab, size), start, end)
        finally:
            buffer.end_user_action()
        self._refresh_formatted(tab)

    def _on_font_size_changed(self, spin: FontSizeEntry) -> None:
        if self._font_size_guard:
            return
        size = int(spin.get_value())
        tab = self._active_tab
        if tab is not None and tab.buffer.get_has_selection():
            # Есть выделение — меняем только его, остальной текст не трогаем.
            self._apply_size_to_selection(size)
        elif tab is not None:
            # Без выделения размер действует лишь на последующий ввод.
            tab.active_size = size
        self._update_format_controls()

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

    def _alignment_at(
        self,
        buffer: Gtk.TextBuffer,
        tags: dict[str, Gtk.TextTag],
        offset: int,
    ) -> str | None:
        if offset < 0:
            return None
        iterator = buffer.get_iter_at_offset(offset)
        for key, tag in tags.items():
            if iterator.has_tag(tag):
                return key
        return None

    def _on_insert_text(self, buffer, location, text, _length) -> None:
        """Продлевает выравнивание и оформление на новый текст."""
        tab = self._tab_for_buffer(buffer)
        if tab is None:
            return
        tags = tab.align_tags
        end_offset = location.get_offset()
        start_offset = max(0, end_offset - len(text))
        start = buffer.get_iter_at_offset(start_offset)
        end = buffer.get_iter_at_offset(end_offset)

        # Оформление, включённое для выделения, должно действовать и на текст,
        # который придёт на место выделения.
        for key in TEXT_FORMAT_KEYS:
            if getattr(tab, f"{key}_typing"):
                buffer.apply_tag(self._format_tag(tab, key), start, end)
        if tab.active_size is not None:
            buffer.apply_tag(self._get_size_tag(tab, tab.active_size), start, end)
        if not text:
            return

        # В пустом документе тегов ещё нет: новый текст наследует выравнивание
        # по умолчанию, которое показывает панель.
        fallback = tab.current_align if tab.current_align != "left" else None

        if "\n" in text:
            # Enter: помечаем сам перевод строки, чтобы следующая пустая
            # строка унаследовала выравнивание предыдущей.
            key = self._alignment_at(buffer, tags, start_offset - 1) or fallback
            if key is not None:
                buffer.apply_tag(tags[key], start, end)
            return

        # Первый символ в пустой строке наследует выравнивание предыдущей;
        # текст в конце выровненной строки тоже нужно пометить.
        line_start = buffer.get_iter_at_offset(start_offset)
        line_start.set_line_offset(0)
        at_line_start = line_start.get_offset() == start_offset

        line_end = buffer.get_iter_at_offset(end_offset)
        line_end.forward_to_line_end()
        line_was_empty = at_line_start and line_end.get_offset() == end_offset

        if at_line_start and not line_was_empty:
            # Начало обычной строки не должно наследовать чужое выравнивание.
            return

        key = self._alignment_at(buffer, tags, start_offset - 1) or fallback
        if key is None:
            return

        if line_was_empty:
            line_end = buffer.get_iter_at_offset(start_offset)
            line_end.forward_line()
            buffer.apply_tag(tags[key], line_start, line_end)
        else:
            buffer.apply_tag(tags[key], start, end)

    def _sync_alignment_buttons(self) -> None:
        iterator = self.buffer.get_iter_at_mark(self.buffer.get_insert())
        # У непомеченного текста (в том числе в пустом новом документе)
        # действует выравнивание по умолчанию из настроек.
        current = self._default_align
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

    def _setup_drop_target(self, widget: Gtk.Widget) -> None:
        """Разрешить бросать файлы на окно, поле ввода и его прокрутку.

        В Windows GTK-цели не регистрируются: иначе GDK вызвал бы
        ``RegisterDragDrop()`` на том же HWND и перехватил бросок раньше нашего
        COM-приёмника. Файлы приходят в ``_on_map()`` через ``win_com_drop``
        и идут в тот же ``_open_dropped_uris()``.

        В остальных случаях проводник предлагает перетаскивание как Move и
        отдаёт путь файла не только как URI, но и как обычный текст. Поэтому
        цели задаём явно и действия не ограничиваем только Copy. Завершение
        перетаскивания делаем сами — без GTK_DEST_DEFAULT_DROP, чтобы один
        бросок не обрабатывался дважды.
        """
        if win_dnd.use_com_file_drop():
            return
        # Без флагов OTHER_APP/SAME_APP: в Windows источником считается
        # корневое окно, и проверка «другое приложение» может не пройти.
        targets = [
            Gtk.TargetEntry.new(name, Gtk.TargetFlags(0), info)
            for name, info in DND_TARGETS
        ]
        widget.drag_dest_set(
            Gtk.DestDefaults.MOTION | Gtk.DestDefaults.HIGHLIGHT,
            targets,
            Gdk.DragAction.COPY | Gdk.DragAction.MOVE | Gdk.DragAction.LINK,
        )
        # Даже если GDK Win32 не показал цели источника, всё равно получаем
        # drag-motion и отвечаем COPY — иначе бросок отклоняется.
        widget.drag_dest_set_track_motion(True)
        widget.connect("drag-motion", self._on_drag_motion)
        widget.connect("drag-drop", self._on_drag_drop)
        widget.connect("drag-data-received", self._on_drag_data)

    def _on_drag_motion(self, _widget, context, _x, _y, time) -> bool:
        """Принять бросок как копирование — курсор не должен быть «запрет»."""
        action = Gdk.DragAction.COPY
        if context is not None:
            try:
                actions = context.get_actions()
                if actions & Gdk.DragAction.COPY:
                    action = Gdk.DragAction.COPY
                elif actions & Gdk.DragAction.MOVE:
                    action = Gdk.DragAction.MOVE
                elif actions & Gdk.DragAction.LINK:
                    action = Gdk.DragAction.LINK
            except Exception:  # noqa: BLE001
                pass
        Gdk.drag_status(context, action, time)
        return True

    def _on_drag_drop(self, widget, context, _x, _y, time) -> bool:
        """Запросить данные броска у источника.

        Файл с рабочего стола или из папки должен открываться и когда его
        бросают прямо в поле ввода уже открытого документа. GDK Win32 не
        всегда сообщает цели источника, поэтому если URI есть в списке,
        запрашиваем именно его — иначе GtkTextView может забрать бросок
        как обычный текст и вставить путь в документ.
        """
        uri_atom = Gdk.atom_intern(DND_TARGETS[0][0], False)
        source_targets: list[Gdk.Atom] = []
        if context is not None:
            try:
                source_targets = list(context.list_targets())
            except Exception:  # noqa: BLE001
                source_targets = []
        target = uri_atom if uri_atom in source_targets else None
        if target is None:
            target = widget.drag_dest_find_target(context, None)
        if target is None:
            target = uri_atom
        widget.drag_get_data(context, target, time)
        return True

    def _connect_signals(self) -> None:
        self.connect("delete-event", self._on_delete_event)
        self.connect("map", self._on_map)
        self.connect("configure-event", self._on_window_configure)
        self._install_find_accelerator()

        # Перетаскивание файла в окно.
        self._setup_drop_target(self)

        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.connect("notify::gtk-theme-name", self._on_system_theme_changed)
            settings.connect(
                "notify::gtk-application-prefer-dark-theme",
                self._on_system_theme_changed,
            )

    def _update_undo(self) -> None:
        if self._active_tab is None or self.undo_action is None:
            return
        self.undo_action.set_enabled(self.buffer.can_undo())
        self.redo_action.set_enabled(self.buffer.can_redo())

    def _grab_active_focus(self) -> None:
        """Фокус в текст, а когда вкладок нет — на кнопку нового документа."""
        if self._active_tab is not None:
            self._active_tab.view.grab_focus()
        else:
            self.empty_new_button.grab_focus()

    def _install_focus_handling(self) -> None:
        """Клавиатурный фокус должен всегда попадать в текст, а не в меню."""
        self.set_focus(self.view)
        self.connect("map", lambda *_: self._grab_active_focus())
        self.connect("window-state-event", self._on_window_state)

    def _on_window_state(self, _widget, event) -> bool:
        if event.changed_mask & Gdk.WindowState.FOCUSED and event.new_window_state & Gdk.WindowState.FOCUSED:
            if self.get_focus() is None:
                self._grab_active_focus()
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
        if self._ai_pulling:
            self._show_message("Дождитесь окончания скачивания модели")
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
                f"Выгружаю модель {self._model_name} из памяти…", seconds=0
            )
            threading.Thread(target=self._ai_unload_worker, daemon=True).start()
        else:
            self._ai_enabled = True
            self._update_ai_button()
            self._show_message(
                "ИИ включён. Модель загрузится при первой проверке; "
                "скачать её можно в меню «ИИ» → «Скачать модель»."
            )

    def _ai_unload_worker(self) -> None:
        freed = 0
        error: str | None = None
        try:
            if self._models_dir:
                # Своя служба: выгружаем только если она уже запущена.
                base_url = self._managed_ollama.base_url
                if self._managed_ollama.running() and base_url:
                    ai_check.configure(base_url=base_url, model=self._model_name)
                    freed = ai_check.unload_model(self._model_name)
            else:
                ai_check.configure(
                    base_url=ai_check.DEFAULT_BASE_URL, model=self._model_name
                )
                freed = ai_check.unload_model(self._model_name)
        except ai_check.AIError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        finally:
            # Выключение ИИ останавливает и отдельную службу.
            self._managed_ollama.stop()
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
        if self._ai_pulling:
            self._show_message("Дождитесь окончания скачивания модели")
            return
        if self._ai_unloading:
            self._show_message("Модель ещё выгружается…")
            return
        if self._ai_busy:
            self._show_message("Проверка уже идёт…")
            return

        bounds = self.buffer.get_selection_bounds()
        if not bounds:
            self._show_message("Сначала выделите текст для проверки")
            return

        tab = self._active_tab
        if tab is None:
            return
        start, end = bounds
        text = tab.buffer.get_text(start, end, True)
        if not text.strip():
            self._show_message("Выделенный фрагмент пуст")
            return

        self._ai_busy = True
        self._show_message("Проверяю готовность ИИ…", seconds=0)
        threading.Thread(
            target=self._ai_probe_worker,
            args=(tab, text, start.get_offset()),
            daemon=True,
        ).start()

    # Модель может быть ещё не скачана. Перед проверкой узнаём её состояние
    # в фоне, чтобы не подвешивать окно. Скачивание запускается только вручную
    # из меню «ИИ», поэтому при отсутствии модели показываем подсказку.

    def _ensure_backend(self) -> str:
        """Активирует выбранную службу Ollama и возвращает её адрес.

        Вызывается из фоновых потоков: запуск отдельной службы занимает время.
        """
        with self._backend_lock:
            if self._models_dir:
                base_url = self._managed_ollama.ensure(
                    self._models_dir, log_path=self._ollama_log_path
                )
            else:
                self._managed_ollama.stop()
                base_url = ai_check.DEFAULT_BASE_URL
            ai_check.configure(base_url=base_url, model=self._model_name)
            return base_url

    def _ai_probe_worker(
        self, tab: DocumentTab, text: str, offset: int
    ) -> None:
        state = "offline"
        error: str | None = None
        try:
            self._ensure_backend()
            state = ai_check.model_state(self._model_name)
        except (ai_check.AIError, OllamaStartError) as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_probe_finished, tab, text, offset, state, error)

    def _ai_probe_finished(
        self,
        tab: DocumentTab,
        text: str,
        offset: int,
        state: str,
        error: str | None,
    ) -> bool:
        if not self.get_realized() or tab not in self._tabs:
            self._ai_busy = False
            return False

        if error is not None:
            self._ai_busy = False
            self._show_message("Ollama не запущена", seconds=10)
            self._show_notice("ИИ недоступен", error)
            return False

        if state == "offline":
            self._ai_busy = False
            self._show_message("Ollama не запущена", seconds=10)
            if self._models_dir:
                details = (
                    "Не удалось подключиться к отдельной службе Ollama.\n"
                    "Проверьте, что Ollama установлена: https://ollama.com"
                )
            else:
                details = (
                    "Не удалось подключиться к Ollama.\n"
                    "Запустите службу Ollama или выберите отдельную папку "
                    "моделей: меню «ИИ» → «Указать папку для моделей»."
                )
            self._show_notice("ИИ недоступен", details)
            return False

        if state == "missing":
            self._ai_busy = False
            self._show_message(
                f"Модель «{self._model_name}» не скачана. "
                "Меню «ИИ» → «Скачать модель»",
                seconds=15,
            )
            return False

        self._show_message(
            f"Проверяю текст с ИИ (модель {self._model_name})…", seconds=0
        )
        threading.Thread(
            target=self._ai_worker, args=(tab, text, offset), daemon=True
        ).start()
        return False

    # ------------------------------------------------------------- настройки

    def _set_alignment_buttons(self, key: str) -> None:
        """Показать выравнивание на панели, не меняя текст документа."""
        self._align_guard = True
        for other_key, button in self.align_buttons.items():
            button.set_active(other_key == key)
        self._align_guard = False

    def _retag_alignment(self, tab: DocumentTab, key: str) -> None:
        """Пометить весь текст вкладки выбранным выравниванием."""
        start, end = tab.buffer.get_bounds()
        for tag in tab.align_tags.values():
            tab.buffer.remove_tag(tag, start, end)
        if key != "left":
            tab.buffer.apply_tag(tab.align_tags[key], start, end)
        tab.current_align = key

    def _apply_preferences(self, font_size: int, align: str) -> None:
        """Применить настройки по умолчанию и сохранить их на диск."""
        self._font_size = font_size
        self._default_align = align
        font = Pango.FontDescription.from_string(
            f"{default_font_family()} {self._font_size}"
        )
        for tab in self._tabs:
            tab.view.modify_font(font)
            tab.active_size = None
            for key in TEXT_FORMAT_KEYS:
                setattr(tab, f"{key}_typing", False)
            tab.buffer.begin_user_action()
            try:
                self._retag_alignment(tab, align)
            finally:
                tab.buffer.end_user_action()
        # Это программная установка значения из настроек, а не действие
        # пользователя над выделением — оформление применять не нужно.
        self._font_size_guard = True
        try:
            self.font_size_spin.set_value(self._font_size)
        finally:
            self._font_size_guard = False
        self._set_alignment_buttons(align)

        config = user_settings.load()
        config["font_size"] = self._font_size
        config["default_align"] = self._default_align
        try:
            user_settings.save(config)
        except OSError as exc:
            self._show_message(f"Не удалось сохранить настройки: {exc}", seconds=12)

    def action_settings(self, *_args) -> None:
        """Открыть настройки после завершения обработки пункта меню."""
        if self._settings_dialog is not None:
            self._settings_dialog.present()
            return
        if self._settings_pending:
            return
        self._settings_pending = True
        # Пока меню удерживает grab, показанный сразу модальный диалог может
        # не появиться с первого клика — открываем его следующим циклом GTK.
        GLib.idle_add(self._open_settings_dialog)

    def _open_settings_dialog(self) -> bool:
        """Диалог настроек: размер шрифта и выравнивание по умолчанию."""
        self._settings_pending = False
        if self._settings_dialog is not None:
            self._settings_dialog.present()
            return False

        dialog = Gtk.Dialog(title="Настройки", transient_for=self, modal=True)
        dialog.set_default_size(400, -1)
        dialog.set_resizable(False)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Сохранить", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        self._watch_dark_titlebar(dialog)
        self._settings_dialog = dialog

        content = dialog.get_content_area()
        content.set_border_width(18)
        content.set_spacing(12)

        grid = Gtk.Grid(column_spacing=12, row_spacing=10)
        content.pack_start(grid, False, False, 0)

        size_label = Gtk.Label(label="Размер шрифта:")
        size_label.set_xalign(0.0)
        grid.attach(size_label, 0, 0, 1, 1)
        size_spin = FontSizeEntry(self._font_size)
        size_spin.set_tooltip_text("Размер шрифта по умолчанию (8–48)")
        grid.attach(size_spin, 1, 0, 1, 1)

        align_label = Gtk.Label(label="Выравнивание:")
        align_label.set_xalign(0.0)
        grid.attach(align_label, 0, 1, 1, 1)
        align_combo = Gtk.ComboBoxText()
        for key, _icon, _justification, title in ALIGNMENTS:
            align_combo.append(key, title)
        for index, (key, *_rest) in enumerate(ALIGNMENTS):
            if key == self._default_align:
                align_combo.set_active(index)
                break
        grid.attach(align_combo, 1, 1, 1, 1)

        hint = Gtk.Label(
            label="Значения используются в новых документах\n"
            "и применяются ко всем открытым вкладкам."
        )
        hint.set_xalign(0.0)
        hint.get_style_context().add_class("dim-label")
        content.pack_start(hint, False, False, 0)

        self._settings_size_spin = size_spin
        self._settings_align_combo = align_combo

        def on_response(dialog: Gtk.Dialog, response: int) -> None:
            if response == Gtk.ResponseType.OK:
                align = str(align_combo.get_active_id() or self._default_align)
                self._apply_preferences(int(size_spin.get_value()), align)
            self._settings_dialog = None
            self._settings_size_spin = None
            self._settings_align_combo = None
            dialog.destroy()

        dialog.connect("response", on_response)
        dialog.show_all()
        dialog.present()
        return False

    # ------------------------------------------------------- AI model setup

    def _save_ai_settings(self) -> None:
        """Сохраняет модель и папку в общий файл настроек."""
        config = user_settings.load()
        config["model"] = self._model_name
        config["models_dir"] = self._models_dir
        try:
            user_settings.save(config)
        except OSError as exc:
            self._show_message(f"Не удалось сохранить настройки: {exc}", seconds=12)

    def action_ai_download(self, *_args) -> None:
        """Ручное скачивание модели в выбранную папку."""
        if self._ai_unloading:
            self._show_message("Модель ещё выгружается…")
            return
        if self._ai_pulling:
            self._show_message("Скачивание уже идёт…")
            return
        if self._ai_busy:
            self._show_message("Дождитесь окончания проверки текста")
            return

        dialog = Gtk.Dialog(title="Скачать модель", transient_for=self, modal=True)
        dialog.set_default_size(600, -1)
        dialog.set_resizable(True)
        dialog.add_button("Отмена", Gtk.ResponseType.CANCEL)
        dialog.add_button("Скачать", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        self._watch_dark_titlebar(dialog)

        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)

        header = Gtk.Label()
        header.set_markup("<b>Скачивание модели Ollama</b>")
        header.set_xalign(0.0)
        content.pack_start(header, False, False, 0)

        explanation = Gtk.Label(
            label=(
                "Модель будет сохранена в выбранную папку. Для неё приложение "
                "запустит отдельную службу Ollama и не изменит системную."
            )
        )
        explanation.set_xalign(0.0)
        explanation.set_line_wrap(True)
        explanation.get_style_context().add_class("dim-label")
        content.pack_start(explanation, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=10)
        model_label = Gtk.Label(label="Модель:", xalign=0.0)
        model_combo = Gtk.ComboBoxText.new_with_entry()
        title_to_id: dict[str, str] = {}
        for model_id, model_title in MODEL_CHOICES:
            model_combo.append(model_id, model_title)
            title_to_id[model_title] = model_id
        model_combo.set_hexpand(True)
        model_entry = model_combo.get_child()
        model_entry.set_placeholder_text("выберите из списка или введите название")
        if not model_combo.set_active_id(self._model_name):
            model_entry.set_text(self._model_name)
        grid.attach(model_label, 0, 0, 1, 1)
        grid.attach(model_combo, 1, 0, 1, 1)

        dir_label = Gtk.Label(label="Папка для моделей:", xalign=0.0)
        dir_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        dir_entry = Gtk.Entry()
        dir_entry.set_text(self._models_dir or str(default_models_dir()))
        dir_entry.set_hexpand(True)
        choose_button = Gtk.Button(label="Выбрать…")
        choose_button.connect(
            "clicked", self._choose_models_folder, dir_entry, dialog
        )
        dir_box.pack_start(dir_entry, True, True, 0)
        dir_box.pack_start(choose_button, False, False, 0)
        grid.attach(dir_label, 0, 1, 1, 1)
        grid.attach(dir_box, 1, 1, 1, 1)
        content.pack_start(grid, False, False, 0)

        hint = Gtk.Label(
            label=(
                "Выберите готовый вариант или введите название модели вручную. "
                "Размер задаёт Ollama; скачивание может занять несколько минут, "
                "его можно отменить."
            )
        )
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("dim-label")
        content.pack_start(hint, False, False, 0)

        error_label = Gtk.Label()
        error_label.set_xalign(0.0)
        error_label.set_line_wrap(True)
        error_label.set_no_show_all(True)
        content.pack_start(error_label, False, False, 0)

        def on_response(dlg: Gtk.Dialog, response: int) -> None:
            if response != Gtk.ResponseType.OK:
                dlg.destroy()
                return
            model = model_entry.get_text().strip()
            # ComboBoxText с полем ввода подставляет в него подпись варианта,
            # поэтому выбранную подпись превращаем обратно в имя модели.
            model = title_to_id.get(model, model)
            directory = dir_entry.get_text().strip()
            if not model:
                error_label.set_text("Укажите название модели.")
                error_label.show()
                return
            if not directory:
                error_label.set_text("Укажите папку для моделей.")
                error_label.show()
                return
            path = Path(directory).expanduser()
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                error_label.set_text(f"Не удалось создать папку:\n{exc}")
                error_label.show()
                return
            if not path.is_dir():
                error_label.set_text("Выбранный путь не является папкой.")
                error_label.show()
                return
            dlg.destroy()
            self._model_name = model
            self._models_dir = str(path)
            self._save_ai_settings()
            self._start_model_pull()

        dialog.connect("response", on_response)
        dialog.show_all()

    def _choose_models_folder(
        self, _button: Gtk.Button, entry: Gtk.Entry, parent: Gtk.Window
    ) -> None:
        chooser = Gtk.FileChooserNative.new(
            "Папка для моделей",
            parent,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Выбрать",
            "_Отмена",
        )
        self._watch_dark_titlebar(chooser)
        current = entry.get_text().strip()
        if current:
            path = Path(current).expanduser()
            if path.is_dir():
                chooser.set_current_folder(str(path))
        response = chooser.run()
        filename = chooser.get_filename()
        chooser.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            entry.set_text(filename)

    def action_ai_models_dir(self, *_args) -> None:
        """Выбор папки, в которой хранятся модели отдельной службы."""
        if self._ai_unloading:
            self._show_message("Модель ещё выгружается…")
            return
        if self._ai_pulling:
            self._show_message("Дождитесь окончания скачивания модели")
            return
        if self._ai_busy:
            self._show_message("Дождитесь окончания проверки текста")
            return

        chooser = Gtk.FileChooserNative.new(
            "Папка для моделей Ollama",
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Выбрать",
            "_Отмена",
        )
        self._watch_dark_titlebar(chooser)
        current = self._models_dir or str(default_models_dir())
        path = Path(current).expanduser()
        if path.is_dir():
            chooser.set_current_folder(str(path))
        response = chooser.run()
        filename = chooser.get_filename()
        chooser.destroy()
        if response == Gtk.ResponseType.ACCEPT and filename:
            self._apply_models_dir(filename)

    def _apply_models_dir(self, filename: str) -> None:
        path = Path(filename).expanduser()
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._show_notice("Папка моделей", f"Не удалось создать папку:\n{exc}")
            return
        if not path.is_dir():
            self._show_notice("Папка моделей", "Выбранный путь не является папкой.")
            return

        self._models_dir = str(path)
        self._save_ai_settings()
        # Следующая операция запустит службу уже с новой папкой.
        self._managed_ollama.stop()
        self._show_message(
            f"Папка моделей: {path}. "
            "Скачайте модель: меню «ИИ» → «Скачать модель»",
            seconds=15,
        )

    def action_ai_open_folder(self, *_args) -> None:
        """Открывает папку с моделями в файловом менеджере."""
        if not self._models_dir:
            self._show_notice(
                "Папка моделей",
                "Папка не выбрана — используется системная Ollama.\n"
                "Выберите папку: меню «ИИ» → «Указать папку для моделей».",
            )
            return

        path = Path(self._models_dir).expanduser()
        if not path.is_dir():
            self._show_notice(
                "Папка моделей", f"Папка не найдена:\n{path}"
            )
            return
        try:
            opened = Gtk.show_uri_on_window(
                self, path.resolve().as_uri(), Gdk.CURRENT_TIME
            )
        except GLib.Error as exc:
            self._show_notice(
                "Папка моделей", f"Не удалось открыть папку:\n{exc.message}"
            )
            return
        if not opened:
            self._show_notice(
                "Папка моделей",
                f"Не удалось открыть папку в файловом менеджере:\n{path}",
            )

    def action_ai_system(self, *_args) -> None:
        """Переключает проверку на системную Ollama."""
        if self._ai_unloading:
            self._show_message("Модель ещё выгружается…")
            return
        if self._ai_pulling:
            self._show_message("Дождитесь окончания скачивания модели")
            return
        if self._ai_busy:
            self._show_message("Дождитесь окончания проверки текста")
            return

        self._models_dir = ""
        self._save_ai_settings()
        self._managed_ollama.stop()
        ai_check.configure(
            base_url=ai_check.DEFAULT_BASE_URL, model=self._model_name
        )
        self._show_message(
            "Используется системная Ollama. Модель можно скачать командой "
            f"ollama pull {self._model_name}.",
            seconds=15,
        )

    def _start_model_pull(self) -> None:
        if self._ai_pulling:
            return
        self._ai_pulling = True
        self._ai_pull_cancel = threading.Event()
        self._ai_pull_status = ""
        self._ai_pull_last_update = 0.0
        self._build_pull_dialog()
        threading.Thread(target=self._ai_pull_worker, daemon=True).start()

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
        self._watch_dark_titlebar(dialog)

        content = dialog.get_content_area()
        content.set_border_width(16)
        content.set_spacing(10)

        header = Gtk.Label()
        header.set_markup(
            "Скачиваю модель <b>"
            f"{GLib.markup_escape_text(self._model_name)}</b>"
        )
        header.set_xalign(0.0)
        header.set_line_wrap(True)
        content.pack_start(header, False, False, 0)

        initial = (
            "Запускаю отдельную службу Ollama…"
            if self._models_dir
            else "Подключаюсь к Ollama…"
        )
        self._ai_pull_title = Gtk.Label(label=initial)
        self._ai_pull_title.set_xalign(0.0)
        self._ai_pull_title.set_line_wrap(True)
        content.pack_start(self._ai_pull_title, False, False, 0)

        self._ai_pull_progress = Gtk.ProgressBar()
        self._ai_pull_progress.set_show_text(True)
        content.pack_start(self._ai_pull_progress, False, False, 0)

        if self._models_dir:
            detail = (
                f"Папка: {self._models_dir}\n"
                "Скачивание можно отменить — загруженные части сохранятся."
            )
        else:
            detail = (
                "Системная Ollama. Скачивание можно отменить — загруженные "
                "части сохранятся."
            )
        self._ai_pull_detail = Gtk.Label(label=detail)
        self._ai_pull_detail.set_xalign(0.0)
        self._ai_pull_detail.set_line_wrap(True)
        self._ai_pull_detail.set_selectable(True)
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

    def _ai_pull_worker(self) -> None:
        downloaded = False
        cancelled = False
        error: str | None = None
        try:
            self._ensure_backend()
            downloaded = ai_check.pull_model(
                model=self._model_name,
                on_progress=self._ai_pull_progress_cb,
                cancel=self._ai_pull_cancel,
            )
        except ai_check.AICancelled as exc:
            cancelled = True
            error = str(exc)
        except (ai_check.AIError, OllamaStartError) as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_pull_finished, downloaded, cancelled, error)

    def _ai_pull_finished(
        self,
        downloaded: bool,
        cancelled: bool,
        error: str | None,
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
            self._show_message("Скачивание модели отменено", seconds=10)
            return False

        if error is not None:
            self._show_message("Не удалось скачать модель", seconds=10)
            self._show_notice("Не удалось скачать модель", error)
            return False

        location = self._models_dir or "папка системной Ollama"
        if downloaded:
            self._show_message(
                f"Модель «{self._model_name}» скачана: {location}", seconds=15
            )
        else:
            self._show_message(
                f"Модель «{self._model_name}» уже была скачана: {location}",
                seconds=15,
            )
        return False

    def _ai_worker(self, tab: DocumentTab, text: str, offset: int) -> None:
        corrected: str | None = None
        error: str | None = None
        try:
            corrected = ai_check.check_text(text, model=self._model_name)
        except ai_check.AIError as exc:
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - поток не должен падать молча
            error = f"Неожиданная ошибка: {exc}"
        GLib.idle_add(self._ai_finished, tab, text, offset, corrected, error)

    def _ai_finished(
        self,
        tab: DocumentTab,
        original: str,
        offset: int,
        corrected: str | None,
        error: str | None,
    ) -> bool:
        self._ai_busy = False
        if not self.get_realized() or tab not in self._tabs:
            return False

        if error is not None:
            self._show_message("ИИ не смог проверить текст", seconds=10)
            self._show_ai_error(error)
            return False

        end_offset = offset + len(original)
        start = tab.buffer.get_iter_at_offset(offset)
        end = tab.buffer.get_iter_at_offset(end_offset)
        if tab.buffer.get_text(start, end, True) != original:
            self._show_message("Текст изменился во время проверки — результат не применён")
            return False

        assert corrected is not None
        if corrected == original or corrected.strip() == original.strip():
            self._show_message("ИИ не нашёл ошибок")
            return False

        # Показываем вкладку, в которой исправляется текст.
        self._select_tab(tab)
        tab.buffer.begin_user_action()
        try:
            tab.buffer.delete(start, end)
            insert_at = tab.buffer.get_iter_at_offset(offset)
            tab.buffer.insert(insert_at, corrected)
            tab.buffer.select_range(
                tab.buffer.get_iter_at_offset(offset),
                tab.buffer.get_iter_at_offset(offset + len(corrected)),
            )
        finally:
            tab.buffer.end_user_action()

        self._show_message(
            f"ИИ исправил текст ({len(original)} → {len(corrected)} символов). "
            "Отменить — Ctrl+Z"
        )
        return False

    def _show_notice(self, title: str, message: str) -> None:
        """Немодальное сообщение об ошибке: не блокирует окно."""
        dialog = Gtk.MessageDialog(
            transient_for=self,
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.CLOSE,
            text=title,
        )
        dialog.format_secondary_text(message)
        self._watch_dark_titlebar(dialog)
        dialog.connect("response", lambda dlg, _response: dlg.destroy())
        dialog.show()

    def _show_ai_error(self, message: str) -> None:
        self._show_notice("Не удалось проверить текст", message)

    # -------------------------------------------------------------- updates

    def _set_update_label(self, text: str, tooltip: str | None = None) -> None:
        if self._destroyed:
            return
        self.version_button.set_label(text)
        self.version_button.set_tooltip_text(tooltip or "Проверить обновления")

    def action_check_updates(self, *_args) -> None:
        """Проверяет обновления; новую версию скачивает и устанавливает."""
        if self._update_busy:
            return
        if self._update_downloaded is not None:
            if updates.platform_kind() == "windows":
                self._install_windows_update()
            else:
                self._install_linux_update()
            return
        self._start_update_check()

    def _start_update_check(self) -> None:
        self._update_busy = True
        self.version_button.set_sensitive(False)
        self._set_update_label("Проверяю обновления…")
        self._show_message("Проверяю обновления на GitHub…", seconds=0)
        threading.Thread(
            target=self._update_check_worker,
            args=(__version__, updates.platform_kind()),
            daemon=True,
        ).start()

    def _update_check_worker(self, current: str, kind: str) -> None:
        try:
            releases = updates.fetch_releases()
            release = updates.select_release(releases, current, kind)
        except updates.UpdateError as exc:
            GLib.idle_add(self._update_failed, str(exc))
            return
        GLib.idle_add(self._update_checked, release, current)

    def _update_checked(
        self, release: updates.ReleaseInfo | None, current: str
    ) -> None:
        if self._destroyed:
            return
        self._update_busy = False
        self.version_button.set_sensitive(True)
        if release is None:
            self._set_update_label("Версия актуальна")
            self._show_message(f"У вас последняя версия {current}.")
            return

        self.version_button.set_sensitive(False)
        self._update_busy = True
        self._update_progress_time = 0.0
        self._set_update_label(f"Скачиваю {release.version}… 0%")
        self._show_message(
            f"Доступна версия {release.version}, скачиваю…", seconds=0
        )
        try:
            destination = self._downloads_dir() / release.asset_name
        except OSError as exc:
            self._update_failed(f"Не удалось создать папку загрузок: {exc}")
            return
        threading.Thread(
            target=self._update_download_worker,
            args=(release, destination),
            daemon=True,
        ).start()

    def _downloads_dir(self) -> Path:
        directory = GLib.get_user_special_dir(
            GLib.UserDirectory.DIRECTORY_DOWNLOAD
        )
        path = Path(directory) if directory else Path.home() / "Downloads"
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError:
            path = user_settings.config_dir() / "updates"
            path.mkdir(parents=True, exist_ok=True)
        return path

    def _update_download_worker(
        self, release: updates.ReleaseInfo, destination: Path
    ) -> None:
        try:
            updates.download_asset(
                release.asset_url,
                destination,
                progress=lambda done, total: GLib.idle_add(
                    self._update_download_progress,
                    release.version,
                    done,
                    total,
                ),
            )
        except updates.UpdateError as exc:
            GLib.idle_add(self._update_failed, str(exc))
            return
        GLib.idle_add(self._update_downloaded_ok, release, destination)

    def _update_download_progress(
        self, version: str, done: int, total: int
    ) -> bool:
        if self._destroyed:
            return False
        now = time.monotonic()
        if now - self._update_progress_time < 0.2 and (total <= 0 or done < total):
            return False
        self._update_progress_time = now
        if total > 0:
            percent = min(100, int(done * 100 / total))
            self._set_update_label(f"Скачиваю {version}… {percent}%")
            self._show_message(
                f"Скачиваю версию {version}: {percent}%", seconds=0
            )
        else:
            self._set_update_label(f"Скачиваю {version}…")
            self._show_message(f"Скачиваю версию {version}…", seconds=0)
        return False

    def _update_downloaded_ok(
        self, release: updates.ReleaseInfo, destination: Path
    ) -> bool:
        if self._destroyed:
            return False
        self._update_busy = False
        self._update_downloaded = destination
        self._update_version = release.version
        self.version_button.set_sensitive(True)

        if updates.platform_kind() == "windows":
            self._set_update_label(
                f"Скачана версия {release.version}", "Установить обновление"
            )
            self._show_message(
                f"Версия {release.version} скачана, запускаю установку…",
                seconds=0,
            )
            GLib.timeout_add(400, self._install_windows_update)
            return False

        self._set_update_label(
            f"Скачана версия {release.version}", "Установить обновление"
        )
        self._show_message(
            f"Версия {release.version} скачана, запускаю установку…",
            seconds=0,
        )
        GLib.timeout_add(400, self._install_linux_update)
        return False

    def _install_windows_update(self) -> bool:
        """Запускает скачанный установщик и закрывает приложение."""
        if self._destroyed or self._update_busy:
            return False
        path = self._update_downloaded
        if path is None:
            return False
        if not self._confirm_discard():
            self._set_update_label(
                f"Скачана версия {self._update_version}", "Установить обновление"
            )
            return False

        ok, error = updates.launch_windows_installer(path)
        if not ok:
            self._set_update_label(
                f"Скачана версия {self._update_version}", "Установить обновление"
            )
            self._show_message("Не удалось запустить установщик обновления", seconds=10)
            self._show_notice("Обновление", error)
            return False

        self._set_update_label(f"Устанавливаю {self._update_version}…")
        self._show_message(
            "Установщик запущен. Приложение закроется и откроется снова.",
            seconds=0,
        )
        GLib.timeout_add(600, self._quit_for_update)
        return False

    def _install_linux_update(self) -> bool:
        """Устанавливает скачанный .deb через pkexec и перезапускает окно."""
        if self._destroyed or self._update_busy:
            return False
        path = self._update_downloaded
        if path is None:
            return False
        if not self._confirm_discard():
            self._set_update_label(
                f"Скачана версия {self._update_version}", "Установить обновление"
            )
            return False

        self._update_busy = True
        self.version_button.set_sensitive(False)
        self._set_update_label(f"Устанавливаю {self._update_version}…")
        self._show_message(
            "Запрашиваю права администратора для установки обновления…",
            seconds=0,
        )
        threading.Thread(
            target=self._linux_install_worker,
            args=(path,),
            daemon=True,
        ).start()
        return False

    def _linux_install_worker(self, path: Path) -> None:
        ok, error = updates.launch_linux_installer(path)
        GLib.idle_add(self._linux_install_done, ok, error)

    def _linux_install_done(self, ok: bool, error: str) -> bool:
        if self._destroyed:
            return False
        self._update_busy = False
        if not ok:
            self.version_button.set_sensitive(True)
            self._set_update_label(
                f"Скачана версия {self._update_version}", "Установить обновление"
            )
            self._show_message("Не удалось установить обновление", seconds=10)
            self._show_notice("Обновление", error)
            return False

        self._set_update_label(f"Установлено {self._update_version}")
        self._show_message(
            "Обновление установлено, перезапускаю приложение…", seconds=0
        )
        GLib.timeout_add(600, self._restart_after_update)
        return False

    def _restart_after_update(self) -> bool:
        ok, error = updates.restart_after_exit()
        if not ok:
            self._show_notice("Обновление", error)
        self._quit_for_update()
        return False

    def _quit_for_update(self) -> bool:
        application = self.get_application()
        if application is not None:
            application.quit()
        else:
            self.destroy()
        return False

    def _update_failed(self, message: str) -> bool:
        if self._destroyed:
            return False
        self._update_busy = False
        self.version_button.set_sensitive(True)
        self._set_update_label("Проверить обновления")
        self._show_message("Не удалось проверить обновления", seconds=10)
        self._show_notice("Обновление", message)
        return False

    # ---------------------------------------------------------------- theme

    @staticmethod
    def _detect_system_dark_theme() -> bool:
        """Тема, выбранная в системе, если пользователь её не переопределил."""
        settings = Gtk.Settings.get_default()
        if settings is None:
            return False
        theme = (settings.get_property("gtk-theme-name") or "").lower()
        return bool(settings.get_property("gtk-application-prefer-dark-theme")) or (
            "dark" in theme
        )

    def _sync_gtk_theme(self) -> None:
        """Меняет тему GTK, чтобы шапка окна и системные виджеты следовали за ней."""
        settings = Gtk.Settings.get_default()
        if settings is None:
            return
        dark = self._dark_theme
        settings.set_property("gtk-application-prefer-dark-theme", dark)
        name = (settings.get_property("gtk-theme-name") or "").lower()
        if dark and "dark" not in name:
            settings.set_property("gtk-theme-name", "Adwaita-dark")
        elif not dark and "dark" in name:
            settings.set_property("gtk-theme-name", "Adwaita")

    def _on_theme_state_changed(self, action, value) -> None:
        dark = value.get_boolean()
        action.set_state(value)
        if dark == self._dark_theme:
            return
        self._dark_theme = dark
        self._theme_preference = "dark" if dark else "light"
        config = user_settings.load()
        config["theme"] = self._theme_preference
        try:
            user_settings.save(config)
        except OSError as exc:
            self._show_message(f"Не удалось сохранить тему: {exc}", seconds=12)
        self._sync_gtk_theme()
        self._apply_style_scheme()
        # Для WM, которые учитывают _GTK_THEME_VARIANT, обновляем свойство
        # после того, как GTK закончит асинхронную смену темы.
        GLib.timeout_add(300, lambda: self._apply_titlebar_variant() or False)

    def _on_system_theme_changed(self, *_args) -> None:
        """Следовать системной теме, пока пользователь не выбрал свою."""
        if self._theme_preference is not None:
            return
        dark = self._detect_system_dark_theme()
        if dark == self._dark_theme:
            return
        self._dark_theme = dark
        if os.name == "nt":
            self._sync_gtk_theme()
        self._apply_style_scheme()

    def _apply_style_scheme(self) -> None:
        dark = self._dark_theme
        context = self.get_style_context()
        if dark:
            context.add_class("dark-theme")
        else:
            context.remove_class("dark-theme")
        # Всплывающие меню и выпадающие списки — отдельные окна GTK, на них
        # класс главного окна не действует. Приложение подключает тёмный CSS
        # для всего экрана, поэтому списки тоже меняют цвет вместе с темой.
        application = self.get_application()
        if application is not None and hasattr(application, "set_dark_style"):
            application.set_dark_style(dark)
        manager = GtkSource.StyleSchemeManager.get_default()
        # Своя тёмная схема: нейтральный серый фон вместо зеленоватого oblivion.
        styles_dir = str(Path(__file__).parent / "resources" / "styles")
        if styles_dir not in manager.get_search_path():
            manager.append_search_path(styles_dir)
        scheme_id = "eves-dark" if dark else "kate"
        scheme = manager.get_scheme(scheme_id)
        if scheme is None:
            fallback = "oblivion" if dark else "classic"
            scheme = manager.get_scheme(fallback)
        if scheme is not None:
            for tab in self._tabs:
                tab.buffer.set_style_scheme(scheme)
        for tab in self._tabs:
            # На Windows системный caret-color не следует за темой: цвет
            # своей каретки обновляем вместе со схемой подсветки.
            if tab.caret is not None:
                tab.caret.set_dark(dark)
            tab.refresh_visuals()
        self._apply_windows_titlebar()

    def _on_map(self, *_args) -> None:
        self._restore_window_geometry()
        self._apply_windows_titlebar()
        self._apply_titlebar_variant()
        self._install_native_file_drop()
        self._install_com_file_drop()

    def _install_native_file_drop(self) -> None:
        """Включить ``DragAcceptFiles`` и перехват ``WM_DROPFILES`` в Windows."""
        if not win_dnd.use_com_file_drop() or self._native_drop is not None:
            return
        gdk_window = self.get_window()
        if gdk_window is None:
            return
        self._native_drop = win_drop.install_for_window(
            self._on_native_files_dropped, gdk_window
        )

    def _on_native_files_dropped(self, paths: list[str]) -> None:
        """Открыть файлы из ``WM_DROPFILES`` в новых вкладках."""
        GLib.idle_add(self._open_native_dropped_paths, list(paths))

    def _open_native_dropped_paths(self, paths: list[str]) -> bool:
        """Открыть файлы из нативного броска, не меняя текущий документ."""
        self._open_dropped_paths_once(paths)
        return False

    def _install_com_file_drop(self) -> None:
        """Зарегистрировать COM ``IDropTarget``, если нативный приём недоступен.

        ``DragAcceptFiles`` — основной путь: он не зависит от OLE и
        работает в Windows 11 так же, как в прежних версиях. COM-приёмник
        подключаем только если подкласс окна установить не удалось, чтобы не
        создавать два конкурирующих приёмника на одном HWND.
        """
        if (
            not win_dnd.use_com_file_drop()
            or self._com_drop is not None
            or self._native_drop is not None
        ):
            return
        gdk_window = self.get_window()
        if gdk_window is None:
            return
        self._com_drop = win_com_drop.install_for_window(
            self._on_com_files_dropped, gdk_window
        )

    def _on_com_files_dropped(self, paths: list[str]) -> None:
        """Передать пути из COM-броска в GTK-поток обработки бросков."""
        GLib.idle_add(self._open_com_dropped_paths, list(paths))

    def _open_com_dropped_paths(self, paths: list[str]) -> bool:
        """Открыть файлы из COM-броска, не меняя текущий документ."""
        self._open_dropped_paths_once(paths)
        return False

    def _open_dropped_paths_once(self, paths: list[str]) -> bool:
        """Открыть пути один раз, если бросок дошёл сразу двумя путями.

        Windows может доставить один и тот же бросок и как OLE ``Drop``, и как
        ``WM_DROPFILES``. Игнорируем повтор только для того же набора путей
        в течение короткого времени.
        """
        key = tuple(paths)
        if not key:
            return False
        now = time.monotonic()
        previous = self._recent_drops.get(key)
        if previous is not None and now - previous < 1.0:
            return False
        self._recent_drops[key] = now
        if len(self._recent_drops) > 64:
            for old_key in list(self._recent_drops)[:32]:
                self._recent_drops.pop(old_key, None)
        return self._open_dropped_uris(list(paths))

    def _apply_titlebar_variant(self, window: Gtk.Window | None = None) -> None:
        """Сообщает WM вариант рамки окна: светлый или тёмный.

        GTK выставляет _GTK_THEME_VARIANT только при создании окна. Некоторые
        окружения учитывают обновление свойства; Cinnamon игнорирует его,
        поэтому главное окно в Linux рисует собственную строку заголовка.
        """
        target = window or self
        gdk_window = target.get_window()
        if gdk_window is None:
            return
        try:
            xid = gdk_window.get_xid()
        except (AttributeError, TypeError):
            return
        if not xid:
            return
        try:
            import ctypes

            x11 = ctypes.CDLL("libX11.so.6")
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            x11.XInternAtom.argtypes = [
                ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int
            ]
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XChangeProperty.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_int,
            ]
            x11.XChangeProperty.restype = ctypes.c_int

            display = x11.XOpenDisplay(None)
            if not display:
                return
            try:
                variant = x11.XInternAtom(display, b"_GTK_THEME_VARIANT", 0)
                utf8 = x11.XInternAtom(display, b"UTF8_STRING", 0)
                value = b"dark" if self._dark_theme else b"light"
                x11.XChangeProperty(
                    display,
                    xid,
                    variant,
                    utf8,
                    8,
                    0,  # PropModeReplace
                    value,
                    len(value),
                )
            finally:
                x11.XCloseDisplay(display)
        except (OSError, AttributeError, TypeError, ValueError):
            pass

    def _apply_windows_titlebar(self, window: Gtk.Window | None = None) -> None:
        """Красит заголовок окна в тёмный цвет на Windows 10/11."""
        if os.name != "nt":
            return
        target = window or self
        gdk_window = target.get_window()
        if gdk_window is None:
            return
        handle = win_theme.gdk_window_handle(gdk_window)
        if handle:
            win_theme.apply_dark_titlebar(handle, self._dark_theme)

    def _watch_dark_titlebar(self, window: Gtk.Window) -> None:
        """Применяет оформление шапки к диалогу после его появления."""
        if not isinstance(window, Gtk.Window):
            return
        window.connect(
            "map",
            lambda widget, *_: (
                self._apply_windows_titlebar(widget),
                self._apply_titlebar_variant(widget),
            ),
        )

    # ------------------------------------------------------------------ text

    def get_text(self, tab: DocumentTab | None = None) -> str:
        target = tab or self._active_tab
        if target is None:
            return ""
        start, end = target.buffer.get_bounds()
        return target.buffer.get_text(start, end, True)

    def set_text(self, text: str, path: Path | None = None) -> None:
        if self._active_tab is None:
            return
        target = self._active_tab
        self._load_tab_text(
            target,
            text,
            Path(path) if path is not None else None,
            target.encoding,
        )
        self._activate_tab(target)

    def _load_tab_text(
        self,
        tab: DocumentTab,
        text: str,
        path: Path | None,
        encoding: str,
        spans: list[document.Span] | None = None,
    ) -> None:
        """Заменяет содержимое вкладки, не создавая шагов отмены."""
        spans = spans or []
        try:
            tab.buffer.begin_not_undoable_action()
            tab.buffer.set_text(text)
            if any(values.get("align") for _first, _last, values in spans):
                self._apply_spans(tab, spans)
            else:
                self._retag_alignment(tab, self._default_align)
                format_spans = [
                    span
                    for span in spans
                    if any(span[2].get(key) for key in (*TEXT_FORMAT_KEYS, "size"))
                ]
                if format_spans:
                    self._apply_spans(tab, format_spans)
        finally:
            try:
                tab.buffer.end_not_undoable_action()
            except Exception:  # noqa: BLE001
                pass
        if not spans:
            tab.formatted = False
            for key in TEXT_FORMAT_KEYS:
                setattr(tab, f"{key}_typing", False)
            tab.active_size = None
        tab.buffer.set_modified(False)
        tab.path = Path(path) if path is not None else None
        tab.encoding = encoding
        tab.counts_cache = (-1, -1, -1)
        tab.buffer.place_cursor(tab.buffer.get_start_iter())
        tab.view.scroll_to_iter(
            tab.buffer.get_start_iter(), 0.0, False, 0.0, 0.0
        )
        tab.spellcheck.schedule()
        self._update_tab_label(tab)
        if tab is self._active_tab:
            self.enc_label.set_text(tab.encoding)
            self._update_title()
            self._update_status()
            self._update_counts()
            self._update_undo()
            tab.view.grab_focus()

    # ------------------------------------------------------------------ files

    def open_path(self, path: str | Path, *, new_tab: bool = False) -> None:
        path = Path(path)
        try:
            text, encoding, spans = document.read_document(path)
        except OSError as exc:
            self._show_error("Не удалось открыть файл", str(exc))
            return

        # Обычное открытие занимает пустой лист, а брошенный файл всегда
        # получает собственную вкладку — даже если текущая не изменена.
        tab = None if new_tab else self._pristine_tab()
        if tab is None:
            tab = self._create_tab()
        self._load_tab_text(tab, text, path, encoding, spans)
        self._select_tab(tab)
        self._update_format_controls()

    def action_new(self, *_args) -> None:
        self._create_tab()

    def action_open(self, *_args) -> None:
        dialog = Gtk.FileChooserNative.new(
            "Открыть файл", self, Gtk.FileChooserAction.OPEN, "_Открыть", "_Отмена"
        )
        self._add_filters(dialog)
        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response == Gtk.ResponseType.ACCEPT and path:
            self.open_path(path)

    def action_close_tab(self, *_args) -> None:
        self.close_tab()

    def action_next_tab(self, *_args) -> None:
        if len(self._tabs) > 1 and self._active_tab is not None:
            index = self._tabs.index(self._active_tab)
            self._select_tab(self._tabs[(index + 1) % len(self._tabs)])

    def action_prev_tab(self, *_args) -> None:
        if len(self._tabs) > 1 and self._active_tab is not None:
            index = self._tabs.index(self._active_tab)
            self._select_tab(self._tabs[(index - 1) % len(self._tabs)])

    def action_save(self, *_args) -> bool:
        return self._save_tab(self._active_tab)

    def action_save_as(self, *_args) -> bool:
        return self._save_tab_as(self._active_tab)

    def _save_tab(self, tab: DocumentTab | None) -> bool:
        if tab is None:
            return False
        if tab.path is None or (
            self._has_rtf_content(tab) and not document.is_rtf(tab.path)
        ):
            return self._save_tab_as(tab)
        return self._write_tab(tab, tab.path)

    def _save_tab_as(self, tab: DocumentTab | None, force_rtf: bool = False) -> bool:
        if tab is None:
            return False
        dialog = Gtk.FileChooserNative.new(
            "Сохранить как", self, Gtk.FileChooserAction.SAVE, "_Сохранить", "_Отмена"
        )
        dialog.set_do_overwrite_confirmation(True)
        self._add_filters(dialog)
        if self._has_rtf_content(tab, force_rtf):
            dialog.set_current_name(f"{tab.default_name}.rtf")
        elif tab.path is not None:
            dialog.set_current_name(tab.path.name)
        else:
            dialog.set_current_name(f"{tab.default_name}.txt")
        if tab.path is not None:
            dialog.set_current_folder(str(tab.path.parent))

        response = dialog.run()
        path = dialog.get_filename()
        dialog.destroy()
        if response != Gtk.ResponseType.ACCEPT or not path:
            return False
        return self._write_tab(tab, Path(path))

    def _has_rtf_content(self, tab: DocumentTab, force_rtf: bool = False) -> bool:
        """Нужно ли сохранять вкладку как RTF (есть оформление или был RTF)."""
        if force_rtf or self._collect_spans(tab):
            return True
        return bool(tab.path is not None and document.is_rtf(tab.path))

    def _write_tab(self, tab: DocumentTab, path: Path) -> bool:
        spans = self._collect_spans(tab)
        try:
            document.write_document(
                path, self.get_text(tab), spans, default_size=self._font_size
            )
        except OSError as exc:
            self._show_error("Не удалось сохранить файл", str(exc))
            return False
        tab.path = Path(path)
        tab.formatted = bool(spans)
        tab.buffer.set_modified(False)
        self._update_tab_label(tab)
        if tab is self._active_tab:
            self._update_title()
        return True

    @staticmethod
    def _add_filters(dialog: Gtk.FileChooserNative) -> None:
        # GtkFileChooserNative на Windows показывает штатный IFileDialog только
        # если все фильтры описываются расширениями. MIME-правила заставляют его
        # откатиться к GTK-диалогу с неподходящей цветовой темой.
        use_mime = os.name != "nt"

        rtf_filter = Gtk.FileFilter()
        rtf_filter.set_name("Форматированный текст (RTF)")
        if use_mime:
            rtf_filter.add_mime_type("application/rtf")
        rtf_filter.add_pattern("*.rtf")
        dialog.add_filter(rtf_filter)

        text_filter = Gtk.FileFilter()
        text_filter.set_name("Текстовые файлы")
        if use_mime:
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

    # ---------------------------------------------------------------- поиск

    def action_find(self, *_args) -> None:
        """Открыть панель поиска (Ctrl+F)."""
        tab = self._active_tab
        if tab is None:
            return
        bounds = tab.buffer.get_selection_bounds()
        if bounds:
            selected = tab.buffer.get_text(bounds[0], bounds[1], True)
            if selected and "\n" not in selected:
                self.search_entry.set_text(selected)
        self.search_bar.show()
        self.search_entry.grab_focus()
        self.search_entry.select_region(0, -1)
        if self.search_entry.get_text():
            self._find_next()

    def _close_search_bar(self, *_args) -> None:
        """Скрыть поиск и вернуть фокус в текст."""
        self.search_bar.hide()
        if self._active_tab is not None:
            self._active_tab.view.grab_focus()

    def _on_search_close(self, *_args) -> None:
        self._close_search_bar()

    def _on_search_next(self, *_args) -> None:
        self._find_next()

    def _on_search_previous(self, *_args) -> None:
        self._find_previous()

    def _on_search_changed(self, _entry) -> None:
        self._find_next()

    def _on_search_key(self, _entry, event) -> bool:
        """Enter — вперёд, Shift+Enter — назад, Esc — закрыть поиск."""
        if event.keyval == Gdk.KEY_Escape:
            self._close_search_bar()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if event.get_state() & Gdk.ModifierType.SHIFT_MASK:
                self._find_previous()
            else:
                self._find_next()
            return True
        return False

    def _search_context(self) -> GtkSource.SearchContext | None:
        """Контекст поиска для активной вкладки с текущими настройками."""
        tab = self._active_tab
        if tab is None:
            return None
        if tab.search_context is None:
            tab.search_context = GtkSource.SearchContext.new(tab.buffer)
        settings = tab.search_context.get_settings()
        settings.set_search_text(self.search_entry.get_text())
        settings.set_case_sensitive(False)
        settings.set_wrap_around(True)
        return tab.search_context

    def _find(self, forward: bool) -> None:
        """Выделить следующее или предыдущее совпадение."""
        tab = self._active_tab
        context = self._search_context()
        if tab is None or context is None:
            return
        if not self.search_entry.get_text():
            self.search_count.set_text("")
            return

        insert = tab.buffer.get_iter_at_mark(tab.buffer.get_insert())
        bound = tab.buffer.get_iter_at_mark(tab.buffer.get_selection_bound())
        if forward:
            start_iter = insert if insert.compare(bound) >= 0 else bound
            found, start, end, _wrapped = context.forward(start_iter)
        else:
            start_iter = insert if insert.compare(bound) <= 0 else bound
            found, start, end, _wrapped = context.backward(start_iter)
        if not found:
            self.search_count.set_text("Нет совпадений")
            return

        tab.buffer.select_range(start, end)
        tab.view.scroll_to_iter(start, 0.05, True, 0.5, 0.5)
        count = context.get_occurrences_count()
        position = context.get_occurrence_position(start, end)
        if count > 0 and position > 0:
            self.search_count.set_text(f"{position} / {count}")
        else:
            self.search_count.set_text("Найдено" if count > 0 else "Нет совпадений")

    def _find_next(self) -> None:
        self._find(forward=True)

    def _find_previous(self) -> None:
        self._find(forward=False)

    # ------------------------------------------------------------- оформление

    @staticmethod
    def _format_tag(tab: DocumentTab, key: str) -> Gtk.TextTag:
        """Текстовый тег для жирного, наклонного или подчёркнутого."""
        return getattr(tab, f"{key}_tag")

    def _get_size_tag(self, tab: DocumentTab, size: int) -> Gtk.TextTag:
        size = int(size)
        tag = tab.size_tags.get(size)
        if tag is None:
            tag = tab.buffer.create_tag(
                f"{SIZE_TAG_PREFIX}{size}", size=size * Pango.SCALE
            )
            tab.size_tags[size] = tag
        return tag

    @staticmethod
    def _tag_ranges(buffer: Gtk.TextBuffer, tag: Gtk.TextTag):
        """Диапазоны, где активен тег (для сбора оформления при сохранении)."""
        iterator = buffer.get_start_iter()
        while not iterator.is_end():
            if iterator.has_tag(tag):
                start = iterator.get_offset()
                if not iterator.forward_to_tag_toggle(tag):
                    yield start, iterator.get_offset()
                    return
                yield start, iterator.get_offset()
            elif not iterator.forward_to_tag_toggle(tag):
                return

    def _toggle_format(self, key: str) -> None:
        """Включить/выключить начертание для выделения или следующего ввода."""
        tab = self._active_tab
        if tab is None:
            return
        buffer = tab.buffer
        tag = self._format_tag(tab, key)
        flag = f"{key}_typing"
        if buffer.get_has_selection():
            start, end = buffer.get_selection_bounds()
            if start.has_tag(tag):
                buffer.remove_tag(tag, start, end)
                setattr(tab, flag, False)
            else:
                buffer.apply_tag(tag, start, end)
                setattr(tab, flag, True)
        else:
            iterator = buffer.get_iter_at_mark(buffer.get_insert())
            current = getattr(tab, flag) or iterator.has_tag(tag)
            setattr(tab, flag, not current)
        self._refresh_formatted(tab)
        self._update_format_controls()

    def action_bold(self, *_args) -> None:
        self._toggle_format("bold")

    def action_italic(self, *_args) -> None:
        self._toggle_format("italic")

    def action_underline(self, *_args) -> None:
        self._toggle_format("underline")

    def _on_format_toggled(self, _button: Gtk.ToggleButton, key: str) -> None:
        if self._format_guard:
            return
        self._toggle_format(key)

    def _refresh_formatted(self, tab: DocumentTab) -> None:
        tab.formatted = bool(self._collect_spans(tab))

    def _collect_spans(self, tab: DocumentTab) -> list[document.Span]:
        """Собрать оформление буфера в диапазоны для RTF."""
        buffer = tab.buffer
        start, end = buffer.get_bounds()
        text = buffer.get_text(start, end, True)
        if not text:
            return []
        changes: dict[int, dict[str, object]] = {}

        def change(offset: int, key: str, value: object) -> None:
            changes.setdefault(offset, {})[key] = value

        for key in TEXT_FORMAT_KEYS:
            for first, last in self._tag_ranges(buffer, self._format_tag(tab, key)):
                change(first, key, True)
                change(last, key, False)
        for size, tag in tab.size_tags.items():
            for first, last in self._tag_ranges(buffer, tag):
                change(first, "size", size)
                change(last, "size", None)

        offset = 0
        for line in text.splitlines(keepends=True):
            iterator = buffer.get_iter_at_offset(offset)
            key = self._alignment_at(buffer, tab.align_tags, offset)
            change(offset, "align", key or "left")
            offset += len(line)

        attrs: dict[str, object] = {key: False for key in TEXT_FORMAT_KEYS}
        attrs.update({"size": None, "align": "left"})
        spans: list[document.Span] = []
        span_start = 0
        for position in sorted(changes):
            if position > span_start:
                spans.append((span_start, position, dict(attrs)))
            attrs.update(changes[position])
            span_start = position
        if span_start < len(text):
            spans.append((span_start, len(text), dict(attrs)))
        return [
            (first, last, values)
            for first, last, values in spans
            if document._is_meaningful(values)
        ]

    def _apply_spans(self, tab: DocumentTab, spans: list[document.Span]) -> None:
        buffer = tab.buffer
        for first, last, values in spans:
            start = buffer.get_iter_at_offset(first)
            end = buffer.get_iter_at_offset(last)
            for key in TEXT_FORMAT_KEYS:
                if values.get(key):
                    buffer.apply_tag(self._format_tag(tab, key), start, end)
            size = values.get("size")
            if isinstance(size, int):
                buffer.apply_tag(self._get_size_tag(tab, size), start, end)
            align = values.get("align")
            if align in tab.align_tags:
                buffer.apply_tag(tab.align_tags[str(align)], start, end)
        tab.formatted = bool(spans)

    def _update_format_controls(self) -> None:
        tab = self._active_tab
        if tab is None:
            return
        buffer = tab.buffer
        if buffer.get_has_selection():
            iterator, _end = buffer.get_selection_bounds()
            active = {
                key: iterator.has_tag(self._format_tag(tab, key))
                for key in TEXT_FORMAT_KEYS
            }
        else:
            iterator = buffer.get_iter_at_mark(buffer.get_insert())
            active = {
                key: getattr(tab, f"{key}_typing")
                or iterator.has_tag(self._format_tag(tab, key))
                for key in TEXT_FORMAT_KEYS
            }
        if buffer.get_has_selection():
            # Для выделения показываем его текущий размер, а не отложенный.
            size = None
            for value, tag in tab.size_tags.items():
                if iterator.has_tag(tag):
                    size = value
                    break
        else:
            # Без выделения поле отражает размер для следующего ввода.
            size = tab.active_size
            if size is None:
                for value, tag in tab.size_tags.items():
                    if iterator.has_tag(tag):
                        size = value
                        break
        if size is None:
            size = self._font_size
        self._format_guard = True
        self._font_size_guard = True
        try:
            self.bold_button.set_active(bool(active["bold"]))
            self.italic_button.set_active(bool(active["italic"]))
            self.underline_button.set_active(bool(active["underline"]))
            self.font_size_spin.set_value(size)
        finally:
            self._font_size_guard = False
            self._format_guard = False

    def action_quit(self, *_args) -> None:
        self.close()

    # --------------------------------------------------------------- status

    def _update_title(self) -> None:
        if self._active_tab is None:
            self.set_title(APP_NAME)
            if self._header_bar is not None:
                self._header_bar.set_title(APP_NAME)
            return
        tab = self._active_tab
        name = tab.path.name if tab.path is not None else tab.default_name
        mark = "• " if tab.buffer.get_modified() else ""
        self.set_title(f"{mark}{name} — {APP_NAME}")
        if self._header_bar is not None:
            self._header_bar.set_title(self.get_title())

    def _update_status(self) -> None:
        if self._active_tab is None:
            self.pos_label.set_text("")
            return
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
        if self._active_tab is None:
            self.counts_label.set_text("")
            return False
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

    def _confirm_discard(self, tab: DocumentTab | None = None) -> bool:
        """True — можно продолжать; False — пользователь отменил."""
        target = tab or self._active_tab
        if target is None or not target.buffer.get_modified():
            return True

        name = target.path.name if target.path is not None else target.default_name
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
            return self._save_tab(target)
        return response == Gtk.ResponseType.NO

    def _on_delete_event(self, *_args) -> bool:
        # Перед закрытием окна проверяем все вкладки, а не только активную.
        for tab in list(self._tabs):
            if not self._confirm_discard(tab):
                return True
        return False

    def _on_destroy(self, *_args) -> None:
        """Останавливает скачивание и отдельную службу при закрытии окна."""
        self._destroyed = True
        self._save_window_geometry()
        if self._native_drop is not None:
            self._native_drop.uninstall()
            self._native_drop = None
        if self._com_drop is not None:
            self._com_drop.uninstall()
            self._com_drop = None
        self._stop_tab_animation()
        self._cancel_tab_drag()
        for tab in self._tabs:
            if tab.caret is not None:
                tab.caret.stop()
        if self._ai_pull_cancel is not None:
            self._ai_pull_cancel.set()
        self._managed_ollama.stop()

    @staticmethod
    def _dropped_paths(raw: str) -> list[Path]:
        """Разобрать URI-список или текст с путями из данных броска."""
        paths: list[Path] = []
        for line in raw.replace("\x00", "\n").splitlines():
            value = line.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            if not value:
                continue
            if value.startswith("file://"):
                try:
                    path, _hostname = GLib.filename_from_uri(value)
                except Exception:  # noqa: BLE001
                    path = unquote(urlparse(value).path)
            else:
                path = unquote(value)
            # Проводник иногда отдаёт URI без хоста: file:///C:/... → /C:/...
            if re.match(r"^/[A-Za-z]:/", path):
                path = path[1:]
            if path:
                paths.append(Path(path))
        return paths

    def _open_dropped_uris(self, uris: list[str]) -> bool:
        """Открыть файлы, брошенные в окно. Возвращает True, если открыли."""
        candidates: list[str] = []
        for uri in uris:
            if not uri:
                continue
            candidates.append(uri)
            try:
                path = Gio.File.new_for_uri(uri).get_path()
            except Exception:  # noqa: BLE001
                path = None
            if path:
                candidates.append(path)

        opened = False
        seen: set[Path] = set()
        for path in self._dropped_paths("\n".join(candidates)):
            try:
                resolved = path.expanduser().resolve()
            except OSError:
                continue
            if resolved in seen or not resolved.is_file():
                continue
            seen.add(resolved)
            self.open_path(resolved, new_tab=True)
            opened = True
        return opened

    def _dropped_text(self, data, info: int) -> str:
        """Достать текст из данных броска: через API или из сырых байтов."""
        if info == DND_URI_LIST:
            try:
                uris = data.get_uris() or []
            except Exception:  # noqa: BLE001
                uris = []
            if uris:
                return "\n".join(uris)
        try:
            raw = data.get_data()
        except Exception:  # noqa: BLE001
            raw = b""
        if raw:
            encodings = ["utf-8"]
            if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
                encodings.insert(0, "utf-16")
            encodings.append("cp1251")
            for encoding in encodings:
                try:
                    return raw.decode(encoding)
                except UnicodeDecodeError:
                    continue
        try:
            return data.get_text() or ""
        except Exception:  # noqa: BLE001
            return ""

    def _on_drag_data(self, widget, context, x, y, data, info, time) -> bool:
        text = self._dropped_text(data, info)
        handled = self._open_dropped_uris(text.splitlines()) if text else False
        if not handled and info != DND_URI_LIST and isinstance(widget, Gtk.TextView):
            # Обычный текст, брошенный в поле ввода, вставляем как есть.
            buffer = widget.get_buffer()
            position = buffer.get_end_iter()
            success, found = buffer.get_iter_at_location(x, y)
            if success:
                position = found
            buffer.insert_interactive(position, text, True)
            handled = True
        if handled and context is not None:
            # Встроенный обработчик GtkTextView иначе может вставить URI
            # или текст файла прямо в редактируемый документ.
            try:
                GObject.signal_stop_emission_by_name(widget, "drag-data-received")
            except Exception:  # noqa: BLE001
                pass
        Gtk.drag_finish(context, handled, False, time)
        # Событие не должно уйти родительскому виджету: иначе один и тот же
        # файл откроется дважды — с поля ввода и сразу с окна.
        return True

    def _on_about(self, *_args) -> None:
        dialog = Gtk.AboutDialog(transient_for=self, modal=True)
        self._watch_dark_titlebar(dialog)
        dialog.set_program_name(APP_NAME)
        dialog.set_version(__version__)
        dialog.set_comments(
            "Текстовый редактор для русского языка с проверкой орфографии.\n"
            "Проверка с ИИ: выделите текст → правый клик → «Проверить с ИИ»."
        )
        logo = app_logo()
        if logo is not None:
            dialog.set_logo(logo)
        dialog.set_icon_name(self._icon_name)
        dialog.set_license_type(Gtk.License.MIT_X11)
        dialog.run()
        dialog.destroy()
