#!/usr/bin/env python3
"""Дымовой тест: проверяет ввод-вывод файлов и сборку окна без показа на экране."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gi  # noqa: E402

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
from gi.repository import Gio, Gtk  # noqa: E402

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


def test_caret_blink() -> None:
    class FakeSettings:
        def __init__(self) -> None:
            self.props: dict[str, object] = {}

        def set_property(self, name: str, value: object) -> None:
            self.props[name] = value

    settings = FakeSettings()
    Application._configure_caret(settings, platform="linux")
    assert settings.props == {}

    Application._configure_caret(settings, platform="win32")
    assert settings.props == {"gtk-cursor-blink": False}, settings.props


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
            "/usr/bin/pkexec",
            "/usr/bin/dpkg",
            "-i",
            str(deb),
        ], command
        assert kwargs["timeout"] == 300

        # Без pkexec остаётся понятная подсказка про ручную установку.
        try:
            updates.subprocess = fake_subprocess
            updates.shutil.which = (
                lambda name: None if name == "pkexec" else f"/usr/bin/{name}"
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


def test_window() -> None:
    app = Application()
    app.register(None)

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

    window.destroy()


if __name__ == "__main__":
    test_document()
    test_updates()
    test_win_theme()
    test_caret_blink()
    test_windows_installer()
    test_linux_installer()
    test_linux_restart()
    test_window()
    print("OK: smoke test пройден")
