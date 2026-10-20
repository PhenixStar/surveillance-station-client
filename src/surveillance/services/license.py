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

"""License management service."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
from typing import TYPE_CHECKING, Any

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from surveillance.api.models import LicenseInfo

if TYPE_CHECKING:
    from surveillance.api.client import SurveillanceAPI

log = logging.getLogger(__name__)

_LICENSE_SERVER_URL = "https://synosurveillance.synology.com:443/license_activation.php"


class OfflineLicenseError(RuntimeError):
    """Synology's offline license server refused an activation request."""


async def load_licenses(api: SurveillanceAPI) -> LicenseInfo:
    """Load license information from the NAS."""
    data = await api.request(
        api="SYNO.SurveillanceStation.License",
        method="Load",
        version=1,
    )
    return LicenseInfo.from_api(data)


async def delete_license(api: SurveillanceAPI, license_ids: list[int]) -> None:
    """Delete licenses by their IDs."""
    await api.request(
        api="SYNO.SurveillanceStation.License",
        method="DeleteKey",
        version=1,
        extra_params={"lic_list": ",".join(str(i) for i in license_ids)},
    )


async def add_license_online(api: SurveillanceAPI, license_keys: list[str]) -> None:
    """Add licenses via the NAS (online activation)."""
    await api.request(
        api="SYNO.SurveillanceStation.License",
        method="AddKey",
        version=1,
        extra_params={"licenseList": ",".join(license_keys)},
    )


async def get_device_info(api: SurveillanceAPI) -> tuple[str, str]:
    """Get NAS serial number and model via SYNO.SurveillanceStation.Info.

    Returns (serial, model). The serial only comes from version 2 on,
    and the model is DSModelName, as the official app reads it (asking
    for up to version 8; the client clamps to what the NAS offers).
    """
    data = await api.request(
        api="SYNO.SurveillanceStation.Info",
        method="GetInfo",
        version=8,
    )
    log.debug("Device info keys: %s", list(data.keys()))
    return data.get("serial", ""), data.get("DSModelName", "")


def _offline_encrypt(content: str, serial: str, seed: int) -> str:
    """Encrypt content for offline license activation.

    Uses AES-CBC with a key derived from seed + serial via MD5/SHA1 chains.
    """
    seed_str = str(seed)

    # Step 1: partial = md5(seed + serial)[:12]
    partial = hashlib.md5((seed_str + serial).encode()).hexdigest()[:12]

    # Step 2: key_hex = md5(seed + partial) -> 32 hex chars
    key_hex = hashlib.md5((seed_str + partial).encode()).hexdigest()

    # Step 3: SHA1 chain to derive AES key + IV (need 48 bytes)
    accumulated = b""
    running = b""
    while len(accumulated) < 48:
        running = running + key_hex.encode()
        for _ in range(5):
            running = hashlib.sha1(running).digest()
        accumulated += running

    # Step 4: AES key = first 32 bytes, IV = bytes 32-47
    aes_key = accumulated[:32]
    iv = accumulated[32:48]

    # Step 5: AES-CBC encrypt with PKCS7 padding
    padder = PKCS7(128).padder()
    padded = padder.update(content.encode()) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    return base64.b64encode(ciphertext).decode()


async def _offline_request(
    serial: str, model: str, payload: dict[str, Any]
) -> tuple[dict[str, Any], int]:
    """Send one encrypted request to Synology's offline license server.

    Returns the decoded reply together with the seed it was encrypted
    with: activation has to hand that same seed back to the NAS as
    encSeed for the reply to be accepted.
    """
    seed = random.randint(100000, 999999)
    cipher_text = _offline_encrypt(json.dumps(payload), serial, seed)

    async with httpx.AsyncClient(verify=True, timeout=30.0) as client:
        resp = await client.get(
            _LICENSE_SERVER_URL,
            params={
                "cipherText": cipher_text,
                "dsSN": serial,
                "dsModel": model,
                "seed": str(seed),
            },
        )
        resp.raise_for_status()
        result: dict[str, Any] = resp.json()
    return result, seed


def _check_offline_reply(result: dict[str, Any]) -> None:
    """Raise if the license server reported a failure.

    A rejection still comes back as HTTP 200, so the body is the only
    thing that says whether it worked. Synology's own client reads the
    same three fields in this order before it touches the payload.
    """
    if not result.get("success"):
        code = result.get("error_code") or (result.get("error") or {}).get("code")
        detail = f" (error {code})" if code else ""
        raise OfflineLicenseError(f"The license server rejected the request{detail}")
    if result.get("has_blocked"):
        blocked = result.get("checkList") or {}
        keys = ", ".join(str(k) for k in blocked) if blocked else "unknown"
        raise OfflineLicenseError(f"The license server blocked these keys: {keys}")


async def offline_get_timestamp(
    api: SurveillanceAPI,
    serial: str = "",
    model: str = "",
) -> int:
    """Get timestamp from Synology license server."""
    if not serial or not model:
        serial, model = await get_device_info(api)
    result, _ = await _offline_request(serial, model, {"method": "GetTimestamp"})
    ts = int(result.get("timestamp", 0))
    if not ts:
        raise RuntimeError("License server returned no timestamp")
    return ts


async def offline_activate(api: SurveillanceAPI, license_keys: list[str]) -> None:
    """Activate licenses offline.

    Two steps, both required: Synology's license server signs the keys and
    returns an encData blob, and that blob is what actually installs them.
    Sending the keys to the server without passing encData on to the NAS
    activates nothing, so the two calls belong in one operation.
    """
    serial, model = await get_device_info(api)
    result, seed = await _offline_request(
        serial,
        model,
        {
            "method": "Add",
            "dsModel": model,
            "dsSerial": serial,
            "dsMac": "XXXXXXXXXXXX",
            "licenseList": license_keys,
            "version": 2,
            "blOffline": True,
        },
    )
    _check_offline_reply(result)

    enc_data = result.get("encData", "")
    if not enc_data:
        raise OfflineLicenseError("The license server returned no activation data")

    await api.request(
        api="SYNO.SurveillanceStation.License",
        method="AddKey",
        version=1,
        extra_params={
            "licenseList": ",".join(license_keys),
            "encSeed": str(seed),
            "encData": enc_data,
        },
    )


async def offline_deactivate(
    api: SurveillanceAPI, license_keys: list[str], license_ids: list[int]
) -> None:
    """Deactivate licenses offline.

    Releasing the keys at Synology's license server only frees them for
    use elsewhere; the licenses stay installed until the NAS is told to
    delete them, so both calls are needed here too.
    """
    serial, model = await get_device_info(api)
    timestamp = await offline_get_timestamp(api, serial, model)

    lic_list = [{"dsModel": model, "dsSerial": serial, "key": key} for key in license_keys]
    result, _ = await _offline_request(
        serial,
        model,
        {
            "method": "Delete",
            "licenseList": lic_list,
            "timestamp": timestamp,
            "blOffline": True,
        },
    )
    _check_offline_reply(result)

    await delete_license(api, license_ids)
