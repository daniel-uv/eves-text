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
    test_window()
    print("OK: smoke test пройден")
