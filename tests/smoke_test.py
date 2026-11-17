#!/usr/bin/env python3
"""Дымовой тест: проверяет ввод-вывод файлов и сборку окна без показа на экране."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gio, Gtk, Gdk, GtkSource, Pango  # noqa: E402

Gtk.init_check(None)

from eves_text import application as application_module  # noqa: E402
from eves_text import document, updates  # noqa: E402
from eves_text.application import Application  # noqa: E402
from eves_text.window import MainWindow  # noqa: E402

# Отдельный id для теста: если запущен настоящий редактор, он владеет
# D-Bus-именем org.eves.Text, и создание второго окна с тем же id роняет GTK.
application_module.APP_ID = "org.eves.TextSmokeTest"


def test_document() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "тест.txt"
        text = "Привет, мир!\nВторая строка."
        document.write_text(path, text)
        loaded, encoding = document.read_text(path)
        assert loaded == text, loaded
        assert encoding == "UTF-8", encoding

        cp = Path(tmp) / "cp1251.txt"
        cp.write_bytes("Ёжик в тумане".encode("cp1251"))
        loaded, encoding = document.read_text(cp)
        assert loaded == "Ёжик в тумане", loaded
        assert encoding == "CP1251", encoding

    assert document.text_stats("один два три") == (3, 12, 10)


def test_updates() -> None:
    assert updates.tag_version("v0.6.5") == "0.6.5"
    assert updates.tag_version("win-v0.6.5") == "0.6.5"
    assert updates.is_newer("0.6.10", "0.6.9")
    assert not updates.is_newer("0.6.5", "0.6.5")
    assert not updates.is_newer("0.6.4", "0.6.5")

    releases = [
        {
            "tag_name": "v0.6.4",
            "draft": False,
            "prerelease": False,
            "html_url": "https://example/v0.6.4",
            "assets": [
                {
                    "name": "eves-text_0.6.4-1_all.deb",
                    "browser_download_url": "https://example/old.deb",
                    "size": 100,
                }
            ],
        },
        {
            "tag_name": "v0.6.6",
            "draft": False,
            "prerelease": False,
            "html_url": "https://example/v0.6.6",
            "assets": [
                {
                    "name": "eves-text_0.6.6-1_all.deb",
                    "browser_download_url": "https://example/new.deb",
                    "size": 200,
                }
            ],
        },
        {
            "tag_name": "win-v0.6.7",
            "draft": False,
            "prerelease": False,
            "html_url": "https://example/win-v0.6.7",
            "assets": [
                {
                    "name": "EvesText-0.6.7-win64-portable.zip",
                    "browser_download_url": "https://example/win.zip",
                    "size": 300,
                },
                {
                    "name": "EvesText-0.6.7-win64-setup.exe",
                    "browser_download_url": "https://example/win.exe",
                    "size": 400,
                },
            ],
        },
    ]
    linux = updates.select_release(releases, "0.6.5", "linux")
    assert linux is not None
    assert linux.version == "0.6.6"
    assert linux.asset_name.endswith(".deb")
    assert linux.asset_url == "https://example/new.deb"

    windows = updates.select_release(releases, "0.6.5", "windows")
    assert windows is not None
    assert windows.version == "0.6.7"
    assert windows.asset_name.endswith("setup.exe")

    assert updates.select_release(releases, "0.6.7", "windows") is None
    assert updates.select_release(releases, "0.6.6", "linux") is None


def test_win_theme() -> None:
    import ctypes
    from types import SimpleNamespace

    from eves_text import win_theme

    class FakeDwm:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            # Атрибут 20 не поддержан, атрибут 19 сработал.
            self.results = [1, 0]

        def DwmSetWindowAttribute(self, hwnd, attribute, value, size):
            pointer = ctypes.cast(value, ctypes.POINTER(ctypes.c_int))
            self.calls.append((attribute, pointer.contents.value))
            return self.results.pop(0)

    class FakeUser32:
        def __init__(self) -> None:
            self.calls = 0

        def SetWindowPos(self, *args):
            self.calls += 1
            return 1

    # На Linux вызов ничего не делает и не падает.
    win_theme.apply_dark_titlebar(42, True)
    assert win_theme.gdk_window_handle(None) == 0
    assert win_theme.gdk_window_handle(object()) == 0

    original = (win_theme._DWMAPI, win_theme._USER32, win_theme.os)
    dwm = FakeDwm()
    user32 = FakeUser32()
    try:
        win_theme._DWMAPI = dwm
        win_theme._USER32 = user32
        win_theme.os = SimpleNamespace(name="nt")
        win_theme.apply_dark_titlebar(42, True)
    finally:
        win_theme._DWMAPI, win_theme._USER32, win_theme.os = original

    assert dwm.calls == [(20, 1), (19, 1)], dwm.calls
    assert user32.calls == 1


def test_caret_blinker() -> None:
    from eves_text.caret import CaretBlinker

    window = Gtk.OffscreenWindow()
    view = Gtk.TextView()
    view.set_left_margin(6)
    view.set_top_margin(4)
    window.add(view)
    window.show_all()
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    view.get_buffer().set_text("Привет\nмир")
    while Gtk.events_pending():
        Gtk.main_iteration_do(False)

    blinker = CaretBlinker(view, dark=True)
    # Штатная каретка скрыта: вместо неё рисуется своя.
    assert view.get_cursor_visible() is False

    rect = blinker._caret_rect()
    assert rect is not None
    left, top, width, height = rect
    assert width >= 1, rect
    assert height > 0, rect
    assert top >= 0, rect
    assert left >= 0, rect

    # В тёмной теме каретка светлая: системный caret-color в Windows этим
    # не управляет, поэтому цвет задаётся явно и меняется с темой.
    color = blinker._cursor_color()
    assert color.red > 0.75 and color.green > 0.75 and color.blue > 0.75, color
    assert 0.0 <= color.alpha <= 1.0
    blinker.set_dark(False)
    color = blinker._cursor_color()
    assert color.red < 0.2 and color.green < 0.2 and color.blue < 0.2, color
    blinker.set_dark(True)

    blinker.stop()
    assert blinker._timeout == 0
    window.destroy()


def test_app_logo() -> None:
    from eves_text.window import app_logo

    logo = app_logo()
    assert logo is not None
    assert logo.get_width() > 0 and logo.get_height() > 0


def test_dark_scheme() -> None:
    manager = GtkSource.StyleSchemeManager.get_default()
    styles_dir = str(ROOT / "eves_text" / "resources" / "styles")
    if styles_dir not in manager.get_search_path():
        manager.append_search_path(styles_dir)

    scheme = manager.get_scheme("eves-dark")
    assert scheme is not None

    text = scheme.get_style("text")
    assert text is not None
    assert text.get_property("background") == "#2b2b2b"

    current = scheme.get_style("current-line")
    assert current is not None
    assert current.get_property("background") == "#4a4a4a"

    # Полосы, чей фон задаёт GTK, должны краситься и без тёмной темы GTK
    # (так бывает в Windows-бандле, если Adwaita-dark не нашлась).
    css = (ROOT / "eves_text" / "resources" / "style.css").read_text(encoding="utf-8")
    for selector in (
        ".dark-theme menubar",
        ".dark-theme .bottom-bar",
        ".dark-theme .statusbar",
        ".empty-state",
        ".dark-theme .empty-state",
        ".empty-state button",
    ):
        assert selector in css

    # Выпадающие списки и меню живут в отдельных окнах: для них нужен
    # глобальный тёмный CSS, а не только класс .dark-theme на главном окне.
    dark_css = (ROOT / "eves_text" / "resources" / "style-dark.css").read_text(
        encoding="utf-8"
    )
    for selector in ("menu,", "combobox", "treeview", "entry", "menuitem"):
        assert selector in dark_css
    assert "set_dark_style" in (ROOT / "eves_text" / "application.py").read_text(
        encoding="utf-8"
    )

    build = (ROOT / "windows" / "build-portable.sh").read_text(encoding="utf-8")
    assert "Adwaita-dark" in build


def test_button_hover() -> None:
    """Подсветка кнопок должна задаваться явными цветами, не currentColor."""
    css = (ROOT / "eves_text" / "resources" / "style.css").read_text(encoding="utf-8")
    assert "alpha(currentColor" not in css
    assert "alpha(@theme_fg_color" not in css

    def declaration(selector: str) -> str:
        start = css.index(selector)
        brace = css.index("{", start)
        end = css.index("}", brace)
        return css[brace + 1 : end]

    for selector in (
        ".bottom-bar button:hover",
        ".bottom-bar button:active,",
        ".new-tab:hover",
        ".new-tab:active",
        ".tab-close:hover",
    ):
        assert "rgba(" in declaration(selector), selector

    for selector in (
        ".dark-theme .bottom-bar button:hover",
        ".dark-theme .bottom-bar button:active,",
        ".dark-theme .new-tab:hover",
        ".dark-theme .new-tab:active",
        ".dark-theme .tab-close:hover",
    ):
        assert "rgba(255, 255, 255" in declaration(selector), selector


def test_windows_installer() -> None:
    import ctypes
    from types import SimpleNamespace

    from eves_text import updates

    # Не-Windows: автоматическая установка не запускается.
    assert updates.launch_windows_installer(Path("setup.exe"))[0] is False

    class FakeShellExecute:
        def __init__(self, result: int) -> None:
            self.result = result
            self.calls: list[tuple] = []

        def __call__(self, *args):
            self.calls.append(args)
            return self.result

    shell = FakeShellExecute(42)
    fake_windll = SimpleNamespace(shell32=SimpleNamespace(ShellExecuteW=shell))
    original_platform = updates.platform_kind
    original_windll = getattr(ctypes, "windll", None)

    def patch() -> None:
        updates.platform_kind = lambda: "windows"
        ctypes.windll = fake_windll

    def restore() -> None:
        updates.platform_kind = original_platform
        if original_windll is None:
            del ctypes.windll
        else:
            ctypes.windll = original_windll

    with tempfile.TemporaryDirectory() as tmp:
        setup = Path(tmp) / "EvesText-9.9.9-win64-setup.exe"
        setup.write_bytes(b"setup")
        try:
            patch()
            ok, error = updates.launch_windows_installer(setup)
        finally:
            restore()

        assert ok and error == "", (ok, error)
        assert shell.calls, shell.calls
        hwnd, verb, path, parameters, directory, show = shell.calls[0]
        assert hwnd is None
        assert verb == "runas"
        assert path == str(setup)
        assert "/SILENT" in parameters and "/CLOSEAPPLICATIONS" in parameters
        assert directory == str(setup.parent)
        assert show == 1

        shell.result = 5
        try:
            patch()
            ok, error = updates.launch_windows_installer(setup)
        finally:
            restore()
        assert not ok and "UAC" in error, (ok, error)


def test_linux_installer() -> None:
    import subprocess
    from types import SimpleNamespace

    from eves_text import updates

    # На не-Linux автоматическая установка не запускается.
    original_platform = updates.platform_kind
    try:
        updates.platform_kind = lambda: "windows"
        assert updates.launch_linux_installer(Path("update.deb"))[0] is False
    finally:
        updates.platform_kind = original_platform

    with tempfile.TemporaryDirectory() as tmp:
        deb = Path(tmp) / "eves-text_9.9.9-1_all.deb"
        deb.write_bytes(b"deb")

        calls: list[tuple] = []

        def fake_run(command, **kwargs):
            env = kwargs.get("env")
            if env is not None:
                askpass = Path(env["SUDO_ASKPASS"])
                assert askpass.is_file()
                assert askpass.stat().st_mode & 0o111
                assert "zenity" in askpass.read_text(encoding="utf-8")
            calls.append((command, kwargs))
            return SimpleNamespace(returncode=0, stdout="")

        fake_subprocess = SimpleNamespace(
            run=fake_run,
            PIPE=subprocess.PIPE,
            STDOUT=subprocess.STDOUT,
            TimeoutExpired=subprocess.TimeoutExpired,
        )
        original_subprocess = updates.subprocess
        original_which = updates.shutil.which
        try:
            updates.subprocess = fake_subprocess
            updates.shutil.which = lambda name: f"/usr/bin/{name}"
            ok, error = updates.launch_linux_installer(deb)
        finally:
            updates.subprocess = original_subprocess
            updates.shutil.which = original_which

        assert ok and error == "", (ok, error)
        command, kwargs = calls[0]
        assert command == [
            "/usr/bin/sudo",
            "-A",
            "/usr/bin/dpkg",
            "-i",
            str(deb),
        ], command
        assert kwargs["timeout"] == 300
        assert kwargs["env"]["SUDO_ASKPASS"].endswith("askpass.sh")

        # Без sudo остаётся pkexec.
        try:
            updates.subprocess = fake_subprocess
            updates.shutil.which = (
                lambda name: None if name == "sudo" else f"/usr/bin/{name}"
            )
            ok, error = updates.launch_linux_installer(deb)
        finally:
            updates.subprocess = original_subprocess
            updates.shutil.which = original_which

        assert ok and error == "", (ok, error)
        command, _kwargs = calls[1]
        assert command == [
            "/usr/bin/pkexec",
            "/usr/bin/dpkg",
            "-i",
            str(deb),
        ], command

        # Без sudo (с askpass) и pkexec — понятная подсказка.
        try:
            updates.subprocess = fake_subprocess
            updates.shutil.which = (
                lambda name: f"/usr/bin/{name}"
                if name == "dpkg"
                else None
            )
            ok, error = updates.launch_linux_installer(deb)
        finally:
            updates.subprocess = original_subprocess
            updates.shutil.which = original_which
        assert not ok and "sudo dpkg -i" in error, error


def test_linux_restart() -> None:
    from types import SimpleNamespace

    from eves_text import updates

    captured: dict = {}

    class FakePopen:
        def __init__(self, command, **kwargs):
            captured["command"] = command
            captured["kwargs"] = kwargs

    fake_subprocess = SimpleNamespace(Popen=FakePopen, DEVNULL=object())
    original = updates.subprocess
    try:
        updates.subprocess = fake_subprocess
        ok, error = updates.restart_after_exit()
    finally:
        updates.subprocess = original

    assert ok and error == "", (ok, error)
    command = captured["command"]
    assert command[0] == "/bin/sh"
    assert command[1] == "-c"
    assert "kill -0" in command[2]
    assert len(command) > 5
    assert captured["kwargs"]["start_new_session"] is True


def test_window() -> Application:
    from eves_text import settings as user_settings

    app = Application()
    app.register(None)

    # Тест не должен зависеть от настроек, сохранённых на этом компьютере.
    original_load = user_settings.load
    original_save = user_settings.save
    user_settings.load = lambda: {}
    user_settings.save = lambda data: None

    window = MainWindow(application=app)
    window.set_text("Он пришёл в течении дня.", Path("/tmp/пример.txt"))
    assert window.get_text() == "Он пришёл в течении дня."
    assert window.get_title().startswith("пример.txt")
    assert window.pos_label.get_text() == "Стр 1, Стлб 1"

    window.buffer.set_modified(True)
    assert window.get_title().startswith("• ")

    window.pos_label.get_text()
    window._update_counts()
    assert "симв" in window.counts_label.get_text()

    # Вкладки: каждая хранит свой документ независимо от других.
    window.buffer.set_modified(False)
    first = window._active_tab
    window.action_new()
    assert len(window._tabs) == 2
    second = window._active_tab
    assert second is not first
    assert window.get_text() == ""
    assert second.name_label.get_text() == "Документ 2"
    # Текст внутри вкладки не прилипает к краям, справа от крестика есть зазор.
    assert second.label_box.get_margin_start() >= 20
    assert second.label_box.get_margin_end() >= 18
    assert second.label_box.get_spacing() >= 6
    # Плюс высотой с заглавную букву в тексте вкладки.
    assert window.new_tab_button.get_image().get_pixel_size() == 13
    assert second.tab.get_style_context().has_class("active")
    assert not first.tab.get_style_context().has_class("active")
    # При наведении неактивная вкладка подсвечивается и показывает крестик.
    assert second.close_button.get_opacity() > 0.8
    assert first.close_button.get_opacity() == 0.0
    first.tab.emit("enter-notify-event", Gdk.Event.new(Gdk.EventType.ENTER_NOTIFY))
    assert first.hovered is True
    assert first.close_button.get_opacity() > 0.8
    first.tab.emit("leave-notify-event", Gdk.Event.new(Gdk.EventType.LEAVE_NOTIFY))
    assert first.hovered is False
    assert first.close_button.get_opacity() == 0.0
    # «+» — последний элемент полосы, сразу за вкладками.
    assert window.tab_bar.get_children()[-1] is window.new_tab_button

    # Вкладки можно перетаскивать мышью: порядок списка совпадает с порядком
    # виджетов на полосе, а «+» остаётся последним.
    assert window._move_tab(first, 1)
    assert window._tabs == [second, first]
    assert window.tab_bar.get_children() == [
        second.tab,
        first.tab,
        window.new_tab_button,
    ]
    # Перестановка неактивной вкладки не меняет активную.
    assert window._active_tab is second

    # Геометрия плашек в невидимом окне: две вкладки по 130 px.
    for index, candidate in enumerate(window._tabs):
        allocation = candidate.tab.get_allocation()
        allocation.x = index * 130
        allocation.width = 130
        candidate.tab.set_allocation(allocation)
    assert window._drop_position_at(20) == 0
    assert window._drop_position_at(150) == 1
    assert window._drop_position_at(400) == 2

    # Нажатие только запоминает вкладку: порядок меняется после сдвига.
    press = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    press.button = 1
    press.x = press.y = 5.0
    press.x_root, press.y_root = 10.0, 20.0
    assert first.tab.emit("button-press-event", press) is True
    assert window._drag_candidate is first
    assert window._drag_tab is None
    assert window._tabs == [second, first]

    # Сдвиг меньше порога не считается перетаскиванием.
    motion = Gdk.Event.new(Gdk.EventType.MOTION_NOTIFY)
    motion.state = Gdk.ModifierType.BUTTON1_MASK
    motion.x_root, motion.y_root = 13.0, 20.0
    assert first.tab.emit("motion-notify-event", motion) is False
    assert window._drag_tab is None

    # После сдвига больше порога вкладка «живо» встаёт на новое место.
    # Полоса в тесте не показана, поэтому подменяем её GDK-окно.
    class FakeGdkWindow:
        def get_root_coords(self, _x, _y):
            return 0, 0

    original_get_window = window.tab_bar.get_window
    window.tab_bar.get_window = FakeGdkWindow
    motion.x_root, motion.y_root = 20.0, 20.0
    try:
        assert first.tab.emit("motion-notify-event", motion) is True
    finally:
        window.tab_bar.get_window = original_get_window
    assert window._drag_tab is first
    assert first.tab.get_style_context().has_class("dragging")
    assert window._tabs == [first, second]
    assert window.tab_bar.get_children() == [
        first.tab,
        second.tab,
        window.new_tab_button,
    ]

    # Отпускание мыши завершает перетаскивание и снимает обводку вкладки.
    release = Gdk.Event.new(Gdk.EventType.BUTTON_RELEASE)
    release.button = 1
    assert second.tab.emit("button-release-event", release) is False
    assert first.tab.emit("button-release-event", release) is True
    assert window._drag_candidate is None
    assert window._drag_tab is None
    assert not first.tab.get_style_context().has_class("dragging")
    # Нажатие сделало вкладку активной — вернём вторую для следующих проверок.
    window._select_tab(second)

    # Подсветка кнопок не зависит от псевдокласса :hover: проверяем, что
    # обработчики наведения ставят и снимают аварийный CSS-класс.
    from eves_text.window import DEFAULT_FONT_SIZE, HOVER_CLASS, FontSizeEntry

    enter = Gdk.Event.new(Gdk.EventType.ENTER_NOTIFY)
    leave = Gdk.Event.new(Gdk.EventType.LEAVE_NOTIFY)
    for button in (
        window.font_larger,
        window.font_smaller,
        window.align_buttons["left"],
        window.ai_button,
        window.new_tab_button,
        window._active_tab.close_button,
    ):
        button.emit("enter-notify-event", enter)
        assert button.get_style_context().has_class(HOVER_CLASS)
        button.emit("leave-notify-event", leave)
        assert not button.get_style_context().has_class(HOVER_CLASS)

    # У размера шрифта нет встроенных стрелок, которые проявлялись при нажатии
    # и дублировали кнопки «−» и «+». Работают только видимые кнопки и ввод.
    assert isinstance(window.font_size_spin, FontSizeEntry)
    assert not isinstance(window.font_size_spin, Gtk.SpinButton)
    assert window.font_size_spin.get_value() == DEFAULT_FONT_SIZE
    window.font_larger.clicked()
    assert window.font_size_spin.get_value() == DEFAULT_FONT_SIZE + 1
    window.font_smaller.clicked()
    assert window.font_size_spin.get_value() == DEFAULT_FONT_SIZE
    window.set_text("Второй документ", Path("/tmp/второй.txt"))
    assert "второй.txt" in window.get_title()
    assert "2 сл." in window.counts_label.get_text()

    window._select_tab(first)
    assert window.get_text() == "Он пришёл в течении дня."
    assert "пример.txt" in window.get_title()
    assert "5 сл." in window.counts_label.get_text()

    first.buffer.set_modified(True)
    assert window.get_title().startswith("• ")
    assert first.name_label.get_text().startswith("• ")
    first.buffer.set_modified(False)

    assert window.close_tab(second)
    assert len(window._tabs) == 1
    assert window._active_tab is first
    assert window.get_text() == "Он пришёл в течении дня."

    # Закрытие последней вкладки показывает экран с двумя действиями.
    assert window.close_tab(first)
    assert window._tabs == []
    assert window._active_tab is None
    assert window.stack.get_visible_child() is window.empty_state
    assert window.tab_strip_row.get_visible() is False
    assert window.bottom_bar.get_visible() is False
    assert window.get_title() == "Eve's Text"
    assert window.pos_label.get_text() == ""
    assert window.lookup_action("save").get_enabled() is False
    assert window.lookup_action("undo").get_enabled() is False

    # Кнопка «Открыть существующий» вызывает диалог открытия файла.
    original_open = window.action_open
    opened = []
    window.action_open = lambda *args: opened.append(True)
    try:
        window.empty_open_button.clicked()
    finally:
        window.action_open = original_open
    assert opened == [True]

    # Кнопка «Новый документ» возвращает окно к работе; после закрытия всех
    # вкладок нумерация документов начинается заново с единицы.
    window.empty_new_button.clicked()
    assert len(window._tabs) == 1
    assert window._active_tab is not None
    assert window._tabs[0].name_label.get_text() == "Документ 1"
    assert window.stack.get_visible_child() is window._active_tab.scroller
    assert window.tab_strip_row.get_visible() is True
    assert window.bottom_bar.get_visible() is True
    assert window.lookup_action("save").get_enabled() is True

    # Переключатель темы в меню «Вид» меняет схему, состояние пункта меню
    # и настройки GTK; файл настроек в тесте не трогаем.
    from eves_text import settings as user_settings

    original_save = user_settings.save
    user_settings.save = lambda data: None
    gtk_settings = Gtk.Settings.get_default()
    old_dark = gtk_settings.get_property("gtk-application-prefer-dark-theme")
    old_theme = gtk_settings.get_property("gtk-theme-name")
    try:
        # В Linux заголовок рисует само приложение: его цвет следует за темой.
        if os.name != "nt":
            assert window._header_bar is not None
            assert window._header_bar.get_style_context().has_class("app-titlebar")
            assert window._header_bar.get_title() == window.get_title()

        action = window.lookup_action("dark-theme")
        assert action is not None
        initial = window._dark_theme
        # На Windows каретка рисуется своей. Проверяем, что смена темы
        # обновляет её цвет; на Linux создаём объект вручную для теста.
        from eves_text.caret import CaretBlinker

        caret_tab = window._active_tab
        caret_tab.caret = CaretBlinker(caret_tab.view, dark=initial)
        assert (caret_tab.caret._cursor_color().red > 0.75) is initial

        window.activate_action("dark-theme", None)
        assert window._dark_theme is (not initial)
        assert window.get_style_context().has_class("dark-theme") is (not initial)
        assert action.get_state().get_boolean() is (not initial)
        assert (caret_tab.caret._cursor_color().red > 0.75) is (not initial)
        if Gdk.Screen.get_default() is not None:
            # Глобальный тёмный CSS включается для popup-меню и списков.
            assert app._dark_style_active is (not initial)
        window.activate_action("dark-theme", None)
        assert window._dark_theme is initial
        assert (caret_tab.caret._cursor_color().red > 0.75) is initial
        if Gdk.Screen.get_default() is not None:
            assert app._dark_style_active is initial
    finally:
        gtk_settings.set_property("gtk-application-prefer-dark-theme", old_dark)
        gtk_settings.set_property("gtk-theme-name", old_theme)
        user_settings.save = original_save

    # Средняя кнопка мыши: по вкладке закрывает её, по пустому месту полосы
    # открывает новую — как в браузере.
    window.action_new()
    assert len(window._tabs) == 2
    closing = window._active_tab
    middle = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
    middle.button = 2
    assert closing.tab.emit("button-press-event", middle) is True
    assert len(window._tabs) == 1
    assert closing not in window._tabs
    assert window.tab_strip_event.emit("button-press-event", middle) is True
    assert len(window._tabs) == 2
    assert window.tab_bar.get_children()[-1] is window.new_tab_button

    window.destroy()
    user_settings.load = original_load
    user_settings.save = original_save
    return app


def test_settings(app: Application) -> None:
    """Настройки по умолчанию загружаются, меняются в диалоге и сохраняются."""
    from eves_text import settings as user_settings

    def open_settings() -> None:
        """Действие меню откладывает диалог до следующего цикла GTK."""
        window.action_settings()
        while Gtk.events_pending():
            Gtk.main_iteration()

    original_load = user_settings.load
    original_save = user_settings.save
    saved = []
    user_settings.load = lambda: {"font_size": 20, "default_align": "center", "theme": "light"}
    user_settings.save = lambda data: saved.append(dict(data))

    try:
        window = MainWindow(application=app)
        try:
            assert window._font_size == 20
            assert window.font_size_spin.get_value() == 20
            assert window._default_align == "center"
            assert window._active_tab is not None
            assert window._active_tab.current_align == "center"
            assert window.align_buttons["center"].get_active() is True
            font = window.view.get_style_context().get_property(
                "font", Gtk.StateFlags.NORMAL
            )
            assert font.get_size() == 20 * Pango.SCALE

            # Пункт меню «Настройки» открывает диалог с первого клика.
            assert window.lookup_action("settings") is not None
            menubar = next(
                child
                for child in window.get_children()[0].get_children()
                if isinstance(child, Gtk.MenuBar)
            )
            settings_item = next(
                child
                for child in menubar.get_children()
                if "Настройки"
                in (
                    child.get_child().get_text()
                    if isinstance(child.get_child(), Gtk.Label)
                    else ""
                )
            )
            press = Gdk.Event.new(Gdk.EventType.BUTTON_PRESS)
            press.button = 1
            assert settings_item.emit("button-press-event", press) is True
            while Gtk.events_pending():
                Gtk.main_iteration()
            dialog = window._settings_dialog
            assert dialog is not None
            assert dialog.get_title() == "Настройки"
            assert window._settings_size_spin is not None
            assert window._settings_size_spin.get_value() == 20
            assert window._settings_align_combo is not None
            assert window._settings_align_combo.get_active_id() == "center"
            # Повторный вызов не создаёт второй диалог.
            open_settings()
            assert window._settings_dialog is dialog

            window._settings_size_spin.set_value(16)
            window._settings_align_combo.set_active(2)  # по правому краю
            dialog.response(Gtk.ResponseType.OK)

            assert window._settings_dialog is None
            assert window._font_size == 16
            assert window.font_size_spin.get_value() == 16
            assert window._default_align == "right"
            assert window.align_buttons["right"].get_active() is True
            assert saved, "настройки должны сохраняться"
            assert saved[-1]["font_size"] == 16
            assert saved[-1]["default_align"] == "right"
            assert saved[-1]["theme"] == "light"

            # Отмена не меняет значения и ничего не сохраняет.
            before = len(saved)
            open_settings()
            window._settings_size_spin.set_value(24)
            window._settings_align_combo.set_active(3)
            window._settings_dialog.response(Gtk.ResponseType.CANCEL)
            assert window._font_size == 16
            assert window._default_align == "right"
            assert len(saved) == before

            # Новая вкладка наследует настройки, текст получает тег выравнивания.
            window.action_new()
            tab = window._active_tab
            assert tab is not None
            assert tab.current_align == "right"
            assert window.align_buttons["right"].get_active() is True
            window.buffer.set_text("Выравнивание по умолчанию")
            iterator = window.buffer.get_iter_at_offset(0)
            assert iterator.has_tag(tab.align_tags["right"])
            font = tab.view.get_style_context().get_property(
                "font", Gtk.StateFlags.NORMAL
            )
            assert font.get_size() == 16 * Pango.SCALE

            # Открытый файл тоже получает выравнивание по умолчанию.
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "файл.txt"
                path.write_text("Первая строка\nВторая строка", encoding="utf-8")
                window.open_path(str(path))
                opened = window._active_tab
                assert opened is not None
                iterator = opened.buffer.get_iter_at_offset(0)
                assert iterator.has_tag(opened.align_tags["right"])
                last = opened.buffer.get_end_iter()
                last.backward_char()
                assert last.has_tag(opened.align_tags["right"])
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save


if __name__ == "__main__":
    test_document()
    test_updates()
    test_win_theme()
    test_caret_blinker()
    test_app_logo()
    test_dark_scheme()
    test_button_hover()
    test_windows_installer()
    test_linux_installer()
    test_linux_restart()
    test_app = test_window()
    test_settings(test_app)
    print("OK: smoke test пройден")
