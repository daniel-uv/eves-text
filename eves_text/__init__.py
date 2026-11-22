"""Eve's Text — простой текстовый редактор на GTK."""

import os

from .win_dnd import enable_ole2_dnd

# Переменную нужно выставить до Gtk.init(), который инициализирует GDK DnD.
enable_ole2_dnd()

__version__ = "0.6.55"
APP_ID = "org.eves.Text"
