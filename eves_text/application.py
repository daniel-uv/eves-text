"""Точка входа приложения."""

from __future__ import annotations

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
        Gtk.Window.set_default_icon_name(app_icon_name())
        self._load_css()

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
