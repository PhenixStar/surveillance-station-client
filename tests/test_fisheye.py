"""Tests for the fisheye dewarp state, lens parsing and config."""

import pytest

from surveillance.ui.fisheye import (
    MODE_PANORAMA,
    MODE_PTZ,
    MOUNT_CEILING,
    MOUNT_WALL,
    PTZ_DEFAULT_FOV,
    PTZ_DEFAULT_TILT,
    PTZ_FOV_MAX,
    PTZ_FOV_MIN,
    DewarpView,
    Lens,
    parse_lens,
    shader_sources,
)


def test_parse_lens_defaults_and_values() -> None:
    assert parse_lens("") == Lens()
    assert parse_lens("0.51,0.49,0.47,190") == Lens(0.51, 0.49, 0.47, 190.0)
    # Bad or out-of-range fields keep their defaults.
    assert parse_lens("x,0.4,0,5") == Lens(center_y=0.4)
    assert parse_lens("0.5,0.5,0.45") == Lens(radius=0.45)


def test_ptz_zoom_is_clamped() -> None:
    view = DewarpView(mode=MODE_PTZ)
    for _ in range(50):
        view.zoom(1.0)
    assert view.fov == PTZ_FOV_MIN
    for _ in range(50):
        view.zoom(-1.0)
    assert view.fov == PTZ_FOV_MAX


def test_ptz_drag_pans_and_tilts_within_the_lens() -> None:
    view = DewarpView(mode=MODE_PTZ, lens=Lens(fov=180.0))
    view.drag(0.25, 0.0)
    assert view.pan == (-0.25 * PTZ_DEFAULT_FOV) % 360.0
    view.drag(0.0, 10.0)
    assert view.tilt == 90.0  # never past the rim
    view.drag(0.0, -10.0)
    assert view.tilt == 0.0  # never past straight down
    view.reset()
    assert (view.pan, view.tilt, view.fov) == (0.0, PTZ_DEFAULT_TILT, PTZ_DEFAULT_FOV)


def test_panorama_drag_rotates_and_scroll_does_nothing() -> None:
    view = DewarpView(mode=MODE_PANORAMA)
    view.drag(0.5, 0.3)
    assert view.pan == 270.0
    fov = view.fov
    view.zoom(1.0)
    assert view.fov == fov


def test_off_is_inactive_and_ignores_input() -> None:
    view = DewarpView()
    assert not view.active
    view.drag(0.5, 0.5)
    view.zoom(1.0)
    assert (view.pan, view.fov) == (0.0, PTZ_DEFAULT_FOV)


def test_shader_sources_match_the_gl_flavour() -> None:
    vertex, fragment = shader_sources(use_es=False)
    assert vertex.startswith("#version 330 core")
    assert "u_half_fov" in fragment
    vertex, fragment = shader_sources(use_es=True)
    assert vertex.startswith("#version 300 es") and "precision highp float;" in fragment


def test_dewarp_settings_are_kept_per_profile() -> None:
    from surveillance.config import _profile_state_from, _profile_state_to

    state = _profile_state_from(
        {"camera_dewarp": {"23": "ptz"}, "camera_fisheye_lens": {"23": "0.5,0.5,0.47,180"}}
    )
    assert state["camera_dewarp"] == {23: "ptz"}
    assert state["camera_fisheye_lens"] == {23: "0.5,0.5,0.47,180"}
    tables = _profile_state_to(state)
    assert tables["camera_dewarp"] == {"23": "ptz"}
    state = _profile_state_from({"camera_fisheye_mount": {"23": "wall"}})
    assert state["camera_fisheye_mount"] == {23: "wall"}
    assert _profile_state_to(state)["camera_fisheye_mount"] == {"23": "wall"}


class _Player:
    """What MpvGLArea.set_dewarp touches on its widget."""

    def __init__(self) -> None:
        self._dewarp = DewarpView()
        self._dewarp_failed = False
        self.resets = 0

    def reset_zoom(self) -> None:
        self.resets += 1

    def queue_render(self) -> None:
        pass


def test_set_dewarp_keeps_the_view_when_nothing_changes() -> None:
    """A Reload or Camera Settings Apply restarts the stream and sets the
    dewarp again; only a change drops the zoom and the steered view."""
    from surveillance.ui.mpv_widget import MpvGLArea

    player = _Player()
    MpvGLArea.set_dewarp(player, "")  # type: ignore[arg-type]
    assert player.resets == 0
    MpvGLArea.set_dewarp(player, MODE_PTZ)  # type: ignore[arg-type]
    assert (player._dewarp.mode, player.resets) == (MODE_PTZ, 1)
    MpvGLArea.set_dewarp(player, MODE_PTZ, Lens())  # type: ignore[arg-type]
    assert player.resets == 1
    MpvGLArea.set_dewarp(player, MODE_PTZ, Lens(radius=0.45))  # type: ignore[arg-type]
    assert player.resets == 2
    MpvGLArea.set_dewarp(player, "")  # type: ignore[arg-type]
    assert (player._dewarp.active, player.resets) == (False, 3)


def test_set_dewarp_stays_off_after_a_failure() -> None:
    """GL that could not dewarp once is not asked again by the next
    stream, which would only log the same traceback."""
    from surveillance.ui.mpv_widget import MpvGLArea

    player = _Player()
    player._dewarp_failed = True
    MpvGLArea.set_dewarp(player, MODE_PANORAMA)  # type: ignore[arg-type]
    assert not player._dewarp.active


def test_camera_dewarp_reads_the_camera_entries() -> None:
    from types import SimpleNamespace

    from surveillance.ui.fisheye import camera_dewarp

    config = SimpleNamespace(
        camera_dewarp={7: MODE_PTZ},
        camera_fisheye_lens={7: ",,0.45"},
        camera_fisheye_mount={7: MOUNT_WALL},
    )
    assert camera_dewarp(config, 7) == (MODE_PTZ, Lens(radius=0.45), MOUNT_WALL)  # type: ignore[arg-type]
    assert camera_dewarp(config, 8) == ("", Lens(), MOUNT_CEILING)  # type: ignore[arg-type]


def test_the_recording_player_dewarps_its_camera(monkeypatch: pytest.MonkeyPatch) -> None:
    """A recording opened from Recordings, Events or Time Lapse showed the
    raw circle of a camera Live View dewarps."""
    from types import SimpleNamespace

    from surveillance.ui import player
    from surveillance.ui.player import PlayerDialog

    monkeypatch.setattr(player, "get_stream_url", lambda api, rec: "https://nas/stream")
    calls: list[tuple[object, ...]] = []
    dialog = SimpleNamespace(
        app=SimpleNamespace(
            api=object(),
            config=SimpleNamespace(
                camera_dewarp={7: MODE_PANORAMA}, camera_fisheye_lens={}, camera_fisheye_mount={}
            ),
        ),
        recording=SimpleNamespace(camera_id=7),
        player=SimpleNamespace(set_dewarp=lambda *args: calls.append(("dewarp", *args))),
        _on_stream_url=lambda url: calls.append(("play", url)),
    )
    PlayerDialog._start_playback(dialog)  # type: ignore[arg-type]
    assert calls == [
        ("dewarp", MODE_PANORAMA, Lens(), MOUNT_CEILING),
        ("play", "https://nas/stream"),
    ]


def test_wall_ptz_starts_level_and_stays_within_the_lens() -> None:
    view = DewarpView(mode=MODE_PTZ, lens=Lens(fov=180.0), mount=MOUNT_WALL)
    view.reset()
    assert (view.pan, view.tilt) == (0.0, 0.0)
    view.drag(-0.5, 0.25)  # content dragged left and down: look right and up
    assert view.pan == 0.5 * PTZ_DEFAULT_FOV
    assert view.tilt == 0.25 * PTZ_DEFAULT_FOV
    view.drag(-10.0, -10.0)
    assert (view.pan, view.tilt) == (90.0, -90.0)  # no wrap past the lens


def test_wall_panorama_does_not_move() -> None:
    view = DewarpView(mode=MODE_PANORAMA, mount=MOUNT_WALL)
    view.drag(0.5, 0.3)
    assert (view.pan, view.tilt) == (0.0, PTZ_DEFAULT_TILT)


def test_set_dewarp_resets_the_view_on_a_mount_change() -> None:
    from surveillance.ui.mpv_widget import MpvGLArea

    player = _Player()
    MpvGLArea.set_dewarp(player, MODE_PTZ)  # type: ignore[arg-type]
    MpvGLArea.set_dewarp(player, MODE_PTZ, None, MOUNT_CEILING)  # type: ignore[arg-type]
    assert player.resets == 1
    MpvGLArea.set_dewarp(player, MODE_PTZ, None, MOUNT_WALL)  # type: ignore[arg-type]
    assert (player._dewarp.mount, player.resets) == (MOUNT_WALL, 2)


def test_camera_settings_keep_no_entry_for_the_defaults() -> None:
    from types import SimpleNamespace

    from surveillance.config import AppConfig
    from surveillance.ui.camera_settings import CameraSettingsDialog
    from surveillance.ui.fisheye import MODES, MOUNTS

    selected = {"dewarp": list(MODES).index(MODE_PTZ), "mount": list(MOUNTS).index(MOUNT_WALL)}
    config = AppConfig()
    dialog = SimpleNamespace(
        app=SimpleNamespace(config=config),
        cam=SimpleNamespace(id=3),
        _dewarp_modes=list(MODES),
        _dewarp_dropdown=SimpleNamespace(get_selected=lambda: selected["dewarp"]),
        _mounts=list(MOUNTS),
        _mount_dropdown=SimpleNamespace(get_selected=lambda: selected["mount"]),
        _lens=Lens(0.51, 0.5, 0.47, 190.0),
    )
    CameraSettingsDialog._save_fisheye(dialog)  # type: ignore[arg-type]
    assert (config.camera_dewarp, config.camera_fisheye_mount) == ({3: MODE_PTZ}, {3: MOUNT_WALL})
    assert config.camera_fisheye_lens == {3: "0.51,0.5,0.47,190"}
    selected.update(dewarp=0, mount=0)
    # A spin button's float noise is still the default lens.
    dialog._lens = Lens(radius=0.48000000000000004)
    CameraSettingsDialog._save_fisheye(dialog)  # type: ignore[arg-type]
    assert (config.camera_dewarp, config.camera_fisheye_mount) == ({}, {})
    assert config.camera_fisheye_lens == {}


def test_format_lens_is_read_back_by_parse_lens() -> None:
    from surveillance.ui.fisheye import format_lens

    assert format_lens(Lens()) == "0.5,0.5,0.48,180"
    lens = Lens(0.512345, 0.49, 0.466, 187.0)
    assert format_lens(lens) == "0.5123,0.49,0.466,187"
    assert parse_lens(format_lens(lens)) == Lens(0.5123, 0.49, 0.466, 187.0)


def test_the_lens_circle_follows_the_fitted_picture() -> None:
    from surveillance.ui.lens_calibration import fitted_rect, lens_circle

    # A 4:3 picture in a wide widget is centred with bars left and right.
    rect = fitted_rect(800, 300, 400, 300)
    assert rect == (200.0, 0.0, 400.0, 300.0)
    assert lens_circle(rect, Lens()) == (400.0, 150.0, 0.48 * 300)
    # In a tall one, bars above and below; the radius follows the height.
    rect = fitted_rect(400, 600, 400, 300)
    assert rect == (0.0, 150.0, 400.0, 300.0)
    assert lens_circle(rect, Lens(0.25, 0.0, 0.5)) == (100.0, 150.0, 150.0)


def test_the_calibration_picture_is_asked_for_as_the_official_app_does() -> None:
    import asyncio

    from surveillance.services.snapshot import fetch_camera_snapshot

    calls: list[dict[str, object]] = []

    class _Api:
        async def download(self, **kwargs: object) -> bytes:
            calls.append(kwargs)
            return b"jpeg"

    assert asyncio.run(fetch_camera_snapshot(_Api(), 7)) == b"jpeg"  # type: ignore[arg-type]
    assert calls == [
        {
            "api": "SYNO.SurveillanceStation.Camera",
            "method": "GetSnapshot",
            "version": 3,
            "extra_params": {"cameraId": "7"},
        }
    ]


def test_wall_ptz_never_tilts_past_straight_up_or_down() -> None:
    """A lens wider than 180 degrees let the view tilt over the top, and
    the picture came back upside down."""
    view = DewarpView(mode=MODE_PTZ, lens=Lens(fov=200.0), mount=MOUNT_WALL)
    view.reset()
    view.drag(-10.0, 10.0)
    assert (view.pan, view.tilt) == (100.0, 90.0)  # the lens still reaches 100 sideways
    view.drag(0.0, -10.0)
    assert view.tilt == -90.0
