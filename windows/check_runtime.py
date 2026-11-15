#!/usr/bin/env python3
"""Проверка Windows-рантайма Eve's Text.

Запускается из бандла и проверяет, что GTK, GtkSourceView, gspell,
словарь hunspell и шрифты действительно работают на этой машине.

    bin\\python.exe app\\check_runtime.py          # проверка без окна
    bin\\python.exe app\\check_runtime.py --gui    # ещё и показать окно

Код возврата 0 — всё в порядке, 1 — есть проблемы.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import launcher  # noqa: E402

# В консоли Windows с кодовой страницей 1252 русский текст иначе падает.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

GOOD = "OK"
BAD = "ОШИБКА"


def check(name: str, func) -> bool:
    try:
        detail = func()
    except Exception as exc:  # noqa: BLE001 - показываем любую проблему
        print(f"[{BAD}] {name}: {type(exc).__name__}: {exc}")
        return False
    print(f"[{GOOD}] {name}: {detail}" if detail else f"[{GOOD}] {name}")
    return True


def main() -> int:
    base = launcher.base_dir()
    print(f"Бандл: {base}")
    print(f"Python: {sys.version.split()[0]} ({sys.platform})")
    launcher.configure(base)

    from gi.repository import GLib  # noqa: E402

    print(f"GLib: {GLib.MAJOR_VERSION}.{GLib.MINOR_VERSION}.{GLib.MICRO_VERSION}")

    results: list[bool] = []

    def gtk_init():
        import gi

        gi.require_version("Gtk", "3.0")
        from gi.repository import Gtk

        if not Gtk.init_check([])[0]:
            raise RuntimeError("Gtk.init_check вернул False (нет дисплея?)")
        return f"{Gtk.MAJOR_VERSION}.{Gtk.MINOR_VERSION}.{Gtk.MICRO_VERSION}"

    results.append(check("GTK", gtk_init))

    def gtk_source():
        import gi

        gi.require_version("GtkSource", "4")
        from gi.repository import GtkSource

        manager = GtkSource.LanguageManager.get_default()
        if manager is None:
            raise RuntimeError("менеджер языков не создан")
        languages = manager.get_language_ids()
        if len(languages) < 10:
            raise RuntimeError(
                "мало языков подсветки — не найден share/gtksourceview-4"
            )
        return f"языков подсветки: {len(languages)}"

    results.append(check("GtkSourceView", gtk_source))

    def gspell():
        import gi

        gi.require_version("Gspell", "1")
        from gi.repository import Gspell

        language = None
        for code in ("ru_RU", "ru", "en_US", "en"):
            language = Gspell.Language.lookup(code)
            if language is not None:
                print(f"       словарь: {code} ({language.get_name()})")
                break
        if language is None:
            available = ", ".join(
                item.get_code() for item in Gspell.Language.get_available()
            )
            raise RuntimeError(
                f"ни один словарь не найден (DICPATH?); доступны: {available or 'нет'}"
            )
        return None

    results.append(check("gspell", gspell))

    def spell_words():
        import gi

        gi.require_version("Gspell", "1")
        from gi.repository import Gspell

        for code in ("ru_RU", "ru"):
            language = Gspell.Language.lookup(code)
            if language is not None:
                break
        else:
            available = ", ".join(
                item.get_code() for item in Gspell.Language.get_available()
            )
            raise RuntimeError(
                f"нет русского словаря; доступны: {available or 'нет'}"
            )
        checker = Gspell.Checker.new(language)
        if checker is None:
            raise RuntimeError("Gspell.Checker.new вернул None")

        def known(word: str) -> bool:
            return checker.check_word(word, len(word.encode("utf-8")))

        if not known("собака"):
            raise RuntimeError("правильное слово «собака» не распознано")
        if known("сабака"):
            raise RuntimeError("опечатка «сабака» признана правильной")
        return "«собака» верно, «сабака» — ошибка"

    results.append(check("орфография", spell_words))

    def font():
        from gi.repository import Pango

        from eves_text.window import default_font_family

        family = default_font_family()
        desc = Pango.FontDescription.from_string(f"{family} 12")
        return f"{desc.to_string()}"

    results.append(check("шрифт", font))

    def icon():
        import gi

        gi.require_version("Gtk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf, Gtk

        theme = Gtk.IconTheme.get_default()
        if theme is None:
            raise RuntimeError("тема иконок не создана")
        data_dir = Path(os.environ["EVES_TEXT_DATA_DIR"])
        icons = data_dir / "icons"
        if icons.is_dir():
            theme.append_search_path(str(icons))

        settings = Gtk.Settings.get_default()
        theme_name = (
            settings.get_property("gtk-icon-theme-name") if settings else "?"
        )
        formats = ", ".join(
            sorted({item.get_name() for item in GdkPixbuf.Pixbuf.get_formats()})
        )
        cache = os.environ.get("GDK_PIXBUF_MODULE_FILE", "не задан")
        if not theme.has_icon("eves-text"):
            icon_file = icons / "hicolor" / "scalable" / "apps" / "eves-text.svg"
            info = GdkPixbuf.Pixbuf.get_file_info(str(icon_file))
            raise RuntimeError(
                f"иконка eves-text не найдена (тема {theme_name}, "
                f"пути: {theme.get_search_path()}, "
                f"файл: {icon_file} -> {info}, "
                f"форматы: {formats}, кэш загрузчиков: {cache})"
            )
        info = theme.lookup_icon("eves-text", 64, 0)
        return info.get_filename() if info else "из темы"

    results.append(check("иконка", icon))

    if "--gui" in sys.argv:
        def show_window():
            import gi

            gi.require_version("Gtk", "3.0")
            gi.require_version("GtkSource", "4")
            from gi.repository import Gtk, GtkSource

            win = Gtk.Window(title="Eve's Text — проверка")
            view = GtkSource.View()
            view.get_buffer().set_text("Проверка окна: собака, сабака.")
            win.add(view)
            win.set_default_size(480, 200)
            win.connect("destroy", Gtk.main_quit)
            win.show_all()
            GLib.timeout_add_seconds(3, Gtk.main_quit)
            Gtk.main()
            return "окно открылось"

        results.append(check("окно", show_window))

    failed = results.count(False)
    print()
    if failed:
        print(f"Проблем: {failed}. Смотрите сообщения выше.")
        return 1
    print("Рантайм в порядке.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
