"""Eve's Text — простой текстовый редактор на GTK."""

import os

from .win_dnd import configure_dnd

# Режим DnD нужно выбрать до Gtk.init(), который инициализирует GDK DnD.
configure_dnd()

__version__ = "0.6.56"
APP_ID = "org.eves.Text"
