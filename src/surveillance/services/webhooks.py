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

"""Outgoing webhooks: tell other systems what the client sees.

Each event the [webhooks] section asks for is POSTed as JSON to every
target there, with an optional bearer token kept in the keyring under
"<profile>:webhook_token". Unlike every other service module this talks
to the receivers, not to the NAS.

Fire-and-forget: the GTK side only hands an event over to the asyncio
bridge's loop, where each target has a bounded queue and a worker of its
own, so a receiver that is slow or down delays nobody else and never the
UI. A full queue drops its oldest event; a target that fails is logged
once, and again once it takes an event again, rather than per event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from surveillance.api.models import Camera, CameraStatus
from surveillance.config import WebhookConfig
from surveillance.credentials import webhook_token
from surveillance.util.async_bridge import get_loop

log = logging.getLogger(__name__)

# Events held per target while it is slow or down; beyond that the
# oldest is dropped.
QUEUE_SIZE = 64


def redacted(url: str) -> str:
    """*url* without what may be a secret (user info, query, fragment),
    for the log."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    with contextlib.suppress(ValueError):
        if parts.port is not None:
            host = f"{host}:{parts.port}"
    return f"{parts.scheme}://{host}{parts.path}"


def _now() -> str:
    """The current time as RFC 3339 UTC, e.g. 2026-01-31T09:12:03Z."""
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class _Target:
    """One receiver: its queue, the worker emptying it, and whether it
    is currently failing (logged once per spell)."""

    def __init__(self, url: str, queue_size: int) -> None:
        self.url = url
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_size)
        self.task: asyncio.Task[None] | None = None
        self.failing = False
        self.overflowing = False

    def put(self, payload: dict[str, Any]) -> None:
        if self.queue.full():
            self.queue.get_nowait()
            self.queue.task_done()
            # A failing target has had its line already.
            if not self.overflowing and not self.failing:
                log.warning(
                    "Webhooks: %s is behind, dropping its oldest events", redacted(self.url)
                )
                self.overflowing = True
        self.queue.put_nowait(payload)

    async def run(self, client: httpx.AsyncClient) -> None:
        while True:
            payload = await self.queue.get()
            try:
                await self._post(client, payload)
            finally:
                self.queue.task_done()
            if self.queue.empty():
                self.overflowing = False

    async def _post(self, client: httpx.AsyncClient, payload: dict[str, Any]) -> None:
        try:
            response = await client.post(self.url, json=payload)
            response.raise_for_status()
        except Exception as e:
            # The type and status only: an exception's text quotes the
            # URL, credentials included.
            reason = (
                f"HTTP {e.response.status_code}"
                if isinstance(e, httpx.HTTPStatusError)
                else type(e).__name__
            )
            if not self.failing:
                log.warning(
                    "Webhooks: %s failed on %s (%s), dropping events until it takes one",
                    redacted(self.url),
                    payload["event"],
                    reason,
                )
                self.failing = True
            return
        if self.failing:
            log.warning("Webhooks: %s takes events again", redacted(self.url))
            self.failing = False


class WebhookDispatcher:
    """Sends one profile's client events to the [webhooks] targets.

    emit() and the note_*() methods are for the GTK main thread, the
    sending runs on *loop*. The note_*() methods turn the state each
    poll reports into events on a change, the first report of a session
    being the baseline rather than a change.
    """

    def __init__(
        self,
        settings: WebhookConfig,
        profile: str,
        loop: asyncio.AbstractEventLoop,
        queue_size: int = QUEUE_SIZE,
    ) -> None:
        self._settings = settings
        self._profile = profile
        self._loop = loop
        self._events = frozenset(settings.events)
        self._targets = [_Target(url, queue_size) for url in dict.fromkeys(settings.targets)]
        self._task: asyncio.Task[None] | None = None
        self._unread: int | None = None
        self._home_mode: bool | None = None
        self._online: dict[int, bool] | None = None
        self._layout: tuple[str, list[int]] | None = None

    def start(self) -> None:
        self._loop.call_soon_threadsafe(self._start_workers)

    def stop(self) -> None:
        """Stop sending, dropping whatever is still queued."""
        self._loop.call_soon_threadsafe(self._stop_workers)

    def _start_workers(self) -> None:
        async def _open_and_run() -> None:
            # Nothing awaits this task, so whatever ends it is logged
            # here or nowhere. Cancellation, from stop(), is not caught.
            try:
                token = await asyncio.to_thread(webhook_token, self._profile)
                headers = {"Authorization": f"Bearer {token}"} if token else {}
                async with httpx.AsyncClient(
                    timeout=self._settings.timeout_seconds, headers=headers
                ) as client:
                    await asyncio.gather(*(target.run(client) for target in self._targets))
            except Exception:
                log.exception("Webhooks stopped")

        self._task = self._loop.create_task(_open_and_run())

    def _stop_workers(self) -> None:
        if self._task is not None:
            self._task.cancel()

    async def wait_idle(self) -> None:
        """Until every event queued so far is sent or dropped."""
        await asyncio.gather(*(target.queue.join() for target in self._targets))

    def emit(
        self, event: str, camera: Camera | None = None, detail: dict[str, Any] | None = None
    ) -> None:
        """Queue *event* for every target, unless [webhooks] leaves it out."""
        if event not in self._events:
            return
        payload = {
            "event": event,
            "profile": self._profile,
            "camera_id": camera.id if camera else 0,
            "camera_name": camera.name if camera else "",
            "at": _now(),
            "detail": detail or {},
        }
        for target in self._targets:
            self._loop.call_soon_threadsafe(target.put, payload)

    def note_unread_alerts(self, count: int) -> None:
        """An "alert" whenever the unread notification count goes up."""
        previous, self._unread = self._unread, count
        if previous is not None and count > previous:
            self.emit("alert", detail={"unread": count, "new": count - previous})

    def note_home_mode(self, enabled: bool) -> None:
        """A "home_mode" whenever home mode is switched on or off."""
        previous, self._home_mode = self._home_mode, enabled
        if previous is not None and enabled != previous:
            self.emit("home_mode", detail={"enabled": enabled})

    def note_cameras(self, cameras: list[Camera]) -> None:
        """A "camera_offline" or "camera_online" for each camera whose
        status crossed between enabled and anything else (disconnected,
        disabled...)."""
        online = {c.id: c.status == CameraStatus.ENABLED for c in cameras}
        previous, self._online = self._online, online
        if previous is None:
            return
        for camera in cameras:
            was_online = previous.get(camera.id)
            if was_online is None or was_online == online[camera.id]:
                continue
            event = "camera_online" if online[camera.id] else "camera_offline"
            self.emit(event, camera, {"status": camera.status.name.lower()})

    def note_layout(self, grid: str, cameras: list[int]) -> None:
        """A "layout_changed" whenever Live View's grid or the camera in
        one of its slots (0 for none) changes."""
        layout = (grid, list(cameras))
        if layout != self._layout:
            self._layout = layout
            self.emit("layout_changed", detail={"grid": grid, "cameras": layout[1]})


# The dispatcher for the session logged into, None while logged out or
# when [webhooks] has no targets.
_dispatcher: WebhookDispatcher | None = None


def start(settings: WebhookConfig, profile: str) -> None:
    """Start sending *profile*'s events, at login."""
    global _dispatcher

    stop()
    if not settings.targets or not settings.events:
        return
    _dispatcher = WebhookDispatcher(settings, profile, get_loop())
    _dispatcher.start()


def stop() -> None:
    """Stop sending, at logout."""
    global _dispatcher

    if _dispatcher is not None:
        _dispatcher.stop()
        _dispatcher = None


def emit(event: str, camera: Camera | None = None, detail: dict[str, Any] | None = None) -> None:
    """See WebhookDispatcher.emit; nothing while webhooks are off."""
    if _dispatcher is not None:
        _dispatcher.emit(event, camera, detail)


def note_unread_alerts(count: int) -> None:
    if _dispatcher is not None:
        _dispatcher.note_unread_alerts(count)


def note_home_mode(enabled: bool) -> None:
    if _dispatcher is not None:
        _dispatcher.note_home_mode(enabled)


def note_cameras(cameras: list[Camera]) -> None:
    if _dispatcher is not None:
        _dispatcher.note_cameras(cameras)


def note_layout(grid: str, cameras: list[int]) -> None:
    if _dispatcher is not None:
        _dispatcher.note_layout(grid, cameras)
