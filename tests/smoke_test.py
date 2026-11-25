#!/usr/bin/env python3
"""Дымовой тест: проверяет ввод-вывод файлов и сборку окна без показа на экране."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gio, Gtk, Gdk, GtkSource, GLib, Pango  # noqa: E402

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


def test_document_rtf() -> None:
    """Оформление живёт в RTF, обычный текст остаётся чистым."""
    with tempfile.TemporaryDirectory() as tmp:
        text = "Обычный текст. Жирный кусок и 18-й размер. Ещё строка. Наклон и линия."
        spans = [
            (15, 28, {"bold": True, "align": "left"}),
            (33, 50, {"size": 18, "align": "left"}),
            (55, 70, {"italic": True, "underline": True, "align": "left"}),
        ]
        path = Path(tmp) / "оформление.rtf"
        document.write_document(path, text, spans, default_size=12)

        loaded, encoding, loaded_spans = document.read_document(path)
        assert loaded == text, repr(loaded)
        assert encoding == "RTF", encoding
        bold = [span for span in loaded_spans if span[2].get("bold")]
        sized = [span for span in loaded_spans if span[2].get("size") == 18]
        italic = [span for span in loaded_spans if span[2].get("italic")]
        underline = [span for span in loaded_spans if span[2].get("underline")]
        assert any(span[0] <= 15 and span[1] >= 28 for span in bold), loaded_spans
        assert any(span[0] <= 33 and span[1] >= 50 for span in sized), loaded_spans
        assert any(span[0] <= 55 and span[1] >= 70 for span in italic), loaded_spans
        assert any(span[0] <= 55 and span[1] >= 70 for span in underline), loaded_spans

        # Текст между кусками не должен стать жирным или крупным.
        middle = next(span for span in loaded_spans if span[0] <= 30 < span[1])
        assert not middle[2].get("bold"), middle
        assert not middle[2].get("italic"), middle
        assert not middle[2].get("underline"), middle
        assert middle[2].get("size") != 18, middle

        plain = Path(tmp) / "обычный.txt"
        document.write_document(plain, text, [])
        assert not plain.read_text(encoding="utf-8").startswith("{\\rtf")
        loaded, encoding, loaded_spans = document.read_document(plain)
        assert loaded == text and encoding == "UTF-8" and loaded_spans == []

        # Файл определяется как RTF по содержимому, а не только по имени.
        # BOM, UTF-16 и оформление в .txt раньше показывали сырой код.
        raw = path.read_bytes()
        with_bom = Path(tmp) / "с-bom.rtf"
        with_bom.write_bytes(b"\xef\xbb\xbf" + raw)
        loaded, encoding, _loaded_spans = document.read_document(with_bom)
        assert loaded == text and encoding == "RTF", repr(loaded[:40])

        utf16 = Path(tmp) / "utf16.rtf"
        utf16.write_bytes(raw.decode("ascii").encode("utf-16"))
        loaded, encoding, _loaded_spans = document.read_document(utf16)
        assert loaded == text and encoding == "RTF", repr(loaded[:40])

        urtf = Path(tmp) / "urtf.rtf"
        urtf.write_bytes(raw.replace(b"{\\rtf", b"{\\urtf", 1))
        loaded, encoding, _loaded_spans = document.read_document(urtf)
        assert loaded == text and encoding == "RTF", repr(loaded[:40])

        formatted_txt = Path(tmp) / "оформление-без-расширения.txt"
        document.write_document(formatted_txt, text, spans)
        loaded, encoding, _loaded_spans = document.read_document(formatted_txt)
        assert loaded == text and encoding == "RTF", repr(loaded[:40])

        # Реальные RTF из Word используют \'hh и резервные символы после \uN.
        word_source = (
            r"{\rtf1\ansi\ansicpg1251\uc1\fs24 "
            r"\'ED\'EC\'E8 \u1055\'ff \par}"
        )
        loaded, _word_spans = document._parse_rtf(word_source)
        assert loaded == "нми П \n", repr(loaded)


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
        ".empty-state button label",
        ".dark-theme .empty-state button label",
    ):
        assert selector in css

    # На Windows тема рисует кнопки градиентом: без явного background-image
    # кнопки остаются светлыми, а текст от тёмной темы — белым.
    def css_block(selector: str) -> str:
        start = css.index(selector)
        brace = css.index("{", start)
        return css[brace + 1 : css.index("}", brace)]

    dark_button = css_block(".dark-theme .empty-state button {")
    assert "background-color: #303134" in dark_button
    assert "background-image: none" in dark_button
    assert "color: #e3e3e3" in dark_button

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


def test_check_icon_parser() -> None:
    """Парсер PE-ресурсов должен находить иконку и в PE32, и в PE32+."""
    import importlib.util
    import struct

    spec = importlib.util.spec_from_file_location(
        "check_icon", ROOT / "windows" / "check_icon.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def resource_dir(
        blob: bytearray, base: int, records: list[tuple[int, int, bool]]
    ) -> None:
        struct.pack_into("<IIHHHH", blob, base, 0, 0, 0, 0, 0, len(records))
        for index, (name, offset, is_dir) in enumerate(records):
            flag = 0x80000000 if is_dir else 0
            struct.pack_into("<II", blob, base + 16 + index * 8, name, offset | flag)

    def build_pe(pe32plus: bool, with_language: bool) -> bytes:
        pe = 0x80
        coff = pe + 4
        opt = coff + 20
        opt_size = 240 if pe32plus else 224
        section = opt + opt_size
        res_rva, res_raw = 0x1000, 0x400
        blob = bytearray(0x800)

        def res(offset: int) -> int:
            return res_raw + offset

        struct.pack_into("<I", blob, 0x3C, pe)
        blob[pe : pe + 4] = b"PE\0\0"
        struct.pack_into(
            "<HHIIIHH",
            blob,
            coff,
            0x8664 if pe32plus else 0x14C,
            1,
            0,
            0,
            0,
            opt_size,
            0x2022,
        )
        struct.pack_into("<H", blob, opt, 0x20B if pe32plus else 0x10B)
        struct.pack_into("<I", blob, opt + (108 if pe32plus else 92), 16)
        directories = opt + (112 if pe32plus else 96)
        struct.pack_into("<II", blob, directories + 16, res_rva, 0x400)
        struct.pack_into("<IIII", blob, section + 8, 0x1000, res_rva, 0x400, res_raw)

        # Тип → имя → (язык) → IMAGE_RESOURCE_DATA_ENTRY. windres может
        # не создавать уровень языка, поэтому проверяем оба варианта.
        resource_dir(blob, res(0x00), [(14, 0x50, True), (3, 0xD4, True)])
        resource_dir(blob, res(0x50), [(1, 0x70, True)])
        resource_dir(
            blob,
            res(0x70),
            [(0x409, 0x90, True)] if with_language else [(0x409, 0xB0, False)],
        )
        if with_language:
            resource_dir(blob, res(0x90), [(0x409, 0xB0, False)])
        struct.pack_into("<IIII", blob, res(0xB0), res_rva + 0x180, 20, 0, 0)
        struct.pack_into("<HHH", blob, res(0x180), 0, 1, 1)
        struct.pack_into(
            "<BBBBHHIH", blob, res(0x186), 16, 16, 0, 0, 1, 32, 32, 7
        )

        resource_dir(blob, res(0xD4), [(7, 0xF4, True)])
        if with_language:
            resource_dir(blob, res(0xF4), [(1, 0x114, True)])
            resource_dir(blob, res(0x114), [(0x409, 0x134, False)])
            icon_data_entry = 0x134
        else:
            resource_dir(blob, res(0xF4), [(1, 0x114, False)])
            icon_data_entry = 0x114
        struct.pack_into(
            "<IIII", blob, res(icon_data_entry), res_rva + 0x200, 32, 0, 0
        )
        blob[res(0x200) : res(0x220)] = b"\x89PNG" + b"\0" * 28
        return bytes(blob)

    with tempfile.TemporaryDirectory() as tmp:
        for pe32plus in (True, False):
            for with_language in (True, False):
                suffix = ("x64" if pe32plus else "x86") + (
                    "-lang" if with_language else "-flat"
                )
                path = Path(tmp) / f"{suffix}.exe"
                path.write_bytes(build_pe(pe32plus, with_language))
                detail = module.check(path)
                assert "RT_ICON 7" in detail, detail

                # В CI консоль Windows может быть с кодовой страницей 1252.
                import subprocess

                env = dict(os.environ, PYTHONIOENCODING="cp1252")
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "windows" / "check_icon.py"),
                        str(path),
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=env,
                )
                assert result.returncode == 0, result.stderr
                assert "RT_ICON 7" in result.stdout, result.stdout


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

    # Инсталлятор должен регистрировать настоящий .exe, а не pythonw.exe.
    iss = (ROOT / "windows" / "eves-text.iss").read_text(encoding="utf-8")
    assert "{app}\\Eve's Text.exe" in iss
    assert "RegisteredApplications" in iss
    assert "FileAssociations" in iss
    assert ".txt" in iss and ".rtf" in iss
    assert "OpenWithProgids" in iss
    assert "AppUserModelID" in iss
    assert "ApplicationIcon" in iss
    assert 'Filename: "{app}\\bin\\pythonw.exe"' not in iss

    launcher_c = (ROOT / "windows" / "launcher.c").read_text(encoding="utf-8")
    assert "wWinMain" in launcher_c
    assert "CreateProcessW" in launcher_c
    assert "SetCurrentProcessExplicitAppUserModelID" in launcher_c

    # Окно GTK принадлежит дочернему pythonw.exe: AUMID должен задаваться
    # и там, иначе Windows показывает системную иконку и группирует окно
    # вместе с pythonw.exe.
    launcher_py = (ROOT / "windows" / "launcher.py").read_text(encoding="utf-8")
    assert "set_app_user_model_id" in launcher_py
    assert "SetCurrentProcessExplicitAppUserModelID" in launcher_py

    # Иконка должна быть встроена в сам .exe, а не только лежать рядом.
    rc = (ROOT / "windows" / "launcher.rc").read_text(encoding="utf-8")
    assert '1 ICON "assets\\\\eves-text.ico"' in rc
    assert "VERSIONINFO" in rc
    assert "@VERSION@" in rc and "@VERSION_COMMA@" in rc

    build = (ROOT / "windows" / "build-portable.sh").read_text(encoding="utf-8")
    assert "launcher.c" in build and "Eve's Text.exe" in build
    assert "-mwindows" in build
    assert "windres" in build and "launcher.rc" in build
    assert "launcher.generated.rc" in build
    assert (ROOT / "windows" / "assets" / "eves-text.ico").is_file()

    check_icon = (ROOT / "windows" / "check_icon.py").read_text(encoding="utf-8")
    assert "RT_GROUP_ICON" in check_icon and "RT_ICON" in check_icon

    ci = (ROOT / ".github" / "workflows" / "windows.yml").read_text(encoding="utf-8")
    assert "mingw-w64-ucrt-x86_64-gcc" in ci
    assert "check_icon.py" in ci


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

    # Подсветка пунктов верхнего меню не зависит от псевдокласса :hover:
    # обработчики наведения ставят и снимают аварийный CSS-класс.
    from eves_text.window import DEFAULT_FONT_SIZE, HOVER_CLASS, FontSizeEntry

    menu_items = window.menubar.get_children()
    assert len(menu_items) == 6
    enter = Gdk.Event.new(Gdk.EventType.ENTER_NOTIFY)
    leave = Gdk.Event.new(Gdk.EventType.LEAVE_NOTIFY)
    for item in menu_items:
        item.emit("enter-notify-event", enter)
        assert item.get_style_context().has_class(HOVER_CLASS)
        item.emit("leave-notify-event", leave)
        assert not item.get_style_context().has_class(HOVER_CLASS)

    # У крестика вкладки нет отдельной ручной подсветки: иначе получается
    # двойное выделение поверх обычного :hover.
    close = window._active_tab.close_button
    close.emit("enter-notify-event", enter)
    assert not close.get_style_context().has_class(HOVER_CLASS)
    root = Path(__file__).parent.parent
    css = (root / "eves_text/resources/style.css").read_text()
    assert ".tab-close.force-hover" not in css
    assert "_close_hover_rgba" not in (root / "eves_text/window.py").read_text()

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


def test_formatting(app: Application) -> None:
    """Начертания, размер куска текста и drop файлов работают в окне."""
    from eves_text import settings as user_settings

    original_load = user_settings.load
    original_save = user_settings.save
    user_settings.load = lambda: {}
    user_settings.save = lambda data: None

    try:
        window = MainWindow(application=app)
        try:
            # Кнопки подписывает само начертание; лишней подписи нет.
            assert window.bold_button.get_label() == "B"
            assert window.italic_button.get_label() == "I"
            assert window.underline_button.get_label() == "U"
            assert "<b>B</b>" in window.bold_button.get_child().get_label()
            assert "<i>I</i>" in window.italic_button.get_child().get_label()
            assert "<u>U</u>" in window.underline_button.get_child().get_label()

            def label_texts(widget) -> list[str]:
                found: list[str] = []
                if isinstance(widget, Gtk.Label):
                    found.append(widget.get_text())
                if hasattr(widget, "get_children"):
                    for child in widget.get_children():
                        found.extend(label_texts(child))
                return found

            assert "Выравнивание:" not in label_texts(window.bottom_bar_content)

            text = "Жирный и обычный"
            window.set_text(text, Path("/tmp/формат.txt"))
            tab = window._active_tab
            assert tab is not None

            buffer = window.buffer
            buffer.select_range(
                buffer.get_iter_at_offset(0), buffer.get_iter_at_offset(6)
            )
            window.action_bold()
            assert buffer.get_iter_at_offset(1).has_tag(tab.bold_tag)
            assert not buffer.get_iter_at_offset(8).has_tag(tab.bold_tag)
            assert window.bold_button.get_active() is True

            window.action_italic()
            window.action_underline()
            assert buffer.get_iter_at_offset(1).has_tag(tab.italic_tag)
            assert buffer.get_iter_at_offset(1).has_tag(tab.underline_tag)
            assert not buffer.get_iter_at_offset(8).has_tag(tab.italic_tag)
            assert window.italic_button.get_active() is True
            assert window.underline_button.get_active() is True

            # Без выделения плюс/минус не меняют весь документ.
            buffer.place_cursor(buffer.get_iter_at_offset(0))
            font_before = window.view.get_style_context().get_property(
                "font", Gtk.StateFlags.NORMAL
            ).get_size()
            window._change_font_size(1)
            font_after = window.view.get_style_context().get_property(
                "font", Gtk.StateFlags.NORMAL
            ).get_size()
            assert font_after == font_before, (font_before, font_after)
            assert not any(
                list(window._tag_ranges(buffer, tag))
                for tag in tab.size_tags.values()
            )
            window.font_size_spin.set_value(window._font_size)
            tab.active_size = None

            # Одно общее поле меняет размер только у выделенного текста.
            buffer.select_range(
                buffer.get_iter_at_offset(7), buffer.get_iter_at_offset(16)
            )
            window.font_size_spin.set_value(18)
            tag_18 = tab.size_tags[18]
            assert buffer.get_iter_at_offset(8).has_tag(tag_18)
            assert not buffer.get_iter_at_offset(1).has_tag(tag_18)

            # Курсор внутри оформленного куска: плюс меняет только значение
            # поля для следующего ввода, но не само оформление.
            buffer.place_cursor(buffer.get_iter_at_offset(8))
            window._change_font_size(1)
            assert window.font_size_spin.get_value() == 19
            assert tab.active_size == 19
            assert buffer.get_iter_at_offset(8).has_tag(tag_18)
            window.font_size_spin.set_value(18)
            tab.active_size = None
            buffer.place_cursor(buffer.get_iter_at_offset(0))

            spans = window._collect_spans(tab)
            assert any(values.get("bold") for _a, _b, values in spans), spans
            assert any(values.get("italic") for _a, _b, values in spans), spans
            assert any(values.get("underline") for _a, _b, values in spans), spans
            assert any(values.get("size") == 18 for _a, _b, values in spans), spans
            assert window._has_rtf_content(tab)

            with tempfile.TemporaryDirectory() as tmp:
                rtf = Path(tmp) / "оформление.rtf"
                assert window._write_tab(tab, rtf)
                raw = rtf.read_bytes()
                assert raw.startswith(b"{\\rtf"), raw[:20]
                loaded, encoding, loaded_spans = document.read_document(rtf)
                assert loaded == text, repr(loaded)
                assert encoding == "RTF"
                assert any(values.get("bold") for _a, _b, values in loaded_spans)
                assert any(values.get("italic") for _a, _b, values in loaded_spans)
                assert any(values.get("underline") for _a, _b, values in loaded_spans)
                assert any(values.get("size") == 18 for _a, _b, values in loaded_spans)

                # Загруженный RTF восстанавливает начертания в буфере.
                window.action_new()
                reloaded = window._active_tab
                assert reloaded is not None
                window._load_tab_text(reloaded, loaded, rtf, "RTF", loaded_spans)
                assert reloaded.buffer.get_iter_at_offset(1).has_tag(
                    reloaded.bold_tag
                )
                assert reloaded.buffer.get_iter_at_offset(1).has_tag(
                    reloaded.italic_tag
                )
                assert reloaded.buffer.get_iter_at_offset(1).has_tag(
                    reloaded.underline_tag
                )

                # Файл без оформления остаётся обычным текстом.
                window.action_new()
                clean = window._active_tab
                assert clean is not None
                window.set_text("Чистый текст")
                clean = window._active_tab
                plain = Path(tmp) / "чистый.txt"
                assert window._write_tab(clean, plain)
                assert not plain.read_text(encoding="utf-8").startswith("{\\rtf")
                assert not window._has_rtf_content(clean)

                # Брошенный в окно файл открывается в отдельной вкладке.
                dropped = Path(tmp) / "из dragon drop.txt"
                dropped.write_text("Текст из брошенного файла", encoding="utf-8")
                uri = Gio.File.new_for_path(str(dropped)).get_uri()
                before = len(window._tabs)
                assert window._open_dropped_uris([uri]) is True
                assert len(window._tabs) == before + 1
                assert window.get_text() == "Текст из брошенного файла"

            # Ввод после включения начертаний продолжает оформление.
            window.action_new()
            typing = window._active_tab
            assert typing is not None
            window.set_text("")
            typing = window._active_tab
            typing.bold_typing = True
            typing.italic_typing = True
            typing.underline_typing = True
            position = window.buffer.get_end_iter()
            window.buffer.insert(position, "новый")
            assert window.buffer.get_iter_at_offset(1).has_tag(typing.bold_tag)
            assert window.buffer.get_iter_at_offset(1).has_tag(typing.italic_tag)
            assert window.buffer.get_iter_at_offset(1).has_tag(typing.underline_tag)
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save


def test_native_file_dialog_filters() -> None:
    """В Windows фильтры только по расширениям — иначе GTK не вызовет IFileDialog."""

    class FakeChooser:
        def __init__(self) -> None:
            self.filters: list[Gtk.FileFilter] = []

        def add_filter(self, file_filter: Gtk.FileFilter) -> None:
            self.filters.append(file_filter)

    from eves_text import window as window_module

    # Windows: ни одного MIME-правила, все фильтры по расширениям.
    with (
        mock.patch.object(window_module, "os", SimpleNamespace(name="nt")),
        mock.patch.object(Gtk.FileFilter, "add_mime_type") as add_mime,
    ):
        chooser = FakeChooser()
        MainWindow._add_filters(chooser)  # type: ignore[arg-type]
    assert add_mime.call_count == 0
    assert [f.get_name() for f in chooser.filters] == [
        "Форматированный текст (RTF)",
        "Текстовые файлы",
        "Все файлы",
    ]

    # Linux: MIME-правила оставляем — нативный диалог там через портал.
    with mock.patch.object(Gtk.FileFilter, "add_mime_type") as add_mime:
        MainWindow._add_filters(FakeChooser())  # type: ignore[arg-type]
    assert add_mime.call_count == 2


def test_drag_and_drop(app: Application) -> None:
    """Бросок .txt открывает файл в новой вкладке — в любом формате данных."""
    from eves_text import settings as user_settings
    from eves_text.window import DND_TARGETS, DND_TEXT_PLAIN, DND_URI_LIST

    # Явный Win32-формат отключает в GDK автоматический CFSTR_SHELLIDLIST →
    # text/uri-list; в OLE2-режиме он нам только мешает.
    assert all(name != "Shell IDList Array" for name, _info in DND_TARGETS)

    class FakeSelectionData:
        """Имитация Gtk.SelectionData без реального X11/Win32-события."""

        def __init__(self, raw: bytes) -> None:
            self.raw = raw

        def get_uris(self) -> list[str]:
            return []

        def get_data(self) -> bytes:
            return self.raw

        def get_text(self) -> str:
            return ""

    original_load = user_settings.load
    original_save = user_settings.save
    user_settings.load = lambda: {}
    user_settings.save = lambda data: None

    try:
        window = MainWindow(application=app)
        try:
            first = window._active_tab
            assert first is not None
            assert first.is_pristine()
            # Бросок на полосу вкладок тоже должен попадать в обработчик.
            assert window.tab_strip_event.drag_dest_get_target_list() is not None

            with tempfile.TemporaryDirectory() as tmp:
                one = Path(tmp) / "первый.txt"
                two = Path(tmp) / "второй.txt"
                one.write_text("Содержимое первого", encoding="utf-8")
                two.write_text("Содержимое второго", encoding="utf-8")
                uri_one = Gio.File.new_for_path(str(one)).get_uri()
                uri_two = Gio.File.new_for_path(str(two)).get_uri()

                # Проводник отдаёт URI-список сырыми байтами с \r\n и \x00.
                raw = f"{uri_one}\r\n{uri_two}\r\n\x00".encode("utf-8")
                with mock.patch.object(Gtk, "drag_finish") as finish:
                    handled = window._on_drag_data(
                        window._active_tab.view, None, 0, 0,
                        FakeSelectionData(raw), DND_URI_LIST, 0,
                    )
                assert handled is True
                assert finish.call_count == 1
                assert len(window._tabs) == 3
                assert window.get_text() == "Содержимое второго"
                assert first.is_pristine(), "чистая вкладка не должна заняться файлом"

                # Даже если активна нетронутая вкладка, файл идёт в новую.
                window.action_new()
                clean = window._active_tab
                assert clean is not None and clean.is_pristine()
                with mock.patch.object(Gtk, "drag_finish"):
                    window._on_drag_data(
                        window._active_tab.view, None, 0, 0,
                        FakeSelectionData(uri_one.encode("utf-8")),
                        DND_URI_LIST, 0,
                    )
                assert len(window._tabs) == 5
                assert clean.is_pristine()
                assert window.get_text() == "Содержимое первого"

                # GDK Win32 иногда даёт обычный текст с путём вместо URI.
                with mock.patch.object(Gtk, "drag_finish"):
                    window._on_drag_data(
                        window._active_tab.view, None, 0, 0,
                        FakeSelectionData(str(two).encode("utf-8")),
                        DND_TEXT_PLAIN, 0,
                    )
                assert len(window._tabs) == 6
                assert window.get_text() == "Содержимое второго"

                # Бросок прямо в поле ввода уже открытого документа: файл
                # открывается рядом, а текст текущей вкладки не портится.
                from gi.repository import GObject

                opened = window._active_tab
                assert opened is not None
                original = window.get_text()
                third = Path(tmp) / "третий.txt"
                third.write_text("Брошено в открытый документ", encoding="utf-8")
                uri_three = Gio.File.new_for_path(str(third)).get_uri()
                with mock.patch.object(Gtk, "drag_finish"), mock.patch.object(
                    GObject, "signal_stop_emission_by_name"
                ) as stop:
                    handled = window._on_drag_data(
                        opened.view, mock.Mock(), 0, 0,
                        FakeSelectionData(uri_three.encode("utf-8")),
                        DND_URI_LIST, 0,
                    )
                assert handled is True
                assert stop.call_count == 1
                assert len(window._tabs) == 7
                assert window._active_tab is not opened
                assert window.get_text() == "Брошено в открытый документ"
                start, end = opened.buffer.get_bounds()
                assert opened.buffer.get_text(start, end, True) == original

                # Если источник предлагает URI, запрашиваем именно его,
                # даже когда find_target вернул текстовую цель.
                source = mock.Mock()
                uri_atom = Gdk.atom_intern("text/uri-list", False)
                source.list_targets.return_value = [uri_atom]
                with mock.patch.object(
                    opened.view, "drag_dest_find_target", return_value=None
                ), mock.patch.object(opened.view, "drag_get_data") as get_data:
                    assert window._on_drag_drop(opened.view, source, 0, 0, 0) is True
                assert get_data.call_args.args[1] == uri_atom

            # Пути Windows без хоста: file:///C:/... и /C:/...
            parsed = MainWindow._dropped_paths(
                "file:///C:/Users/Jean/a.txt\n/C:/Users/Jean/b.txt"
            )
            assert parsed == [Path("C:/Users/Jean/a.txt"), Path("C:/Users/Jean/b.txt")]
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save


def test_com_drop_parsing() -> None:
    """COM-приёмник разбирает CIDA, HDROP и FILEDESCRIPTOR без Windows."""
    import ctypes
    import struct

    from eves_text import win_com_drop

    def pidl(path: str) -> bytes:
        if not path:
            return b"\x00\x00"
        data = path.encode("utf-16-le")
        return struct.pack("<H", len(data) + 2) + data + b"\x00\x00"

    first = r"C:\Users\Jean\первый файл.txt"
    second = r"C:\Users\Jean\документ.rtf"
    parent = pidl("")
    children = [pidl(first), pidl(second)]
    # CIDA: cidl + (cidl + 1) смещений: родитель и cidl относительных PIDL.
    header_size = 4 + 4 * (len(children) + 1)
    offsets = [header_size, header_size + len(parent)]
    for child in children[:-1]:
        offsets.append(offsets[-1] + len(child))
    cida = struct.pack("<I", len(children)) + struct.pack(
        f"<{len(offsets)}I", *offsets
    ) + parent + b"".join(children)

    class FakeShell:
        def __init__(self) -> None:
            self.expected = {
                parent[:-2] + child: path
                for child, path in zip(children, (first, second))
            }

        def SHGetPathFromIDListW(self, pointer, buffer) -> int:
            raw = ctypes.string_at(pointer, 2 + 2 * 260)
            for item, path in self.expected.items():
                if raw.startswith(item):
                    buffer.value = path
                    return 1
            return 0

    assert win_com_drop.paths_from_shell_idlist(cida, FakeShell()) == [first, second]

    class FakeDropShell:
        files = [r"C:\Users\Jean\первый файл.txt", r"C:\Users\Jean\файл.rtf", ""]

        def DragQueryFileW(self, _hdrop, index, buffer, size):
            if index == 0xFFFFFFFF:
                return len(self.files)
            value = self.files[index]
            if buffer is None:
                return len(value)
            buffer.value = value
            return len(value)

    assert win_com_drop.paths_from_hdrop_handle(42, FakeDropShell()) == [
        r"C:\Users\Jean\первый файл.txt",
        r"C:\Users\Jean\файл.rtf",
    ]

    name = "из дескриптора.txt"
    descriptor = (
        struct.pack("<I", 1)
        + b"\x00" * win_com_drop.FILEDESCRIPTORW_NAME_OFFSET
        + name.encode("utf-16-le").ljust(
            win_com_drop.FILEDESCRIPTORW_NAME_SIZE, b"\x00"
        )
    )
    assert win_com_drop.paths_from_file_descriptor(descriptor) == [name]


def test_com_drop_medium_bytes_uses_kernel32() -> None:
    """HGLOBAL читается через kernel32: в ole32 этих функций нет."""
    import ctypes

    from eves_text import win_com_drop

    payload = b"CFSTR_SHELLIDLIST"
    buffer = ctypes.create_string_buffer(payload)

    class FakeKernel32:
        def __init__(self) -> None:
            self.unlocked = 0

        def GlobalSize(self, _handle) -> int:
            return len(payload)

        def GlobalLock(self, _handle) -> int:
            return ctypes.addressof(buffer)

        def GlobalUnlock(self, _handle) -> int:
            self.unlocked += 1
            return 1

    class FakeOle32:
        def __getattr__(self, name: str):
            if name.startswith("Global"):
                raise AssertionError(f"нельзя вызывать ole32.{name}")
            raise AttributeError(name)

    kernel32 = FakeKernel32()
    medium = win_com_drop.STGMEDIUM()
    medium.u.hGlobal = 123
    with (
        mock.patch.object(win_com_drop, "_kernel32", kernel32),
        mock.patch.object(win_com_drop, "_ole32", FakeOle32()),
    ):
        assert win_com_drop._medium_bytes(medium) == payload
    assert kernel32.unlocked == 1


def test_com_drop_dropped_paths_reads_medium() -> None:
    """Полный путь Drop: HGLOBAL → CIDA → список путей → ReleaseStgMedium."""
    import ctypes

    from eves_text import win_com_drop as w

    payload = b"cida"
    buffer = ctypes.create_string_buffer(payload)

    class FakeKernel32:
        def GlobalSize(self, _handle) -> int:
            return len(payload)

        def GlobalLock(self, _handle) -> int:
            return ctypes.addressof(buffer)

        def GlobalUnlock(self, _handle) -> int:
            return 1

    class FakeOle32:
        def __init__(self) -> None:
            self.released = 0

        def ReleaseStgMedium(self, _medium) -> None:
            self.released += 1

        def __getattr__(self, name: str):
            if name.startswith("Global"):
                raise AssertionError(f"нельзя вызывать ole32.{name}")
            raise AttributeError(name)

    medium = w.STGMEDIUM()
    medium.tymed = w.TYMED_HGLOBAL
    medium.u.hGlobal = 123
    ole = FakeOle32()
    seen: list[bytes] = []

    def parse(data: bytes, _shell32) -> list[str]:
        seen.append(data)
        return [r"C:\Users\Jean\файл.txt"]

    with (
        mock.patch.object(w, "_libraries", return_value=(ole, mock.Mock(), 100, 101)),
        mock.patch.object(w, "_kernel32", FakeKernel32()),
        mock.patch.object(w, "_enum_data_formats", return_value={100}),
        mock.patch.object(w, "_get_medium", return_value=medium),
        mock.patch.object(w, "paths_from_shell_idlist", side_effect=parse),
    ):
        assert w.dropped_paths(object()) == [r"C:\Users\Jean\файл.txt"]
    assert seen == [payload]
    assert ole.released == 1


def test_com_drop_registration() -> None:
    """IDropTarget регистрируется на HWND и снимается при закрытии окна."""
    from eves_text import win_com_drop

    class FakeOle:
        def __init__(self) -> None:
            self.registered: list[tuple[int, int]] = []
            self.revoked: list[int] = []
            self.initialized = 0
            self.uninitialized = 0

        def OleInitialize(self, _reserved) -> int:
            self.initialized += 1
            return win_com_drop.S_OK

        def OleUninitialize(self) -> None:
            self.uninitialized += 1

        def RegisterDragDrop(self, hwnd, pointer) -> int:
            self.registered.append((int(hwnd.value), pointer))
            return win_com_drop.S_OK

        def RevokeDragDrop(self, hwnd) -> int:
            self.revoked.append(int(hwnd.value))
            return win_com_drop.S_OK

        def ReleaseStgMedium(self, _medium) -> None:
            pass

    ole = FakeOle()
    libraries = (ole, mock.Mock(), 100, 101)
    received: list[list[str]] = []
    with (
        mock.patch.object(win_com_drop, "_libraries", return_value=libraries),
        mock.patch.object(win_com_drop, "_ole_initialized", False),
    ):
        drop = win_com_drop.ComFileDrop(received.append)
        assert drop.install(321) is True
        assert ole.initialized == 1
        assert len(ole.registered) == 1
        assert ole.registered[0][0] == 321
        assert ole.registered[0][1].value == drop.pointer
        with mock.patch.object(
            win_com_drop, "_enum_data_formats", return_value={100}
        ):
            assert (
                drop._effect_for(object()) == win_com_drop.DROPEFFECT_COPY
            )
        drop.uninstall()
        assert ole.revoked == [321]
        assert ole.uninitialized == 0
        assert drop.installed is False
        assert 321 not in win_com_drop._targets


def test_com_drop_enum_s_false() -> None:
    """Проводник возвращает S_FALSE с форматами: их нельзя терять."""
    import ctypes

    from eves_text import win_com_drop

    enum = ctypes.c_void_p(1)

    def fake_call(_pointer, index, _restype, _argtypes, *args):
        if index == 8:  # IDataObject::EnumFormatEtc
            ctypes.cast(args[1], ctypes.POINTER(ctypes.c_void_p))[0] = enum
            return win_com_drop.S_OK
        if index == 3:  # IEnumFORMATETC::Next
            entries, fetched = args[1], args[2]
            entries[0].cfFormat = win_com_drop.CF_HDROP
            entries[1].cfFormat = 100
            ctypes.cast(fetched, ctypes.POINTER(win_com_drop.ULONG))[0] = 2
            return win_com_drop.S_FALSE
        return win_com_drop.S_OK

    with mock.patch.object(win_com_drop, "_com_call", side_effect=fake_call):
        assert win_com_drop._enum_data_formats(object()) == {
            win_com_drop.CF_HDROP,
            100,
        }

    # Наличие IDataObject уже достаточно, чтобы разрешить бросок: окончательные
    # форматы проверяются в Drop, а не в DragEnter.
    drop = win_com_drop.ComFileDrop(lambda _paths: None)
    assert drop._effect_for(object()) == win_com_drop.DROPEFFECT_COPY
    assert drop._effect_for(None) == win_com_drop.DROPEFFECT_NONE
    # DragEnter обязан выбрать эффект из разрешённых источником, иначе
    # Проводник показывает «запрет» и не вызывает Drop.
    assert (
        drop._effect_for(object(), win_com_drop.DROPEFFECT_MOVE)
        == win_com_drop.DROPEFFECT_MOVE
    )
    assert (
        drop._effect_for(object(), win_com_drop.DROPEFFECT_LINK)
        == win_com_drop.DROPEFFECT_LINK
    )
    assert drop._effect_for(object(), 0) == win_com_drop.DROPEFFECT_NONE
    allowed = ctypes.c_ulong(win_com_drop.DROPEFFECT_MOVE)
    assert drop._drag_enter(None, object(), 0, None, ctypes.pointer(allowed)) == win_com_drop.S_OK
    assert allowed.value == win_com_drop.DROPEFFECT_MOVE


def test_com_drop_opens_new_tab(app: Application) -> None:
    """COM-бросок открывает файл рядом, не меняя текст текущей вкладки."""
    from eves_text import settings as user_settings

    original_load = user_settings.load
    original_save = user_settings.save
    user_settings.load = lambda: {}
    user_settings.save = lambda data: None

    try:
        window = MainWindow(application=app)
        try:
            opened = window._active_tab
            assert opened is not None
            window.set_text("Текущий текст документа", None)
            original = window.get_text()

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "из проводника.txt"
                path.write_text("Файл из Проводника", encoding="utf-8")
                assert window._open_com_dropped_paths([str(path)]) is False

            assert len(window._tabs) == 2
            assert window._active_tab is not opened
            assert window.get_text() == "Файл из Проводника"
            start, end = opened.buffer.get_bounds()
            assert opened.buffer.get_text(start, end, True) == original
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save


def test_native_drop_install_and_hdrop() -> None:
    """WM_DROPFILES устанавливается и читает пути, включая ошибку SetWindowLongPtr."""
    from eves_text import win_drop

    files = [r"C:\Users\Jean\первый файл.txt", r"C:\Users\Jean\документ.rtf"]
    received: list[list[str]] = []
    finished: list[int] = []
    accepted: list[tuple[int, bool]] = []

    class FakeShell:
        def DragQueryFileW(self, _hdrop, index, buffer, size):
            if index == 0xFFFFFFFF:
                return len(files)
            value = files[index]
            if buffer is None:
                return len(value)
            buffer.value = value
            return len(value)

        def DragFinish(self, hdrop) -> None:
            finished.append(hdrop)

        def DragAcceptFiles(self, hwnd, flag) -> None:
            accepted.append((int(hwnd), bool(flag)))

    class FakeUser32:
        def __init__(self, error: int = 0) -> None:
            self.error = error

        def GetWindowLongPtrW(self, _hwnd, _index) -> int:
            return 111

        def SetWindowLongPtrW(self, _hwnd, _index, proc) -> int:
            assert proc
            return 0 if self.error else 222

        def GetLastError(self) -> int:
            return self.error

        def SetLastError(self, value: int) -> None:
            if not self.error:
                self.error = value

        def CallWindowProcW(self, *_args) -> int:
            return 0

        def DefWindowProcW(self, *_args) -> int:
            return 0

    shell = FakeShell()
    user32 = FakeUser32()
    win_drop._windows.clear()
    with (
        mock.patch.object(win_drop, "_libraries", return_value=(user32, shell)),
        mock.patch.object(win_drop, "_get_window_long_ptr", user32.GetWindowLongPtrW, create=True),
        mock.patch.object(win_drop, "_set_window_long_ptr", user32.SetWindowLongPtrW, create=True),
        mock.patch.object(win_drop, "_get_last_error", user32.GetLastError, create=True),
        mock.patch.object(win_drop, "_set_last_error", user32.SetLastError, create=True),
        mock.patch.object(win_drop, "_shell32", shell, create=True),
        mock.patch.object(win_drop, "_subclass_proc", None, create=True),
    ):
        drop = win_drop.NativeFileDrop(received.append)
        assert drop.install(321) is True
        assert accepted == [(321, True)]
        assert win_drop._window_proc(321, win_drop._WM_DROPFILES, 77, 0) == 0
        assert received == [files]
        assert finished == [77]
        drop.uninstall()
        assert accepted[-1] == (321, False)
        assert drop.installed is False

    bad_user32 = FakeUser32(error=5)
    with (
        mock.patch.object(win_drop, "_libraries", return_value=(bad_user32, shell)),
        mock.patch.object(win_drop, "_get_window_long_ptr", bad_user32.GetWindowLongPtrW, create=True),
        mock.patch.object(win_drop, "_set_window_long_ptr", bad_user32.SetWindowLongPtrW, create=True),
        mock.patch.object(win_drop, "_get_last_error", bad_user32.GetLastError, create=True),
        mock.patch.object(win_drop, "_set_last_error", bad_user32.SetLastError, create=True),
        mock.patch.object(win_drop, "_shell32", shell, create=True),
        mock.patch.object(win_drop, "_subclass_proc", None, create=True),
    ):
        try:
            win_drop.NativeFileDrop(received.append).install(322)
        except win_drop.NativeDropError:
            pass
        else:
            raise AssertionError("ошибка SetWindowLongPtrW не распознана")


def test_native_drop_libraries_use_kernel32() -> None:
    """GetLastError/SetLastError берутся из kernel32, а не из user32."""
    from eves_text import win_drop

    class FakeFunc:
        def __init__(self) -> None:
            self.argtypes = None
            self.restype = None

        def __call__(self, *_args) -> int:
            return 0

    user32 = SimpleNamespace(
        GetWindowLongPtrW=FakeFunc(),
        SetWindowLongPtrW=FakeFunc(),
        CallWindowProcW=FakeFunc(),
        DefWindowProcW=FakeFunc(),
    )
    shell32 = SimpleNamespace(
        DragAcceptFiles=FakeFunc(),
        DragQueryFileW=FakeFunc(),
        DragFinish=FakeFunc(),
    )
    kernel32 = SimpleNamespace(GetLastError=FakeFunc(), SetLastError=FakeFunc())
    windll = SimpleNamespace(user32=user32, shell32=shell32, kernel32=kernel32)

    names = (
        "_user32",
        "_shell32",
        "_kernel32",
        "_get_window_long_ptr",
        "_set_window_long_ptr",
        "_get_last_error",
        "_set_last_error",
    )
    saved = {name: getattr(win_drop, name) for name in names}
    try:
        with (
            mock.patch.object(win_drop.os, "name", "nt"),
            mock.patch.object(win_drop.ctypes, "windll", windll, create=True),
        ):
            for name in names:
                setattr(win_drop, name, None)
            win_drop._libraries()
            assert win_drop._kernel32 is kernel32
            assert win_drop._get_last_error is kernel32.GetLastError
            assert win_drop._set_last_error is kernel32.SetLastError
            assert not hasattr(user32, "GetLastError")
            assert not hasattr(user32, "SetLastError")
    finally:
        for name, value in saved.items():
            setattr(win_drop, name, value)


def test_native_drop_opens_new_tab(app: Application) -> None:
    """Нативный бросок открывает файл рядом и не дублирует повторный вызов."""
    from eves_text import settings as user_settings

    original_load = user_settings.load
    original_save = user_settings.save
    user_settings.load = lambda: {}
    user_settings.save = lambda data: None

    try:
        window = MainWindow(application=app)
        try:
            opened = window._active_tab
            assert opened is not None
            window.set_text("Текущий текст документа", None)
            original = window.get_text()

            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "из проводника.txt"
                path.write_text("Файл из Проводника", encoding="utf-8")
                assert window._open_native_dropped_paths([str(path)]) is False
                # Дубликат от второго приёмника не должен открыть вкладку ещё раз.
                assert window._open_native_dropped_paths([str(path)]) is False

            assert len(window._tabs) == 2
            assert window._active_tab is not opened
            assert window.get_text() == "Файл из Проводника"
            start, end = opened.buffer.get_bounds()
            assert opened.buffer.get_text(start, end, True) == original
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save


def test_win_dnd_mode() -> None:
    """В Windows OLE поднимается до GDK, GDK OLE2 выключен."""
    from eves_text import win_com_drop, win_dnd

    with (
        mock.patch.object(os, "name", "nt"),
        mock.patch.dict(os.environ, {win_dnd.OLE2_ENV: "1"}, clear=False),
        mock.patch.object(win_com_drop, "initialize_ole", return_value=True) as initialize,
    ):
        win_dnd.configure_dnd()
        assert initialize.call_count == 1
        assert win_dnd.use_com_file_drop() is True
        # GDK смотрит на наличие переменной: "0" её не отключил бы.
        assert win_dnd.OLE2_ENV not in os.environ


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


def test_window_geometry(app: Application) -> None:
    """Размер и положение окна сохраняются и восстанавливаются."""
    from eves_text import settings as user_settings

    original_load = user_settings.load
    original_save = user_settings.save
    saved: list[dict] = []
    user_settings.load = lambda: {
        "font_size": 16,
        "window": {"x": 120, "y": 90, "width": 900, "height": 640},
    }
    user_settings.save = lambda data: saved.append(dict(data))

    try:
        window = MainWindow(application=app)
        try:
            assert window._window_geometry == (120, 90, 900, 640)
            event = SimpleNamespace(width=1120, height=780)
            with mock.patch.object(window, "get_position", return_value=(30, 40)):
                assert window._on_window_configure(window, event) is False
            assert window._window_geometry == (30, 40, 1120, 780)
            clamped = MainWindow._validated_window_geometry(
                {"x": -50, "y": 99999, "width": 100, "height": 10000}
            )
            assert clamped is None  # y вне разумного диапазона
            offscreen_left = MainWindow._validated_window_geometry(
                {"x": -800, "y": 10, "width": 900, "height": 640}
            )
            assert offscreen_left is not None
            assert offscreen_left[0] == -800 and offscreen_left[2] >= 400
        finally:
            window.destroy()
    finally:
        user_settings.load = original_load
        user_settings.save = original_save

    assert saved
    assert saved[-1]["window"] == {"x": 30, "y": 40, "width": 1120, "height": 780}
    assert saved[-1]["font_size"] == 16


def test_find_text(app: Application) -> None:
    """Ctrl+F открывает поиск, Enter листает совпадения, Esc закрывает."""
    window = MainWindow(application=app)
    try:
        assert window.lookup_action("find") is not None
        assert "<Primary>f" not in app.get_accels_for_action("win.find")
        window.set_text("Первый файл\nВторой файл и снова файл", None)

        window.action_find()
        assert window.search_bar.get_visible() is True
        window.search_entry.set_text("файл")
        bounds = window.buffer.get_selection_bounds()
        assert bounds is not None
        assert window.buffer.get_text(bounds[0], bounds[1], True) == "файл"
        first = window.buffer.get_iter_at_mark(window.buffer.get_insert()).get_offset()

        window._find_next()
        second = window.buffer.get_iter_at_mark(window.buffer.get_insert()).get_offset()
        assert second > first

        window._find_previous()
        previous = window.buffer.get_iter_at_mark(window.buffer.get_insert()).get_offset()
        assert previous == first

        window.search_entry.set_text("нет такого слова")
        assert window.search_count.get_text() == "Нет совпадений"

        window._close_search_bar()
        assert window.search_bar.get_visible() is False
    finally:
        window.destroy()


def test_search_bar_only_on_shortcut(app: Application) -> None:
    """Панель поиска скрыта при запуске и не открывается из меню."""
    css = (ROOT / "eves_text" / "resources" / "style.css").read_text(encoding="utf-8")
    search_block = css.split(".search-bar {", 1)[1].split("}", 1)[0]
    dark_block = css.split(".dark-theme .search-bar {", 1)[1].split("}", 1)[0]
    assert "background-color: #ffffff" in search_block
    assert "background-color: #2b2b2b" in dark_block

    window = MainWindow(application=app)
    try:
        labels = []
        for item in window.menubar.get_children():
            menu = item.get_submenu()
            if menu is None:
                continue
            for child in menu.get_children():
                label = child.get_label()
                if label:
                    labels.append(label.replace("_", ""))
        assert not any("Найти" in label for label in labels)
        assert window.search_entry.get_width_chars() <= 24
        assert window.search_bar.get_visible() is False

        # application.py показывает окно через show_all(): панель не должна
        # появляться вместе с ним.
        window.show_all()
        assert window.search_bar.get_visible() is False

        # gtk_accel_group_activate() — тот же путь, что и реальное нажатие
        # Ctrl+F: accel group окна ловит клавишу до фокусного виджета.
        plain_name = Gtk.accelerator_name(Gdk.KEY_f, 0)
        plain_quark = GLib.quark_from_string(plain_name)
        assert not window._find_accel_group.activate(
            plain_quark, window, Gdk.KEY_f, 0
        )
        assert window.search_bar.get_visible() is False

        name = Gtk.accelerator_name(Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK)
        quark = GLib.quark_from_string(name)
        assert window._find_accel_group.activate(
            quark, window, Gdk.KEY_f, Gdk.ModifierType.CONTROL_MASK
        )
        assert window.search_bar.get_visible() is True

        window._close_search_bar()
        assert window.search_bar.get_visible() is False
        window.action_find()
        assert window.search_bar.get_visible() is True
    finally:
        window.destroy()


if __name__ == "__main__":
    test_document()
    test_document_rtf()
    test_updates()
    test_win_theme()
    test_caret_blinker()
    test_app_logo()
    test_dark_scheme()
    test_button_hover()
    test_check_icon_parser()
    test_windows_installer()
    test_linux_installer()
    test_linux_restart()
    test_native_file_dialog_filters()
    test_app = test_window()
    test_formatting(test_app)
    test_drag_and_drop(test_app)
    test_com_drop_parsing()
    test_com_drop_medium_bytes_uses_kernel32()
    test_com_drop_dropped_paths_reads_medium()
    test_com_drop_registration()
    test_com_drop_enum_s_false()
    test_com_drop_opens_new_tab(test_app)
    test_native_drop_install_and_hdrop()
    test_native_drop_libraries_use_kernel32()
    test_native_drop_opens_new_tab(test_app)
    test_win_dnd_mode()
    test_window_geometry(test_app)
    test_find_text(test_app)
    test_search_bar_only_on_shortcut(test_app)
    test_settings(test_app)
    print("OK: smoke test пройден")
