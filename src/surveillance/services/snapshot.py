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

"""Snapshot management service."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from surveillance.api.models import Snapshot
from surveillance.services.download import write_download

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI


async def take_and_save_snapshot(api: SurveillanceAPI, camera_id: int, ds_id: int = 0) -> int:
    """Take a snapshot and persist it to the server's snapshot database
    (so it shows up on the Snapshots page), returning the new snapshot's id.

    This is SnapShot::TakeSnapshot with blSave=true — NOT
    Camera::GetSnapshot, which was tried first: that only returns a raw
    live JPEG and never touches the snapshot database at all, so "Take
    Snapshot" appeared to succeed (a real image came back) but nothing
    ever showed up on the Snapshots page. Confirmed against Synology's
    official Web API reference and verified against a real NAS.
    """
    data = await api.request(
        api="SYNO.SurveillanceStation.SnapShot",
        method="TakeSnapshot",
        version=1,
        extra_params={
            "dsId": str(ds_id),
            "camId": str(camera_id),
            "blSave": "true",
        },
    )
    return int(data.get("id", 0))


async def fetch_camera_snapshot(api: SurveillanceAPI, camera_id: int) -> bytes:
    """The camera's current picture as a JPEG, saved nowhere. Camera
    GetSnapshot at version 3 with cameraId, as the official app asks for
    it (ApiSrvCamera's snapshot URL)."""
    return await api.download(
        api="SYNO.SurveillanceStation.Camera",
        method="GetSnapshot",
        version=3,
        extra_params={"cameraId": str(camera_id)},
    )


async def list_snapshots(
    api: SurveillanceAPI,
    camera_id: int | None = None,
    from_time: int | None = None,
    to_time: int | None = None,
    offset: int = 0,
    limit: int = 50,
) -> tuple[list[Snapshot], int]:
    """List saved snapshots, optionally filtered by camera and time range.

    Confirmed against the official Synology client (SrvSnapshotListTask):
    the pagination param is `start`, not `offset`, and time-range filtering
    uses `from`/`to` (not fromTime/toTime). `camera_id` is sent as
    `camIdList` (the name the official client uses, which only ever sends
    one value), but whether the server actually honors it appears to vary
    by NAS/DSM version — confirmed silently ignored (returning every
    camera's snapshots) on at least one real NAS, despite matching the
    official client exactly. Treat it as a best-effort hint to shrink the
    response, never as guaranteed filtering: callers must still filter the
    results themselves, see ui/snapshots.py.

    Returns (snapshots, total_count).
    """
    params: dict[str, str] = {
        "start": str(offset),
        "limit": str(limit),
    }
    if camera_id is not None:
        params["camIdList"] = str(camera_id)
    if from_time is not None:
        params["from"] = str(from_time)
    if to_time is not None:
        params["to"] = str(to_time)

    data = await api.request(
        api="SYNO.SurveillanceStation.SnapShot",
        method="List",
        version=1,
        extra_params=params,
    )

    snapshots = [Snapshot.from_api(s) for s in data.get("data", data.get("snapshot", []))]
    total = data.get("total", len(snapshots))
    return snapshots, total


async def fetch_snapshot_image(api: SurveillanceAPI, snapshot_id: int) -> bytes:
    """Fetch a snapshot's full image bytes without writing to disk — used
    both for the browser's in-row thumbnail and the full-size picture
    viewer, since SnapShot has no separate lightweight thumbnail endpoint
    the way Recording::GetThumbnail does."""
    return await api.download(
        api="SYNO.SurveillanceStation.SnapShot",
        method="Download",
        version=1,
        extra_params={"id": str(snapshot_id)},
    )


async def download_snapshot(
    api: SurveillanceAPI,
    snapshot_id: int,
    output_path: Path,
) -> Path:
    """Download a snapshot to disk.

    Raises ValueError if the server returned an error body rather than an
    image. A partial file from a failed write is removed.
    """
    data = await fetch_snapshot_image(api, snapshot_id)
    return await write_download(data, output_path, f"Snapshot {snapshot_id}")


async def delete_snapshot(api: SurveillanceAPI, snapshot_id: int, ds_id: int = 0) -> None:
    """Delete a single snapshot.

    Confirmed against Synology's official Surveillance Station Web API
    reference: Delete takes objList, a JSON array of {"id":
    "{dsId}:{snapshotId}"} objects — NOT a simple idList param. The
    previous idList-based call sent a parameter this method doesn't
    recognize at all; DSM silently ignored it and deleted every snapshot
    in the database instead of just this one (confirmed the hard way
    against a real NAS — see git history/PR discussion for context).
    """
    await api.request(
        api="SYNO.SurveillanceStation.SnapShot",
        method="Delete",
        version=1,
        extra_params={"objList": json.dumps([{"id": f"{ds_id}:{snapshot_id}"}])},
    )
