"""Tests for the fisheye dewarp state, lens parsing and config."""

from surveillance.ui.fisheye import (
    MODE_PANORAMA,
    MODE_PTZ,
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
