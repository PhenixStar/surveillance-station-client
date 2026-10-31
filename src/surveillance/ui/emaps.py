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

"""Maps page: Surveillance Station E-Maps with their cameras on them.

Read only. Each camera marker is a dot coloured by the camera's status in
the sidebar's camera list; hovering one names the camera, and clicking it
puts the camera in Live View the way a click in the sidebar does.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("GdkPixbuf", "2.0")
gi.require_version("Pango", "1.0")

import cairo
from gi.repository import Gdk, GdkPixbuf, GLib, Gtk, Pango  # type: ignore[import-untyped]

from surveillance.api.models import Camera, CameraStatus, EMap, EMapItem
from surveillance.services.emap import EMapService, emaps_unavailable, marker_to_pixels
from surveillance.util.async_bridge import run_async

if TYPE_CHECKING:
    from surveillance.ui.window import MainWindow

log = logging.getLogger(__name__)

_MARKER_RADIUS = 7.0
# How far from a marker's centre the pointer still counts as on it.
_HIT_RADIUS = 12.0

_COLOUR_ENABLED = (0.20, 0.70, 0.33)
_COLOUR_OFFLINE = (0.85, 0.27, 0.22)
# A camera the sidebar's list does not hold: deleted, or not visible to
# this account.
_COLOUR_UNKNOWN = (0.55, 0.55, 0.55)

# Side of the blank square drawn when a map has no image that decodes
# and the NAS gave no size for it either.
_PLACEHOLDER_SIZE = 1000


def fit_image(
    image_width: float, image_height: float, area_width: float, area_height: float
) -> tuple[float, float, float]:
    """Where an image is drawn to fit an area with its aspect kept, centred:
    (x offset, y offset, scale)."""
    if image_width <= 0 or image_height <= 0 or area_width <= 0 or area_height <= 0:
        return 0.0, 0.0, 0.0
    scale = min(area_width / image_width, area_height / image_height)
    return (
        (area_width - image_width * scale) / 2,
        (area_height - image_height * scale) / 2,
        scale,
    )


def marker_to_widget(
    lng: float,
    lat: float,
    image_width: float,
    image_height: float,
    area_width: float,
    area_height: float,
) -> tuple[float, float]:
    """A marker's position in an area its map image is fitted into."""
    x_off, y_off, scale = fit_image(image_width, image_height, area_width, area_height)
    x, y = marker_to_pixels(lng, lat, image_height)
    return x_off + x * scale, y_off + y * scale


def marker_at(points: list[tuple[float, float]], x: float, y: float, radius: float) -> int | None:
    """Index of the point nearest (x, y) within *radius*, None if none is."""
    best: int | None = None
    best_distance = radius
    for index, (px, py) in enumerate(points):
        distance = math.hypot(px - x, py - y)
        if distance <= best_distance:
            best, best_distance = index, distance
    return best


def marker_colour(camera: Camera | None) -> tuple[float, float, float]:
    """Enabled cameras green, disconnected or disabled ones red, cameras
    missing from the list grey."""
    if camera is None:
        return _COLOUR_UNKNOWN
    return _COLOUR_ENABLED if camera.status == CameraStatus.ENABLED else _COLOUR_OFFLINE


def _decode_image(content: bytes) -> GdkPixbuf.Pixbuf | None:
    """The map image, None when it does not decode. GdkPixbuf reads SVG as
    well as PNG and JPEG, where the SVG loader is installed."""
    loader = GdkPixbuf.PixbufLoader()
    try:
        loader.write(content)
        loader.close()
    except GLib.Error as e:
        log.warning("E-Map image could not be decoded: %s", e)
        return None
    pixbuf: GdkPixbuf.Pixbuf | None = loader.get_pixbuf()
    return pixbuf


class EMapsView(Gtk.Box):
    """List of E-Maps on the left, the selected one on the right."""

    def __init__(self, window: MainWindow) -> None:
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.window = window
        self.app = window.app
        self._service = EMapService(self.app.api) if self.app.api else None
        self._listed = False
        self._unavailable = False
        self._emap: EMap | None = None
        self._pixbuf: GdkPixbuf.Pixbuf | None = None
        # Bumped on every map pick, so a slow answer for a map picked
        # earlier cannot replace the one picked since.
        self._request = 0
        self._redraw_id = 0
        self._filling_list = False

        paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        paned.set_hexpand(True)
        paned.set_position(200)
        self.append(paned)

        # Left: the map list
        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.set_margin_top(8)
        header.set_margin_bottom(8)
        header.set_margin_start(8)
        header.set_margin_end(8)
        title = Gtk.Label(label="E-Maps")
        title.add_css_class("title-4")
        title.set_hexpand(True)
        title.set_xalign(0)
        header.append(title)
        self._refresh_btn = Gtk.Button()
        self._refresh_btn.set_icon_name("view-refresh-symbolic")
        self._refresh_btn.set_tooltip_text("Refresh")
        self._refresh_btn.connect("clicked", lambda _: self._refresh())
        header.append(self._refresh_btn)
        left.append(header)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)
        scroll.set_child(self.listbox)
        left.append(scroll)
        paned.set_start_child(left)
        paned.set_resize_start_child(False)
        paned.set_shrink_start_child(False)

        # Right: the map, or a message in its place
        self._content = Gtk.Stack()
        self._content.set_hexpand(True)
        self._content.set_vexpand(True)

        self._message = Gtk.Label(label="Loading E-Maps…")
        self._message.add_css_class("dim-label")
        self._message.set_wrap(True)
        self._message.set_justify(Gtk.Justification.CENTER)
        self._content.add_named(self._message, "message")

        map_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.area = Gtk.DrawingArea()
        self.area.set_hexpand(True)
        self.area.set_vexpand(True)
        self.area.set_draw_func(self._draw)
        self.area.set_has_tooltip(True)
        self.area.connect("query-tooltip", self._on_query_tooltip)
        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self._on_motion)
        self.area.add_controller(motion)
        click = Gtk.GestureClick(button=1)
        click.connect("released", self._on_click)
        self.area.add_controller(click)
        map_box.append(self.area)
        self._image_note = Gtk.Label(
            label="The map image could not be shown; its cameras are placed on a blank map."
        )
        self._image_note.add_css_class("dim-label")
        self._image_note.add_css_class("caption")
        self._image_note.set_margin_top(4)
        self._image_note.set_margin_bottom(4)
        self._image_note.set_visible(False)
        map_box.append(self._image_note)
        self._content.add_named(map_box, "map")

        paned.set_end_child(self._content)
        paned.set_shrink_end_child(False)

        # Loaded the first time the page is on screen, not at login: a NAS
        # without E-Maps then costs nothing to users who never open it.
        self.connect("map", self._on_mapped)
        self.connect("unmap", self._on_unmapped)

    # -- loading -------------------------------------------------------

    def _on_mapped(self, _widget: Gtk.Widget) -> None:
        if not self._listed:
            self._load_list()
        else:
            self.area.queue_draw()
        # Camera status changes arrive with the sidebar's poll; redraw on
        # the same beat while the page is shown.
        if not self._redraw_id:
            self._redraw_id = GLib.timeout_add_seconds(
                self.app.config.poll_interval_cameras, self._on_redraw_tick
            )

    def _on_unmapped(self, _widget: Gtk.Widget) -> None:
        if self._redraw_id:
            GLib.source_remove(self._redraw_id)
            self._redraw_id = 0

    def _on_redraw_tick(self) -> bool:
        self.area.queue_draw()
        return True

    def _refresh(self) -> None:
        if self._service is not None:
            self._service.clear()
        self._load_list()

    def _load_list(self) -> None:
        service = self._service
        if service is None or self._unavailable:
            return
        self._listed = True
        self._refresh_btn.set_sensitive(False)
        run_async(
            service.list_maps(),
            callback=lambda maps: self._on_list_loaded(service, maps),
            error_callback=self._on_load_error,
        )

    def _on_list_loaded(self, service: EMapService, maps: list[EMap]) -> None:
        if self.app.api is not service.api:
            return
        self._refresh_btn.set_sensitive(True)
        self._filling_list = True
        try:
            while (row := self.listbox.get_row_at_index(0)) is not None:
                self.listbox.remove(row)
            for emap in sorted(maps, key=lambda m: (m.name.casefold(), m.id)):
                self.listbox.append(self._create_row(emap))
        finally:
            self._filling_list = False

        if not maps:
            self._show_message("No E-Maps have been set up in Surveillance Station.")
            return
        rows = [self.listbox.get_row_at_index(i) for i in range(len(maps))]
        wanted = self.app.config.last_emap_id
        row = next((r for r in rows if r.emap.id == wanted), rows[0])  # type: ignore[union-attr]
        self.listbox.select_row(row)

    def _create_row(self, emap: EMap) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        label = Gtk.Label(label=emap.name or f"E-Map {emap.id}")
        label.set_xalign(0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        label.set_margin_start(8)
        label.set_margin_end(8)
        row.set_child(label)
        row.emap = emap  # type: ignore[attr-defined]
        return row

    def _on_row_selected(self, _listbox: Gtk.ListBox, row: Gtk.ListBoxRow | None) -> None:
        if row is None or self._filling_list:
            return
        emap: EMap = row.emap  # type: ignore[attr-defined]
        if self.app.config.last_emap_id != emap.id:
            self.app.config.last_emap_id = emap.id
            from surveillance.config import save_config

            save_config(self.app.config)
        self._load_map(emap.id)

    def _load_map(self, emap_id: int) -> None:
        service = self._service
        if service is None:
            return
        self._request += 1
        request = self._request
        self._show_message("Loading the map…")

        def _loaded(result: tuple[EMap | None, bytes | None]) -> None:
            if request != self._request or self.app.api is not service.api:
                return
            emap, content = result
            if emap is None:
                self._show_message("This E-Map no longer exists. Refresh the list.")
                return
            self._show_map(emap, _decode_image(content) if content else None)

        def _failed(e: Exception) -> None:
            if request == self._request:
                self._on_load_error(e)

        run_async(service.load_map_with_image(emap_id), callback=_loaded, error_callback=_failed)

    def _on_load_error(self, error: Exception) -> None:
        self._refresh_btn.set_sensitive(True)
        if emaps_unavailable(error):
            log.info("E-Maps not available: %s", error)
            self._unavailable = True
            self._refresh_btn.set_sensitive(False)
            self.listbox.set_sensitive(False)
            self._show_message(
                "E-Maps are not available: this Surveillance Station has none, "
                "or this account may not view them."
            )
            return
        log.error("Failed to load E-Maps: %s", error)
        self._listed = False
        self._show_message(f"E-Maps could not be loaded: {error}")

    def _show_message(self, text: str) -> None:
        self._message.set_label(text)
        self._content.set_visible_child_name("message")

    def _show_map(self, emap: EMap, pixbuf: GdkPixbuf.Pixbuf | None) -> None:
        self._emap = emap
        self._pixbuf = pixbuf
        self._image_note.set_visible(pixbuf is None)
        self._content.set_visible_child_name("map")
        self.area.queue_draw()

    # -- drawing and pointer -------------------------------------------

    def _image_size(self) -> tuple[float, float]:
        """The size markers are placed against: the decoded image's, else
        the one the NAS reports, else a blank square."""
        if self._pixbuf is not None:
            return float(self._pixbuf.get_width()), float(self._pixbuf.get_height())
        if self._emap is not None and self._emap.width > 0 and self._emap.height > 0:
            return float(self._emap.width), float(self._emap.height)
        # lng is a fraction of the height and can run past 1: widen the
        # square to keep every marker on it.
        widest = max((m.lng for m in self._emap.cameras), default=1.0) if self._emap else 1.0
        height = float(_PLACEHOLDER_SIZE)
        return height * max(1.0, widest + 0.05), height

    def _marker_points(
        self, area_w: float, area_h: float
    ) -> tuple[list[EMapItem], list[tuple[float, float]]]:
        """The camera markers and where each sits in an area_w x area_h
        drawing area."""
        if self._emap is None:
            return [], []
        width, height = self._image_size()
        markers = list(self._emap.cameras)
        points = [marker_to_widget(m.lng, m.lat, width, height, area_w, area_h) for m in markers]
        return markers, points

    def _cameras_by_id(self) -> dict[int, Camera]:
        return {cam.id: cam for cam in self.window.sidebar.cameras}

    def _draw(self, _area: Gtk.DrawingArea, cr: cairo.Context, width: int, height: int) -> None:
        if self._emap is None:
            return
        image_w, image_h = self._image_size()
        x_off, y_off, scale = fit_image(image_w, image_h, width, height)
        if scale <= 0:
            return

        cr.save()
        cr.translate(x_off, y_off)
        cr.scale(scale, scale)
        if self._pixbuf is not None:
            Gdk.cairo_set_source_pixbuf(cr, self._pixbuf, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD)
            cr.paint()
        else:
            cr.rectangle(0, 0, image_w, image_h)
            cr.set_source_rgba(0.5, 0.5, 0.5, 0.15)
            cr.fill()
        cr.restore()

        cameras = self._cameras_by_id()
        markers, points = self._marker_points(width, height)
        for marker, (x, y) in zip(markers, points, strict=True):
            cr.arc(x, y, _MARKER_RADIUS, 0, 2 * math.pi)
            cr.set_source_rgb(*marker_colour(cameras.get(marker.ref_item_id)))
            cr.fill_preserve()
            cr.set_source_rgb(1, 1, 1)
            cr.set_line_width(2)
            cr.stroke()

    def _marker_under(self, x: float, y: float) -> EMapItem | None:
        markers, points = self._marker_points(self.area.get_width(), self.area.get_height())
        index = marker_at(points, x, y, _HIT_RADIUS)
        return None if index is None else markers[index]

    def _on_query_tooltip(
        self,
        _area: Gtk.DrawingArea,
        x: int,
        y: int,
        _keyboard: bool,
        tooltip: Gtk.Tooltip,
    ) -> bool:
        marker = self._marker_under(x, y)
        if marker is None:
            return False
        camera = self._cameras_by_id().get(marker.ref_item_id)
        tooltip.set_text(camera.name if camera else f"Camera {marker.ref_item_id}")
        return True

    def _on_motion(self, _controller: Gtk.EventControllerMotion, x: float, y: float) -> None:
        over = self._marker_under(x, y) is not None
        self.area.set_cursor_from_name("pointer" if over else None)

    def _on_click(self, _gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        if n_press != 1:
            return
        marker = self._marker_under(x, y)
        if marker is None:
            return
        camera = self._cameras_by_id().get(marker.ref_item_id)
        if camera is None:
            return
        # Live View first: the window hands a camera pick to the page on
        # screen, and this way it is Live View's, as from the sidebar.
        self.window.show_page("live")
        self.window.on_camera_selected(camera)
