"""Non-focus-stealing live transcript overlay at monitor bottom-centre."""

from __future__ import annotations

import math
import time


def format_elapsed(seconds: float) -> str:
    """Format a short recording duration as ``M:SS``."""
    total = max(0, int(seconds))
    return f"{total // 60}:{total % 60:02d}"


def preview_text(text: str, limit: int = 48) -> str:
    """Keep the newest part of a transcript within a compact overlay."""
    clean = " ".join((text or "").split())
    if len(clean) <= limit:
        return clean
    return "…" + clean[-(limit - 1) :]


def overlay_position(
    size: tuple[int, int],
    workarea: tuple[int, int, int, int],
    bottom_margin: int = 72,
) -> tuple[int, int]:
    """Centre an overlay near the bottom of a monitor workarea."""
    width, height = size
    wx, wy, ww, wh = workarea
    x = wx + (ww - width) // 2
    y = wy + wh - height - max(0, bottom_margin)
    x = max(wx, min(x, wx + max(0, ww - width)))
    y = max(wy, min(y, wy + max(0, wh - height)))
    return x, y


class PreeditOverlay:
    """Small dark capsule showing waveform, live text, and elapsed time."""

    _TICK_MS = 100
    _POST_FINAL_MS = 900

    def __init__(self, Gtk, GLib, Gdk):
        self._Gtk = Gtk
        self._GLib = GLib
        self._Gdk = Gdk
        self._started_at = 0.0
        self._tick_id = 0
        self._hide_id = 0
        self._active = False

        window = Gtk.Window(type=Gtk.WindowType.POPUP)
        window.set_decorated(False)
        window.set_resizable(False)
        window.set_accept_focus(False)
        window.set_focus_on_map(False)
        window.set_skip_taskbar_hint(True)
        window.set_skip_pager_hint(True)
        window.set_keep_above(True)
        try:
            window.set_type_hint(Gdk.WindowTypeHint.TOOLTIP)
            visual = window.get_screen().get_rgba_visual()
            if visual is not None:
                window.set_visual(visual)
        except Exception:
            pass

        frame = Gtk.EventBox()
        frame.set_name("spitch-preedit")
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        row.set_border_width(10)
        frame.add(row)

        wave = Gtk.DrawingArea()
        wave.set_size_request(72, 20)
        wave.connect("draw", self._draw_wave)
        row.pack_start(wave, False, False, 0)

        label = Gtk.Label(label="听写中…")
        label.set_name("spitch-preedit-text")
        label.set_xalign(0.0)
        label.set_max_width_chars(48)
        label.set_width_chars(10)
        row.pack_start(label, True, True, 0)

        elapsed = Gtk.Label(label="0:00")
        elapsed.set_name("spitch-preedit-time")
        row.pack_start(elapsed, False, False, 0)
        window.add(frame)

        css = Gtk.CssProvider()
        css.load_from_data(b"""
            #spitch-preedit {
                background: rgba(24, 24, 29, 0.96);
                border: 1px solid rgba(255, 255, 255, 0.10);
                border-radius: 18px;
                box-shadow: 0 5px 16px rgba(0, 0, 0, 0.35);
            }
            #spitch-preedit-text { color: #f5f5f7; font-size: 14px; }
            #spitch-preedit-time { color: #f5f5f7; font-size: 14px; }
        """)
        Gtk.StyleContext.add_provider_for_screen(
            window.get_screen(), css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        self._window = window
        self._label = label
        self._elapsed = elapsed
        self._wave = wave

    def start(self) -> None:
        self._cancel_hide()
        self._started_at = time.monotonic()
        self._active = True
        self._label.set_text("听写中…")
        self._elapsed.set_text("0:00")
        self._window.show_all()
        self._position_bottom_center()
        self._GLib.idle_add(self._position_bottom_center)
        if not self._tick_id:
            self._tick_id = self._GLib.timeout_add(self._TICK_MS, self._tick)

    def update(self, text: str) -> None:
        if not self._active:
            return
        shown = preview_text(text)
        if shown:
            self._label.set_text(shown)
            # Text growth changes the capsule width; re-centre after GTK
            # processes the label's resize request.
            self._GLib.idle_add(self._position_bottom_center)

    def finalizing(self) -> None:
        if self._active and not self._label.get_text().strip():
            self._label.set_text("转写中…")

    def finish(self) -> None:
        if not self._active:
            return
        self._active = False
        self._cancel_hide()
        self._hide_id = self._GLib.timeout_add(
            self._POST_FINAL_MS, self._hide_now
        )

    def hide(self) -> None:
        self._active = False
        self._cancel_hide()
        self._hide_now()

    def _tick(self) -> bool:
        if not self._window.get_visible():
            self._tick_id = 0
            return False
        self._elapsed.set_text(format_elapsed(time.monotonic() - self._started_at))
        self._wave.queue_draw()
        return True

    def _draw_wave(self, _widget, cr) -> bool:
        elapsed = max(0.0, time.monotonic() - self._started_at)
        cr.set_source_rgb(0.72, 0.27, 0.98)
        for index in range(12):
            amplitude = 3.0 + 6.0 * abs(math.sin(elapsed * 5.0 + index * 0.8))
            x = 3.0 + index * 5.7
            cr.set_line_width(2.2)
            cr.move_to(x, 10.0 - amplitude / 2.0)
            cr.line_to(x, 10.0 + amplitude / 2.0)
            cr.stroke()
        return False

    def _pointer_position(self) -> tuple[int, int]:
        try:
            pointer = self._Gdk.Display.get_default().get_default_seat().get_pointer()
            _screen, x, y = pointer.get_position()
            return int(x), int(y)
        except Exception:
            return 0, 0

    def _position_bottom_center(self) -> bool:
        if not self._window.get_visible():
            return False
        self._window.realize()
        preferred = self._window.get_preferred_size()[1]
        width = max(220, preferred.width)
        height = max(40, preferred.height)
        try:
            display = self._Gdk.Display.get_default()
            px, py = self._pointer_position()
            monitor = display.get_monitor_at_point(px, py)
            area = monitor.get_workarea()
            workarea = (area.x, area.y, area.width, area.height)
        except Exception:
            screen = self._window.get_screen()
            workarea = (0, 0, screen.get_width(), screen.get_height())
        x, y = overlay_position((width, height), workarea)
        try:
            self._window.move(x, y)
        except Exception:
            pass
        return False

    def _cancel_hide(self) -> None:
        if self._hide_id:
            try:
                self._GLib.source_remove(self._hide_id)
            except Exception:
                pass
            self._hide_id = 0

    def _hide_now(self) -> bool:
        self._hide_id = 0
        self._window.hide()
        return False
