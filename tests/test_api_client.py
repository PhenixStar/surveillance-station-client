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

"""Tests for API client."""

from __future__ import annotations

import pytest
import respx
from httpx import Response

from surveillance.api.client import ApiError, SurveillanceAPI
from surveillance.config import ConnectionProfile


@pytest.fixture
def profile() -> ConnectionProfile:
    return ConnectionProfile(
        name="test", host="192.168.1.100", port=5001, https=True, verify_ssl=False
    )


@pytest.fixture
def api(profile: ConnectionProfile) -> SurveillanceAPI:
    client = SurveillanceAPI(profile)
    client.sid = "test-sid"
    return client


class TestSurveillanceAPI:
    def test_init(self, profile: ConnectionProfile) -> None:
        api = SurveillanceAPI(profile)
        assert api.base_url == "https://192.168.1.100:5001"
        assert api.sid == ""

    @pytest.mark.asyncio
    @respx.mock
    async def test_discover_apis(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/query.cgi",
        ).mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "data": {
                        "SYNO.API.Auth": {
                            "path": "entry.cgi",
                            "minVersion": 1,
                            "maxVersion": 7,
                        },
                        "SYNO.SurveillanceStation.Camera": {
                            "path": "entry.cgi",
                            "minVersion": 1,
                            "maxVersion": 9,
                        },
                    },
                },
            )
        )

        await api.discover_apis()
        assert "SYNO.API.Auth" in api._api_info
        assert api._api_info["SYNO.API.Auth"].max_version == 7

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_request_success(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/entry.cgi",
        ).mock(
            return_value=Response(
                200,
                json={
                    "success": True,
                    "data": {"cameras": [{"id": 1, "name": "Test"}]},
                },
            )
        )

        data = await api.raw_request(
            api="SYNO.SurveillanceStation.Camera",
            method="List",
            version=9,
        )
        assert "cameras" in data
        assert data["cameras"][0]["id"] == 1

    @pytest.mark.asyncio
    @respx.mock
    async def test_raw_request_error(self, api: SurveillanceAPI) -> None:
        respx.get(
            "https://192.168.1.100:5001/webapi/entry.cgi",
        ).mock(
            return_value=Response(
                200,
                json={"success": False, "error": {"code": 102}},
            )
        )

        with pytest.raises(ApiError) as exc_info:
            await api.raw_request(
                api="SYNO.SurveillanceStation.Camera",
                method="List",
            )
        assert exc_info.value.code == 102

    def test_get_stream_url(self, api: SurveillanceAPI) -> None:
        url = api.get_stream_url("entry.cgi", {"api": "SYNO.Test", "method": "Stream"})
        assert "192.168.1.100:5001" in url
        assert "_sid=test-sid" in url
        assert "api=SYNO.Test" in url

    @pytest.mark.asyncio
    async def test_close(self, api: SurveillanceAPI) -> None:
        # Access client to create it
        _ = api.client
        await api.close()
        assert api._client is None


class TestLogout:
    @pytest.mark.asyncio
    @respx.mock
    async def test_clears_credentials_so_nothing_can_re_login(self, api: SurveillanceAPI) -> None:
        """request() re-logins on a session error whenever the username and
        password are still set, which after a logout would open a fresh
        session behind the user's back."""
        from surveillance.api.auth import logout

        api.username = "admin"
        api.password = "secret"  # noqa: S105
        respx.get(url__regex=r".*").mock(
            return_value=Response(200, json={"success": True, "data": {}})
        )

        await logout(api)

        assert api.sid == ""
        assert not api.username
        assert not api.password


class TestRelogin:
    """A session error logs in again with the stored credentials. One the
    NAS refuses must not be sent again by every later poll."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_refused_password_is_not_sent_again(self, api: SurveillanceAPI) -> None:
        from surveillance.api.auth import SessionExpiredError

        api.username = "admin"
        api.password = "stale"  # noqa: S105
        logins = respx.post(url__regex=r".*").mock(
            return_value=Response(200, json={"success": False, "error": {"code": 400}})
        )
        respx.get(url__regex=r".*").mock(
            return_value=Response(200, json={"success": False, "error": {"code": 119}})
        )

        # Every call, not only the first, says the session is gone for
        # good, and why: the pages that tell the user so show the message.
        reasons = []
        for _poll in range(4):
            with pytest.raises(SessionExpiredError) as exc_info:
                await api.request(api="SYNO.SurveillanceStation.Camera", method="List")
            reasons.append(str(exc_info.value))
        await api.close()

        assert logins.call_count == 1
        assert reasons == ["No such account or incorrect password"] * 4

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_busy_nas_keeps_the_password(self, api: SurveillanceAPI) -> None:
        """109 is the shared "network unstable or system busy" code, not
        a refusal: the next poll has to be able to log in."""
        from surveillance.api.auth import SessionExpiredError

        api.username = "admin"
        api.password = "right"  # noqa: S105
        logins = respx.post(url__regex=r".*").mock(
            side_effect=[
                Response(200, json={"success": False, "error": {"code": 109}}),
                Response(200, json={"success": True, "data": {"sid": "fresh"}}),
            ]
        )
        data = respx.get(url__regex=r".*").mock(
            side_effect=[
                Response(200, json={"success": False, "error": {"code": 119}}),
                Response(200, json={"success": False, "error": {"code": 119}}),
                Response(200, json={"success": True, "data": {"cameras": []}}),
            ]
        )

        with pytest.raises(SessionExpiredError):
            await api.request(api="SYNO.SurveillanceStation.Camera", method="List")
        assert await api.request(api="SYNO.SurveillanceStation.Camera", method="List") == {
            "cameras": []
        }
        await api.close()

        assert logins.call_count == 2
        assert data.call_count == 3
        assert api.sid == "fresh"

    @pytest.mark.asyncio
    @respx.mock
    async def test_a_login_with_no_answer_is_tried_again(self, api: SurveillanceAPI) -> None:
        import httpx

        api.username = "admin"
        api.password = "right"  # noqa: S105
        logins = respx.post(url__regex=r".*").mock(side_effect=httpx.ConnectError("down"))
        respx.get(url__regex=r".*").mock(
            return_value=Response(200, json={"success": False, "error": {"code": 119}})
        )

        for _poll in range(2):
            with pytest.raises(httpx.ConnectError):
                await api.request(api="SYNO.SurveillanceStation.Camera", method="List")
        await api.close()

        assert logins.call_count == 2
        assert api.password == "right"  # noqa: S105

    @pytest.mark.asyncio
    @respx.mock
    async def test_downloads_stop_after_a_refusal_too(self, profile: ConnectionProfile) -> None:
        from surveillance.api.auth import SessionExpiredError

        respx.post(url__regex=r".*").mock(
            return_value=Response(200, json={"success": False, "error": {"code": 403}})
        )
        respx.get(url__regex=r".*").mock(
            return_value=Response(200, json={"success": False, "error": {"code": 119}})
        )

        async def download(api: SurveillanceAPI) -> None:
            await api.download(api="SYNO.SurveillanceStation.Recording", method="Download")

        async def stream(api: SurveillanceAPI) -> None:
            async for _chunk in api.stream_download(
                api="SYNO.SurveillanceStation.Recording", method="Download"
            ):
                pass

        for fetch in (download, stream):
            api = SurveillanceAPI(profile)
            api.username = "admin"
            api.password = "stale"  # noqa: S105
            logins = respx.calls.call_count
            for _try in range(2):
                with pytest.raises(SessionExpiredError, match="Two-factor"):
                    await fetch(api)
            await api.close()
            # The first data request, its login, then the second data
            # request alone.
            assert respx.calls.call_count - logins == 3


class TestLoginRequest:
    """The password and OTP code travel in a POST body, as the official
    client sends them. In the URL they landed in every proxy's and DSM's
    access log."""

    @respx.mock
    async def test_credentials_are_not_in_the_url(self, profile: ConnectionProfile) -> None:
        from surveillance.api.auth import login

        route = respx.post(url__regex=r".*").mock(
            return_value=Response(200, json={"success": True, "data": {"sid": "new-sid"}})
        )
        api = SurveillanceAPI(profile)
        sid = await login(api, "alice", "s3cret!", otp_code="123456")
        await api.close()

        assert sid == "new-sid"
        request = route.calls.last.request
        assert request.method == "POST"
        assert "s3cret" not in str(request.url) and "123456" not in str(request.url)
        body = request.content.decode()
        assert "passwd=s3cret%21" in body and "otp_code=123456" in body
        assert "api=SYNO.API.Auth" in body and "method=Login" in body


class TestHttpStatusError:
    def test_message_has_no_credentials(self) -> None:
        import httpx

        from surveillance.api.client import HttpStatusError, _raise_for_status

        req = httpx.Request(
            "GET",
            "https://nas:5001/webapi/auth.cgi?api=SYNO.API.Auth&method=Login"
            "&account=admin&passwd=Sup3rSecret&otp_code=123456&_sid=abc123",
        )
        with pytest.raises(HttpStatusError) as excinfo:
            _raise_for_status(httpx.Response(500, request=req))
        message = str(excinfo.value)
        assert excinfo.value.status_code == 500
        for secret in ("Sup3rSecret", "123456", "abc123", "passwd", "nas:5001"):
            assert secret not in message

    def test_success_does_not_raise(self) -> None:
        import httpx

        from surveillance.api.client import _raise_for_status

        req = httpx.Request("GET", "https://nas:5001/webapi/entry.cgi")
        _raise_for_status(httpx.Response(200, request=req))  # must not raise


class TestNonJsonResponse:
    def test_html_body_becomes_an_api_error(self) -> None:
        """A proxy in front of DSM answers with a login page under a 200.
        Callers only handle ApiError, so a bare ValueError must not escape."""
        import httpx

        from surveillance.api.client import ApiError, _json_or_raise

        resp = httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>Login</body></html>",
            request=httpx.Request("GET", "https://nas:5001/webapi/entry.cgi"),
        )
        with pytest.raises(ApiError) as excinfo:
            _json_or_raise(resp)
        assert excinfo.value.code == 119, "must be a session error so request() retries once"

    def test_json_body_is_returned(self) -> None:
        import httpx

        from surveillance.api.client import _json_or_raise

        resp = httpx.Response(
            200,
            json={"success": True, "data": {"ok": 1}},
            request=httpx.Request("GET", "https://nas:5001/webapi/entry.cgi"),
        )
        assert _json_or_raise(resp) == {"success": True, "data": {"ok": 1}}


class TestErrorNamespaces:
    def test_auth_and_surveillance_codes_differ(self) -> None:
        from surveillance.api.client import ApiError, _error_table

        auth = _error_table("SYNO.API.Auth")
        svs = _error_table("SYNO.SurveillanceStation.Camera")
        assert ApiError(400, table=auth).message == "No such account or incorrect password"
        assert ApiError(400, table=svs).message == "Execution failed"
        assert ApiError(407, table=auth).message.startswith("Account blocked")
        assert ApiError(407, table=svs).message == "CMS closed"

    def test_password_and_lock_codes_are_named(self) -> None:
        """408-411 per the DSM login and Surveillance Station guides."""
        from surveillance.api.client import AUTH_ERRORS, ApiError

        for code in (408, 409, 410, 411):
            assert "Unknown" not in ApiError(code, table=AUTH_ERRORS).message

    def test_shared_codes_agree(self) -> None:
        from surveillance.api.client import AUTH_ERRORS, COMMON_ERRORS, ERRORS

        for code, text in COMMON_ERRORS.items():
            assert AUTH_ERRORS[code] == text
            assert ERRORS[code] == text

    def test_server_message_overrides_table(self) -> None:
        from surveillance.api.client import ApiError

        assert ApiError(400, "Camera is offline").message == "Camera is offline"
