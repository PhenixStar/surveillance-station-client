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

"""Tests for editing E-Maps: the upload, Save and Delete calls, the item
round trip, the stale-list guard and the Maps page's editing state."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from surveillance.api.client import ApiError, SurveillanceAPI
from surveillance.api.models import EMap, EMapItem
from surveillance.config import ConnectionProfile
from surveillance.services.emap import (
    EMAP_API,
    EMAP_IMAGE_API,
    DuplicateMapNameError,
    EMapService,
    StaleMapError,
    UploadedImage,
    emaps_read_only,
    emaps_unavailable,
)
from tests.test_emap import LIST_DATA, LOAD_DATA

UPLOAD_DATA: dict[str, Any] = {
    "filename": "emap-stored-7.png",
    "imgWidth": 1600,
    "imgHeight": 1000,
    "imageData": "iVBORw0KGgo=",
    "imageSize": 12,
}

UPLOADED = UploadedImage.from_api(UPLOAD_DATA)


def _api() -> SurveillanceAPI:
    api = SurveillanceAPI(ConnectionProfile(name="t", host="nas", port=5001, https=True))
    api.sid, api.username, api.password = "sid-1", "user", "pw"
    return api


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"success": True, "data": data})


def _error(code: int) -> httpx.Response:
    return httpx.Response(200, json={"success": False, "error": {"code": code}})


def _params(request: httpx.Request) -> dict[str, str]:
    """Query and form parameters of a request, as the NAS sees them."""
    params = dict(request.url.params)
    if request.method == "POST" and not request.headers.get("content-type", "").startswith(
        "multipart/"
    ):
        params.update(dict(httpx.QueryParams(request.content.decode())))
    return params


class _Nas:
    """A handler answering like Surveillance Station, recording what it got."""

    def __init__(self, maps: dict[str, Any] = LIST_DATA, loaded: dict[str, Any] = LOAD_DATA):
        self.maps = maps
        self.loaded = loaded
        self.requests: list[httpx.Request] = []
        self.save_answer: Any = {"addEmapId": 9, "emapObj": {}}
        self.fail_edits_with: int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        request.read()
        self.requests.append(request)
        params = _params(request)
        if params.get("api") == "SYNO.API.Auth":
            return _ok({"sid": "sid-2"})
        method = params.get("method", "")
        if method in ("Upload", "Save", "Delete") and self.fail_edits_with is not None:
            return _error(self.fail_edits_with)
        answers: dict[str, Any] = {
            "Upload": UPLOAD_DATA,
            "List": self.maps,
            "Load": self.loaded,
            "Save": self.save_answer,
            "Delete": {},
        }
        return _ok(answers[method]) if method in answers else _error(103)

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if _params(r).get("method") == method]


def _run(nas: _Nas, coro: Any) -> Any:
    with respx.mock(assert_all_called=False) as mock:
        mock.route(host="nas").mock(side_effect=nas)
        return asyncio.run(coro)


class TestUpload:
    def test_is_a_multipart_post_with_one_png_field(self) -> None:
        nas = _Nas()
        uploaded = _run(nas, EMapService(_api()).upload_image(b"\x89PNG\r\n", "floor.png"))

        (request,) = nas.requests
        assert request.method == "POST"
        query = dict(request.url.params)
        assert (query["api"], query["method"], query["version"]) == (EMAP_IMAGE_API, "Upload", "1")
        assert query["_sid"] == "sid-1"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        body = request.content
        assert body.count(b"Content-Disposition") == 1
        assert b'name="emapImg"' in body
        assert b'filename="floor.png"' in body
        assert b"Content-Type: image/png" in body
        assert b"\x89PNG\r\n" in body
        assert uploaded == UploadedImage("emap-stored-7.png", 1600, 1000, "iVBORw0KGgo=", 12)

    def test_an_answer_without_a_filename_is_an_error(self) -> None:
        with pytest.raises(ValueError, match="name"):
            UploadedImage.from_api({"imgWidth": 1})
        with pytest.raises(ValueError, match="name"):
            UploadedImage.from_api(None)

    def test_a_spectator_gets_105_after_one_fresh_login(self) -> None:
        nas = _Nas()
        nas.fail_edits_with = 105
        with pytest.raises(ApiError) as exc:
            _run(nas, EMapService(_api()).upload_image(b"png", "a.png"))
        assert exc.value.code == 105
        assert [_params(r).get("method") for r in nas.requests] == ["Upload", "Login", "Upload"]
        assert emaps_read_only(exc.value)

    def test_read_only_is_only_105(self) -> None:
        assert not emaps_read_only(ApiError(400))
        assert not emaps_read_only(ApiError(102))
        assert not emaps_read_only(OSError("down"))
        # The viewer's predicate stays the broader one.
        assert emaps_unavailable(ApiError(105))


class TestSaveParams:
    def test_create_posts_name_and_image_with_emap_id_zero(self) -> None:
        nas = _Nas()
        new_id = _run(nas, EMapService(_api()).create_map("Floor 2", UPLOADED))

        assert new_id == 9
        (save,) = nas.calls("Save")
        assert save.method == "POST"
        params = _params(save)
        assert (params["api"], params["method"], params["version"]) == (EMAP_API, "Save", "2")
        assert params["_sid"] == "sid-1"
        assert params["emapIds"] == json.dumps("0")
        assert params["name"] == json.dumps("Floor 2")
        assert params["imgName"] == json.dumps("emap-stored-7.png")
        assert params["imgData"] == json.dumps("iVBORw0KGgo=")
        assert (params["imgWidth"], params["imgHeight"], params["byteSize"]) == (
            "1600",
            "1000",
            "12",
        )
        assert "items" not in params

    def test_create_refuses_a_name_already_listed(self) -> None:
        nas = _Nas()
        with pytest.raises(DuplicateMapNameError):
            _run(nas, EMapService(_api()).create_map("map a", UPLOADED))
        assert nas.calls("Save") == []

    def test_create_without_an_id_in_the_answer_fails(self) -> None:
        nas = _Nas()
        nas.save_answer = {}
        with pytest.raises(ApiError):
            _run(nas, EMapService(_api()).create_map("Floor 2", UPLOADED))

    def test_replace_image_saves_the_image_fields_only(self) -> None:
        nas = _Nas()
        service = EMapService(_api())
        _run(nas, service.load_map_with_image(1))
        assert service._maps and service._images
        _run(nas, service.replace_image(1, UPLOADED))

        (save,) = nas.calls("Save")
        params = _params(save)
        assert params["emapIds"] == json.dumps("1")
        assert params["imgName"] == json.dumps("emap-stored-7.png")
        assert "name" not in params
        assert "items" not in params
        # The next load has to see the new image, not the cached one.
        assert not service._maps
        assert not service._images

    def test_delete_sends_the_map_id(self) -> None:
        nas = _Nas()
        service = EMapService(_api())
        _run(nas, service.load_map(1))
        _run(nas, service.delete_map(1))

        (delete,) = nas.calls("Delete")
        params = _params(delete)
        assert (params["api"], params["version"]) == (EMAP_API, "1")
        assert params["emapIds"] == json.dumps("1")
        assert not service._maps


class TestItemRoundTrip:
    def test_unknown_fields_survive_and_the_position_wins(self) -> None:
        emap = EMap.from_api(LOAD_DATA["emap"][0])
        item = emap.items[0]
        assert item.raw["radius"] == 20000
        moved = EMapItem(**{**item.__dict__, "lng": 0.9, "lat": 0.1})
        row = moved.to_api()
        assert row["id"] == 11
        assert row["radius"] == 20000
        assert row["vertices"] == []
        assert (row["refItemIdOnHost"], row["refItemType"]) == (5, 0)
        assert (row["lng"], row["lat"]) == (0.9, 0.1)

    def test_a_new_marker_sends_no_id(self) -> None:
        row = EMapItem.new_camera(42, 0.5, 0.5).to_api()
        assert "id" not in row
        assert row == {"refItemIdOnHost": 42, "refItemType": 0, "lat": 0.5, "lng": 0.5}

    def test_raw_is_not_part_of_equality(self) -> None:
        a = EMapItem.new_camera(1, 0.5, 0.5)
        b = EMapItem(**{**a.__dict__, "raw": {"id": 3}})
        assert a == b


class TestSaveMarkers:
    def test_sends_every_item_as_a_json_list(self) -> None:
        nas = _Nas()
        service = EMapService(_api())
        emap = _run(nas, service.load_map(1))
        assert emap is not None
        items = [*emap.items, EMapItem.new_camera(42, 0.5, 0.5)]
        _run(nas, service.save_markers(1, items, [i.id for i in emap.items]))

        (save,) = nas.calls("Save")
        assert save.method == "POST"
        params = _params(save)
        assert params["emapIds"] == json.dumps("1")
        sent = json.loads(params["items"])
        assert list(sent) == ["list"]
        rows = sent["list"]
        # The non-camera item (type 2) is kept, and so are the two rows
        # the client could not parse: Save replaces the list.
        assert [r.get("id") for r in rows] == [11, 12, 13, None, 14, 15]
        assert rows[0]["radius"] == 20000
        assert rows[3] == {"refItemIdOnHost": 42, "refItemType": 0, "lat": 0.5, "lng": 0.5}
        assert rows[4:] == LOAD_DATA["emap"][0]["item"][3:]
        assert not service._maps

    def test_reloads_first_and_refuses_a_changed_list(self) -> None:
        nas = _Nas()
        service = EMapService(_api())
        emap = _run(nas, service.load_map(1))
        assert emap is not None
        # Someone else added item 99 meanwhile.
        nas.loaded = {
            "emap": [
                {
                    **LOAD_DATA["emap"][0],
                    "item": [
                        *LOAD_DATA["emap"][0]["item"],
                        {"id": 99, "refItemType": 0, "refItemIdOnHost": 8, "lat": 0.2, "lng": 0.2},
                    ],
                }
            ]
        }
        with pytest.raises(StaleMapError):
            _run(nas, service.save_markers(1, emap.items, [i.id for i in emap.items]))
        assert len(nas.calls("Load")) == 2
        assert nas.calls("Save") == []

    def test_a_deleted_map_is_stale_too(self) -> None:
        nas = _Nas()
        nas.loaded = {"emap": []}
        with pytest.raises(StaleMapError):
            _run(nas, EMapService(_api()).save_markers(1, [], []))
        assert nas.calls("Save") == []

    def test_a_spectator_is_told_so(self) -> None:
        nas = _Nas()
        nas.fail_edits_with = 105
        with pytest.raises(ApiError) as exc:
            _run(nas, EMapService(_api()).save_markers(1, [], [11, 12, 13]))
        assert emaps_read_only(exc.value)
