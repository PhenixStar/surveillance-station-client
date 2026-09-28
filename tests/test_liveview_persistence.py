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

"""Tests for Live View layout persistence (config-level)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from surveillance.config import AppConfig, ConnectionProfile, _write_config, load_config


def _profiled(**kwargs: Any) -> AppConfig:
    """An AppConfig logged into one profile, which is where camera-keyed
    settings live (see PROFILE_STATE_FIELDS)."""
    return AppConfig(
        default_profile="nas",
        active_profile="nas",
        profiles={"nas": ConnectionProfile("nas", "192.168.1.10")},
        **kwargs,
    )


class TestLayoutCamerasConfigRoundTrip:
    """layout_cameras persists correctly through save/load."""

    def test_single_layout_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled(grid_layout="2x2")
        config.layout_cameras["2x2"] = [1, 2, 0, 3]

        _write_config(config)
        loaded = load_config()

        assert loaded.grid_layout == "2x2"
        assert loaded.layout_cameras.get("2x2") == [1, 2, 0, 3]

    def test_multi_layout_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled(grid_layout="4x4")
        config.layout_cameras["1x1"] = [5]
        config.layout_cameras["2x2"] = [1, 2, 3, 4]
        config.layout_cameras["3x3"] = [1, 2, 3, 4, 5, 6, 7, 8, 9]
        config.layout_cameras["4x4"] = list(range(1, 17))

        _write_config(config)
        loaded = load_config()

        assert loaded.layout_cameras["1x1"] == [5]
        assert loaded.layout_cameras["2x2"] == [1, 2, 3, 4]
        assert loaded.layout_cameras["3x3"] == [1, 2, 3, 4, 5, 6, 7, 8, 9]
        assert loaded.layout_cameras["4x4"] == list(range(1, 17))

    def test_empty_layout_cameras_default(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled()
        _write_config(config)
        loaded = load_config()

        assert loaded.layout_cameras == {}

    def test_camera_zeros_preserved(self, tmp_path: Path, monkeypatch: object) -> None:
        """Slots with no camera are stored as 0 and restored correctly."""
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled(grid_layout="2x2")
        config.layout_cameras["2x2"] = [7, 0, 0, 12]

        _write_config(config)
        loaded = load_config()

        assert loaded.layout_cameras["2x2"] == [7, 0, 0, 12]

    def test_layout_cameras_not_overwritten_on_1x1_switch(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """Switching to 1x1 must not erase other layouts' saved cameras."""
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        # Simulate state after switching: both layouts are present in config
        config = _profiled(grid_layout="1x1")
        config.layout_cameras["2x2"] = [1, 2, 3, 4]  # preserved from before switch
        config.layout_cameras["1x1"] = [5]  # new 1x1 selection

        _write_config(config)
        loaded = load_config()

        assert loaded.grid_layout == "1x1"
        assert loaded.layout_cameras["1x1"] == [5]
        assert loaded.layout_cameras["2x2"] == [1, 2, 3, 4]

    def test_grid_layout_persisted(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        for layout in ("1x1", "2x2", "3x3", "4x4"):
            config = _profiled(grid_layout=layout)
            _write_config(config)
            loaded = load_config()
            assert loaded.grid_layout == layout


class TestCameraProtocolPersistence:
    def test_camera_protocols_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled()
        config.camera_protocols = {1: "rtsp", 2: "mjpeg", 3: "websocket"}

        _write_config(config)
        loaded = load_config()

        assert loaded.camera_protocols[1] == "rtsp"
        assert loaded.camera_protocols[2] == "mjpeg"
        assert loaded.camera_protocols[3] == "websocket"

    def test_camera_overrides_round_trip(self, tmp_path: Path, monkeypatch: object) -> None:
        import surveillance.config as cfg

        monkeypatch.setattr(cfg, "CONFIG_FILE", tmp_path / "config.toml")  # type: ignore[attr-defined]
        monkeypatch.setattr(cfg, "CONFIG_DIR", tmp_path)  # type: ignore[attr-defined]

        config = _profiled()
        config.camera_overrides = {42: "rtsp://10.0.0.1:554/stream1"}

        _write_config(config)
        loaded = load_config()

        assert loaded.camera_overrides[42] == "rtsp://10.0.0.1:554/stream1"


class TestSaveWhileCameraListMissing:
    """While the NVR camera list has not loaded (e.g. Surveillance Station is
    restarting), every slot is empty; saving must not wipe the stored layout."""

    def _fake_view(self, cameras: list[object]) -> object:
        from types import SimpleNamespace

        config = AppConfig(grid_layout="4x4")
        config.layout_cameras["4x4"] = [10, 31, 46]
        empty_slot = SimpleNamespace(camera=None)
        return SimpleNamespace(
            app=SimpleNamespace(config=config),
            window=SimpleNamespace(sidebar=SimpleNamespace(cameras=cameras)),
            _cameras=[],
            _active=[0, 1, 2],
            _slots=[empty_slot] * 3,
            _current_layout="4x4",
            _request_presence_refresh=lambda: None,
        )

    def test_layout_kept_without_camera_list(self, monkeypatch: object) -> None:
        import surveillance.ui.liveview as lv

        monkeypatch.setattr(lv, "save_config_now", lambda config: None)  # type: ignore[attr-defined]
        view = self._fake_view(cameras=[])
        view._camera_list_loaded = lambda: lv.LiveView._camera_list_loaded(view)  # type: ignore[attr-defined]
        lv.LiveView._save_session(view)  # type: ignore[arg-type]
        lv.LiveView._save_layout_cameras(view)  # type: ignore[arg-type]
        assert view.app.config.layout_cameras["4x4"] == [10, 31, 46]  # type: ignore[attr-defined]

    def test_layout_saved_once_camera_list_loaded(self, monkeypatch: object) -> None:
        import surveillance.ui.liveview as lv

        monkeypatch.setattr(lv, "save_config_now", lambda config: None)  # type: ignore[attr-defined]
        view = self._fake_view(cameras=[object()])
        view._camera_list_loaded = lambda: lv.LiveView._camera_list_loaded(view)  # type: ignore[attr-defined]
        lv.LiveView._save_session(view)  # type: ignore[arg-type]
        assert view.app.config.layout_cameras["4x4"] == [0, 0, 0]  # type: ignore[attr-defined]
