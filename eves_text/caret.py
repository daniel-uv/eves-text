"""Мигающая каретка без остаточного пикселя.

GTK3 рисует каретку по округлённым координатам, а при мигании стирает её
по усечённым координатам из ``gtk_text_layout_get_cursor_locations()``.
Из-за этого нижняя строка пикселей каретки не попадает в область
перерисовки и остаётся на экране (заметно на Win32-бэкенде).

Здесь каретка рисуется самостоятельно по тем же целым координатам, по
которым потом стирается: при мигании область перерисовки совпадает с
нарисованным прямоугольником, поэтому лишних пикселей не остаётся.
"""

from __future__ import annotations

import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk  # noqa: E402


class CaretBlinker:
    """Своя мигающая каретка для текстового вида.

    Встраивается только на Windows: на Linux штатная каретка GTK
    работает без этого дефекта.
    """

    def __init__(self, view: Gtk.TextView) -> None:
        self.view = view
        self._visible = True
        self._rect: tuple[int, int, int, int] | None = None
        self._timeout = 0
        self._last_activity = time.monotonic()
        self._stopped = False

        view.connect_after("draw", self._on_draw)
        view.connect("focus-in-event", self._on_focus_in)
        view.connect("focus-out-event", self._on_focus_out)
        view.connect("destroy", self._on_destroy)

        buffer = view.get_buffer()
        buffer.connect("notify::cursor-position", self._on_activity)
        buffer.connect("changed", self._on_activity)

        # Штатная каретка больше не нужна: её мигание и оставляло пиксель.
        view.set_cursor_visible(False)
        self._schedule()

    # ------------------------------------------------------------- рисование

    def _on_draw(self, _view: Gtk.TextView, cr) -> bool:
        if self._stopped or not self._visible:
            return False
        if not self.view.has_focus() or not self.view.get_editable():
            return False

        rect = self._caret_rect()
        if rect is None:
            return False
        self._rect = rect
        x, y, width, height = rect

        cr.save()
        Gdk.cairo_set_source_rgba(cr, self._cursor_color())
        cr.rectangle(x, y, width, height)
        cr.fill()
        cr.restore()
        return False

    def _cursor_color(self) -> Gdk.RGBA:
        context = self.view.get_style_context()
        state = context.get_state()
        try:
            color = context.get_property("caret-color", state)
        except (AttributeError, TypeError):
            color = None
        return color or context.get_color(state)

    def _caret_rect(self) -> tuple[int, int, int, int] | None:
        buffer = self.view.get_buffer()
        if buffer is None:
            return None
        iterator = buffer.get_iter_at_mark(buffer.get_insert())
        strong, _weak = self.view.get_cursor_locations(iterator)
        if strong.height <= 0:
            return None
        x, y = self.view.buffer_to_window_coords(
            Gtk.TextWindowType.WIDGET, strong.x, strong.y
        )
        width = int(strong.height * self._aspect_ratio()) + 1
        return (x - width // 2, y, width, strong.height)

    def _aspect_ratio(self) -> float:
        settings = self.view.get_settings()
        if settings is not None:
            value = settings.get_property("gtk-cursor-aspect-ratio")
            if value:
                return max(0.0, float(value))
        return 0.04

    # ---------------------------------------------------------------- мигание

    def _on_focus_in(self, *_args) -> bool:
        self._on_activity()
        return False

    def _on_focus_out(self, *_args) -> bool:
        self._cancel()
        self._set_visible(False)
        return False

    def _on_activity(self, *_args) -> None:
        self._last_activity = time.monotonic()
        # Стираем каретку на прежнем месте: при скрытой штатной каретке
        # GTK не всегда перерисовывает старую строку при перемещении курсора.
        self._invalidate()
        self._set_visible(True)
        self._schedule()

    def _on_destroy(self, *_args) -> None:
        self.stop()

    def stop(self) -> None:
        self._stopped = True
        self._cancel()
        self._rect = None

    def _blink_enabled(self) -> bool:
        settings = self.view.get_settings()
        return settings is None or bool(settings.get_property("gtk-cursor-blink"))

    def _cycle_ms(self) -> int:
        settings = self.view.get_settings()
        value = 1200
        if settings is not None:
            try:
                value = int(settings.get_property("gtk-cursor-blink-time") or 1200)
            except (TypeError, ValueError):
                pass
        return max(200, value)

    def _timed_out(self) -> bool:
        settings = self.view.get_settings()
        timeout = 10
        if settings is not None:
            try:
                timeout = int(settings.get_property("gtk-cursor-blink-timeout"))
            except (TypeError, ValueError):
                pass
        return timeout > 0 and time.monotonic() - self._last_activity >= timeout

    def _schedule(self) -> None:
        self._cancel()
        if self._stopped or not self._blink_enabled():
            return
        if not self.view.has_focus():
            return
        if self._timed_out():
            self._set_visible(True)
            return
        delay = self._cycle_ms() * (2 if self._visible else 1) // 3
        self._timeout = GLib.timeout_add(delay, self._blink)

    def _blink(self) -> bool:
        self._timeout = 0
        if self._stopped:
            return False
        if not self._blink_enabled() or not self.view.has_focus():
            self._set_visible(True)
            return False
        self._set_visible(not self._visible)
        self._schedule()
        return False

    def _cancel(self) -> None:
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0

    # ------------------------------------------------------------ перерисовка

    def _set_visible(self, visible: bool) -> None:
        if visible == self._visible:
            return
        self._visible = visible
        self._invalidate()

    def _invalidate(self) -> None:
        current = self._caret_rect()
        previous = self._rect
        self._rect = current
        areas: list[tuple[int, int, int, int]] = []
        for area in (previous, current):
            if area is not None and area not in areas:
                areas.append(area)
        for area in areas:
            self.view.queue_draw_area(*area)
