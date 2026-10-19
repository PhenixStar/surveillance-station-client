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

"""Surveillance Station Transactions (POS) service.

A POS device in Surveillance Station is paired with a camera; each sale it
sends becomes a transaction whose text Surveillance Station shows over that
camera's video. This reads the device -> camera pairing and the latest
transactions so Live View can show the same receipt over the paired slot.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

DEVICE_API = "SYNO.SurveillanceStation.Transactions.Device"
TRANSACTION_API = "SYNO.SurveillanceStation.Transactions.Transaction"


@dataclass(frozen=True)
class Transaction:
    """One finished POS transaction: id is "<pos_id>_<serial>"."""

    id: str
    pos_id: int
    begin: int
    text: str


def _rows(data: Any) -> list[dict[str, Any]]:
    # Enum answers {"data": [...], "total": n}; tolerate a bare list too.
    if isinstance(data, dict):
        data = data.get("data", [])
    return [row for row in data or [] if isinstance(row, dict)]


def parse_devices(data: Any) -> dict[int, int]:
    """POS device id -> paired camera id, for devices with pairing enabled."""
    paired: dict[int, int] = {}
    for row in _rows(data):
        try:
            pos_id, cam_id = int(row["id"]), int(row["pairedcam_id"])
        except (KeyError, TypeError, ValueError):
            continue
        if row.get("pairedcam_enable") and cam_id > 0:
            paired[pos_id] = cam_id
    return paired


def parse_transactions(data: Any) -> list[Transaction]:
    """Transactions oldest first; each row's content lines joined as text.

    Rows carry camera_id 0 — the device pairing is the only link to a camera.
    """
    result: list[Transaction] = []
    for row in _rows(data):
        lines = [
            str(part.get("content", ""))
            for part in sorted(row.get("content") or [], key=lambda p: p.get("line_id", 0))
            if isinstance(part, dict)
        ]
        text = "\n".join(line for line in lines if line).strip()
        try:
            tx = Transaction(
                id=str(row["id"]),
                pos_id=int(row["pos_id"]),
                begin=int(row.get("begin_tmstmp") or 0),
                text=text,
            )
        except (KeyError, TypeError, ValueError):
            continue
        if tx.text:
            result.append(tx)
    return sorted(result, key=lambda t: (t.begin, t.id))


async def get_paired_devices(api: SurveillanceAPI) -> dict[int, int]:
    """POS device id -> paired camera id."""
    data = await api.request(api=DEVICE_API, method="Enum", version=1)
    return parse_devices(data)


async def get_recent_transactions(api: SurveillanceAPI, limit: int = 5) -> list[Transaction]:
    """The newest *limit* transactions, oldest first.

    Kept small on purpose: every row embeds a ~14 KB base64 snapshot.
    """
    data = await api.request(
        api=TRANSACTION_API,
        method="Enum",
        version=2,
        extra_params={"start": 0, "limit": limit},
    )
    return parse_transactions(data)
