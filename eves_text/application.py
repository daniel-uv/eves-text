"""Точка входа приложения."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("GtkSource", "4")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import APP_ID, __version__  # noqa: E402
from .window import MainWindow, app_icon_name  # noqa: E402

APP_NAME = "Eve's Text"


class Application(Gtk.Application):
    def __init__(self) -> None:
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.HANDLES_OPEN,
        )
        GLib.set_application_name(APP_NAME)
        GLib.set_prgname("eves-text")
        self.add_main_option(
            "version",
            0,
            GLib.OptionFlags.NONE,
            GLib.OptionArg.NONE,
            "Показать версию и выйти",
            None,
        )

    def do_handle_local_options(self, options: GLib.VariantDict) -> int:
        if options.contains("version"):
            print(f"{APP_NAME} {__version__}")
            return 0
        return -1

    def do_startup(self) -> None:
        Gtk.Application.do_startup(self)
        self._setup_icons()
        self._apply_system_theme()
        self._load_css()

    @staticmethod
    def _setup_icons() -> None:
        """Иконка из темы или из файла рядом с пакетом (Windows-бандл)."""
        data_dir = os.environ.get("EVES_TEXT_DATA_DIR")
        theme = Gtk.IconTheme.get_default()
        if data_dir and theme is not None:
            icons = Path(data_dir) / "icons"
            if icons.is_dir():
                theme.append_search_path(str(icons))

        Gtk.Window.set_default_icon_name(app_icon_name())
        if theme is None or not theme.has_icon("eves-text"):
            # PNG первым: на Windows в бандле может не быть SVG-загрузчика.
            resources = Path(__file__).parent / "resources"
            for name in ("eves-text.png", "eves-text.svg"):
                icon_file = resources / name
                if not icon_file.exists():
                    continue
                try:
                    Gtk.Window.set_default_icon_from_file(str(icon_file))
                    break
                except GLib.Error:
                    continue

    @staticmethod
    def _apply_system_theme() -> None:
        """На Windows 11 берём светлую/тёмную тему из настроек системы."""
        if sys.platform != "win32":
            return
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
            ) as key:
                light, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        except OSError:
            return
        settings = Gtk.Settings.get_default()
        if settings is not None:
            settings.set_property("gtk-application-prefer-dark-theme", not bool(light))

    @staticmethod
    def _load_css() -> None:
        css = Path(__file__).parent / "resources" / "style.css"
        if not css.exists():
            return
        provider = Gtk.CssProvider()
        provider.load_from_path(str(css))
        screen = Gdk.Screen.get_default()
        if screen is not None:
            Gtk.StyleContext.add_provider_for_screen(
                screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def do_activate(self) -> None:
        window = self.props.active_window
        if window is None:
            window = MainWindow(application=self)
            window.show_all()
        window.present()

    def do_open(self, files, n_files: int, hint: str) -> None:
        self.activate()
        window = self.props.active_window
        if window is None or not files:
            return
        path = files[0].get_path()
        if path:
            window.open_path(path)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    return Application().run(argv)


if __name__ == "__main__":
    sys.exit(main())
