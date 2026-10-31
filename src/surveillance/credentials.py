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

"""Secure credential storage via keyring."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import keyring

SERVICE_NAME = "surveillance-station"

log = logging.getLogger(__name__)


def store_credentials(profile_name: str, username: str, password: str) -> bool:
    """Store credentials for a connection profile.

    Returns False when the keyring is unavailable or locked. Remembering
    credentials is a convenience, so the caller carries on without it
    rather than failing the login it has already completed.
    """
    try:
        keyring.set_password(SERVICE_NAME, f"{profile_name}:username", username)
        keyring.set_password(SERVICE_NAME, f"{profile_name}:password", password)
    except keyring.errors.KeyringError:
        log.warning("Could not save credentials to the keyring for profile %s", profile_name)
        return False
    return True


def get_credentials(profile_name: str) -> tuple[str, str] | None:
    """Retrieve credentials for a connection profile.

    Returns (username, password), or None if they are not stored or the
    keyring is unavailable or locked.
    """
    try:
        username = keyring.get_password(SERVICE_NAME, f"{profile_name}:username")
        password = keyring.get_password(SERVICE_NAME, f"{profile_name}:password")
    except keyring.errors.KeyringError:
        log.warning("Could not read credentials from the keyring for profile %s", profile_name)
        return None
    if username is None or password is None:
        return None
    return username, password


def delete_credentials(profile_name: str) -> None:
    """Delete credentials for a connection profile."""
    with contextlib.suppress(keyring.errors.KeyringError):
        keyring.delete_password(SERVICE_NAME, f"{profile_name}:username")
    with contextlib.suppress(keyring.errors.KeyringError):
        keyring.delete_password(SERVICE_NAME, f"{profile_name}:password")


def webhook_token(profile_name: str) -> str | None:
    """The bearer token for *profile_name*'s webhooks (see
    services.webhooks), or None if there is none or the keyring is
    unavailable or locked."""
    try:
        return keyring.get_password(SERVICE_NAME, f"{profile_name}:webhook_token")
    except keyring.errors.KeyringError as e:
        log.debug("No webhook token from the keyring: %s", e)
        return None


# The blocking versions above talk to the Secret Service over D-Bus. A
# locked collection makes that call wait until the user answers the
# unlock prompt, and no timeout is set anywhere in the stack, so calling
# them from the GTK main thread freezes the whole UI for as long as the
# prompt is up. These wrappers keep that off the main thread.


async def get_credentials_async(profile_name: str) -> tuple[str, str] | None:
    """get_credentials() run off the calling thread."""
    return await asyncio.to_thread(get_credentials, profile_name)


async def store_credentials_async(profile_name: str, username: str, password: str) -> bool:
    """store_credentials() run off the calling thread."""
    return await asyncio.to_thread(store_credentials, profile_name, username, password)


async def delete_credentials_async(profile_name: str) -> None:
    """delete_credentials() run off the calling thread."""
    await asyncio.to_thread(delete_credentials, profile_name)
