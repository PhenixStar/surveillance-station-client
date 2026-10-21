# Copyright (c) 2026, Renaud Allard <renaud@allard.it>
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
#
# 1. Redistributions of source code must retain the above copyright notice,
#    this list of conditions and the following disclaimer.
#
# 2. Redistributions in binary form must reproduce the above copyright notice,
#    this list of conditions and the following disclaimer in the documentation
#    and/or other materials provided with the distribution.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
# ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
# LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
# CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
# SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
# INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
# CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
# ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
# POSSIBILITY OF SUCH DAMAGE.

"""Fisheye lens calibration, opened from Camera Settings: the camera's
current picture with the lens circle drawn over it, adjusted until the
circle sits on the edge of the image."""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("GdkPixbuf", "2.0")

from gi.repository import Gdk, GdkPixbuf, Gtk  # type: ignore[import-untyped]

from surveillance.api.models import Camera
from surveillance.services.snapshot import fetch_camera_snapshot
from surveillance.ui.fisheye import LENS_FOV_MAX, LENS_FOV_MIN, LENS_RADIUS_MAX, Lens
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.app import SurveillanceApp

log = logging.getLogger(__name__)

# Lens field, label, lower and upper bound, step, digits.
_FIELDS = (
    ("center_x", "Centre, across", 0.0, 1.0, 0.002, 3),
    ("center_y", "Centre, down", 0.0, 1.0, 0.002, 3),
    ("radius", "Radius", 0.01, LENS_RADIUS_MAX, 0.002, 3),
    ("fov", "Field of view (degrees)", LENS_FOV_MIN, LENS_FOV_MAX, 1.0, 0),
)


def fitted_rect(
    width: float, height: float, image_width: int, image_height: int
) -> tuple[float, float, float, float]:
    """Where Gtk.ContentFit.CONTAIN draws an image of that size in a
    *width* x *height* widget: (x, y, width, height), centred."""
    scale = min(width / image_width, height / image_height)
    w, h = image_width * scale, image_height * scale
    return (width - w) / 2, (height - h) / 2, w, h


def lens_circle(rect: tuple[float, float, float, float], lens: Lens) -> tuple[float, float, float]:
    """Centre and radius, in widget pixels, of *lens*'s circle on the
    picture drawn in *rect*: the centre in fractions of its width and
    height, the radius in fractions of its height."""
    x, y, w, h = rect
    return x + lens.center_x * w, y + lens.center_y * h, lens.radius * h


class LensCalibrationDialog(Gtk.Window):
    """Fit a fisheye camera's lens circle; *on_done* gets the result."""

    def __init__(
        self,
        parent: Gtk.Window,
        app: SurveillanceApp,
        cam: Camera,
        lens: Lens,
        on_done: Callable[[Lens], None],
    ) -> None:
        super().__init__(transient_for=parent, modal=True)
        self._on_done = on_done
        self._image_size = (0, 0)
        self.set_title(f"Fisheye Lens — {cam.name}")
        self.set_default_size(640, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.set_margin_top(16)
        box.set_margin_bottom(16)
        box.set_margin_start(16)
        box.set_margin_end(16)
        self.set_child(box)

        hint = Gtk.Label(
            label="Set the centre and radius until the circle lies on the edge of "
            "the picture, and the field of view to the lens's own, from its "
            "specifications."
        )
        hint.set_wrap(True)
        hint.set_xalign(0)
        box.append(hint)

        overlay = Gtk.Overlay()
        overlay.set_size_request(480, 360)
        overlay.set_vexpand(True)
        self._picture = Gtk.Picture()
        self._picture.set_content_fit(Gtk.ContentFit.CONTAIN)
        self._picture.set_can_shrink(True)
        overlay.set_child(self._picture)
        self._circle = Gtk.DrawingArea()
        self._circle.set_draw_func(self._draw)
        overlay.add_overlay(self._circle)
        box.append(overlay)

        self._status = Gtk.Label(label="Loading the camera's picture…")
        self._status.set_xalign(0)
        self._status.add_css_class("dim-label")
        box.append(self._status)

        grid = Gtk.Grid(row_spacing=6, column_spacing=12)
        self._spins: dict[str, Gtk.SpinButton] = {}
        for row, (key, text, lower, upper, step, digits) in enumerate(_FIELDS):
            label = Gtk.Label(label=text)
            label.set_xalign(0)
            label.set_hexpand(True)
            grid.attach(label, 0, row, 1, 1)
            spin = Gtk.SpinButton.new_with_range(lower, upper, step)
            spin.set_digits(digits)
            spin.connect("value-changed", lambda _spin: self._circle.queue_draw())
            grid.attach(spin, 1, row, 1, 1)
            self._spins[key] = spin
        box.append(grid)
        self._show(lens)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        reset_btn = Gtk.Button(label="Reset")
        reset_btn.set_tooltip_text("A centred full circle of 180°")
        reset_btn.connect("clicked", lambda _btn: self._show(Lens()))
        btn_box.append(reset_btn)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_box.append(spacer)
        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda _btn: self.close())
        btn_box.append(cancel_btn)
        use_btn = Gtk.Button(label="Use")
        use_btn.add_css_class("suggested-action")
        use_btn.connect("clicked", self._on_use)
        btn_box.append(use_btn)
        box.append(btn_box)

        if app.api is None:
            self._status.set_label("Not connected, so there is no picture to fit.")
        else:
            run_async(
                fetch_camera_snapshot(app.api, cam.id),
                callback=self._on_snapshot,
                error_callback=self._on_snapshot_failed,
            )

    @property
    def lens(self) -> Lens:
        return Lens(**{key: spin.get_value() for key, spin in self._spins.items()})

    def _show(self, lens: Lens) -> None:
        for key, spin in self._spins.items():
            spin.set_value(getattr(lens, key))

    def _on_snapshot(self, data: bytes) -> None:
        try:
            loader = GdkPixbuf.PixbufLoader()
            loader.write(data)
            loader.close()
            texture = Gdk.Texture.new_for_pixbuf(loader.get_pixbuf())
        except Exception as exc:
            self._on_snapshot_failed(exc)
            return
        self._image_size = (texture.get_width(), texture.get_height())
        self._picture.set_paintable(texture)
        self._status.set_visible(False)
        self._circle.queue_draw()

    def _on_snapshot_failed(self, exc: BaseException) -> None:
        log.warning("Lens calibration picture failed: %s", exc)
        self._status.set_label(f"Could not load the camera's picture: {exc}")

    def _draw(self, _area: Gtk.DrawingArea, cr: Any, width: int, height: int) -> None:
        image_width, image_height = self._image_size
        if not image_width or not image_height:
            return
        rect = fitted_rect(width, height, image_width, image_height)
        x, y, radius = lens_circle(rect, self.lens)
        cr.set_source_rgb(1.0, 0.8, 0.0)
        cr.set_line_width(2.0)
        cr.arc(x, y, radius, 0.0, 2.0 * math.pi)
        cr.stroke()
        cr.move_to(x - 10.0, y)
        cr.line_to(x + 10.0, y)
        cr.move_to(x, y - 10.0)
        cr.line_to(x, y + 10.0)
        cr.stroke()

    def _on_use(self, _btn: Gtk.Button) -> None:
        self._on_done(self.lens)
        self.close()
