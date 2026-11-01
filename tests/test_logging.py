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

"""Tests for debug-log credential redaction and --log-file handling."""

from __future__ import annotations

import ast
import io
import logging
from pathlib import Path

import pytest

from surveillance import logfile
from surveillance.__main__ import _LOG_FORMAT, _RedactFormatter
from surveillance.logfile import _clean_completed, mark_complete, parse_arg


def _emit(msg: str, *args: object, exc: BaseException | None = None) -> str:
    """Log through a module logger the way the application does, return output."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(_RedactFormatter(_LOG_FORMAT))
    root = logging.getLogger()
    old_handlers, old_level = root.handlers[:], root.level
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    try:
        log = logging.getLogger("surveillance.api.client")
        if exc is not None:
            try:
                raise exc
            except type(exc):
                log.exception(msg, *args)
        else:
            log.debug(msg, *args)
    finally:
        root.handlers, root.level = old_handlers, old_level
    return stream.getvalue()


class TestRedaction:
    def test_module_logger_is_redacted(self) -> None:
        """The application never logs through the root logger itself."""
        out = _emit("request %s", "https://nas/webapi/entry.cgi?passwd=hunter2&_sid=ABCDEF")
        assert "hunter2" not in out
        assert "ABCDEF" not in out
        assert "passwd=***" in out
        assert "_sid=***" in out

    def test_login_parameters(self) -> None:
        out = _emit("params: account=admin&passwd=s3cret&otp_code=123456&device_id=tok")
        for secret in ("admin", "s3cret", "123456", "tok"):
            assert secret not in out
        assert out.count("=***") == 4

    def test_stream_url_credentials(self) -> None:
        out = _emit("Starting stream: %s", "rtsp://admin:letmein@192.168.1.50:554/h265")
        assert "admin" not in out
        assert "letmein" not in out
        assert "rtsp://***@192.168.1.50:554/h265" in out

    def test_a_password_containing_at_signs(self) -> None:
        """The pattern used to stop at the first "@", logging the rest of
        a password that has one. ffmpeg and mpv accept it unescaped."""
        out = _emit("Starting stream: %s", "rtsp://admin:S3c@ret@2024@192.168.1.50:554/main")
        assert "S3c" not in out and "ret" not in out and "2024" not in out
        assert "rtsp://***@192.168.1.50:554/main" in out

    def test_an_at_sign_in_the_path_is_not_userinfo(self) -> None:
        out = _emit("Opening %s", "rtsp://192.168.1.50/live@main")
        assert "rtsp://192.168.1.50/live@main" in out

    def test_stream_keys(self) -> None:
        """The RTSP over HTTP and MJPEG paths DSM hands out carry the
        stream key, which is the password in its RTSP paths too."""
        key = "faaa8c5e0d20b71038e35e7e3635b0ee"
        out = _emit(
            "Starting stream: %s %s",
            f"rtsp://nas:5000/webman/3rdparty/SurveillanceStation/cgi/rtsp.cgi?Sms=14.unicast&DsId=0&StmKey={key}",
            f'http://nas:5000/webapi/entry.cgi?format=mjpeg&cameraId=14&StmKey="{key}"',
        )
        assert key not in out
        assert "DsId=0&StmKey=***" in out
        assert 'cameraId=14&StmKey="***"' in out

    def test_traceback_is_redacted(self) -> None:
        out = _emit("stream failed", exc=ValueError("GET https://nas/x.cgi?_sid=LEAKED failed"))
        assert "LEAKED" not in out
        assert "Traceback" in out

    def test_ordinary_fields_survive(self) -> None:
        out = _emit("query: api=SYNO.SurveillanceStation.Camera&cameraId=5&version=9")
        assert "api=SYNO.SurveillanceStation.Camera" in out
        assert "cameraId=5" in out

    def test_plain_url_keeps_host(self) -> None:
        out = _emit("connecting to %s", "wss://nas:5001/webman/3rdparty/x?api=y")
        assert "wss://nas:5001/webman/3rdparty/x?api=y" in out


class TestParseLogFileArg:
    def test_absent_is_none(self) -> None:
        value, argv = parse_arg(["surveillance", "--debug"])
        assert value is None
        assert argv == ["surveillance", "--debug"]

    def test_bare_is_empty_string(self) -> None:
        value, argv = parse_arg(["surveillance", "--log-file", "--debug"])
        assert value == ""
        assert argv == ["surveillance", "--debug"]

    def test_with_path(self) -> None:
        value, argv = parse_arg(["surveillance", "--log-file=/var/log/run.log"])
        assert value == "/var/log/run.log"
        assert argv == ["surveillance"]

    def test_repeated_last_one_wins_and_all_removed(self) -> None:
        value, argv = parse_arg(
            ["surveillance", "--log-file=/var/log/a.log", "--log-file=/var/log/b.log"]
        )
        assert value == "/var/log/b.log"
        assert argv == ["surveillance"]

    def test_other_args_untouched_and_ordered(self) -> None:
        value, argv = parse_arg(["surveillance", "--debug", "--log-file", "--help-all"])
        assert value == ""
        assert argv == ["surveillance", "--debug", "--help-all"]

    def test_a_path_after_a_space(self) -> None:
        """The app takes no other arguments, so the word after a bare
        --log-file is its path. Left in, Gio read it as a file to open
        and exited with an error that never named the flag."""
        value, argv = parse_arg(["surveillance", "--log-file", "run.log", "--debug"])
        assert value == "run.log"
        assert argv == ["surveillance", "--debug"]

    def test_bare_at_the_end(self) -> None:
        value, argv = parse_arg(["surveillance", "--debug", "--log-file"])
        assert value == ""
        assert argv == ["surveillance", "--debug"]


class TestCleanCompletedLogs:
    def test_removes_log_and_sentinel_for_completed_session(self, tmp_path: Path) -> None:
        log = tmp_path / "debug-20260101T000000.log"
        sentinel = tmp_path / "debug-20260101T000000.log.complete"
        log.write_text("some log content")
        sentinel.touch()

        _clean_completed(tmp_path)

        assert not log.exists()
        assert not sentinel.exists()

    def test_keeps_log_with_no_sentinel(self, tmp_path: Path) -> None:
        log = tmp_path / "debug-20260101T000000.log"
        log.write_text("a crash log, never marked complete")

        _clean_completed(tmp_path)

        assert log.exists()

    def test_ignores_unrelated_files(self, tmp_path: Path) -> None:
        other = tmp_path / "notes.txt"
        other.write_text("unrelated")

        _clean_completed(tmp_path)

        assert other.exists()

    def test_empty_directory_is_a_no_op(self, tmp_path: Path) -> None:
        _clean_completed(tmp_path)  # must not raise


class TestMarkLogComplete:
    def test_touches_the_configured_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sentinel = tmp_path / "debug-20260101T000000.log.complete"
        monkeypatch.setattr(logfile, "_complete_path", sentinel)

        mark_complete()

        assert sentinel.exists()

    def test_no_op_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(logfile, "_complete_path", None)

        mark_complete()  # must not raise


class TestNoStateInDunderMain:
    """__main__ is the top of the import graph, so nothing may reach into it.

    Under `python -m surveillance` the interpreter runs __main__.py as the
    module __main__ and leaves surveillance.__main__ out of sys.modules, so
    a module that imports surveillance.__main__ gets a second copy with its
    own globals. State shared with the quit paths went stale that way once.
    """

    def test_nothing_imports_surveillance_dunder_main(self) -> None:
        src = Path(__file__).resolve().parent.parent / "src" / "surveillance"
        offenders = []
        for path in src.rglob("*.py"):
            if path.name == "__main__.py":
                continue
            for node in ast.walk(ast.parse(path.read_text())):
                if isinstance(node, ast.ImportFrom):
                    imported = {node.module}
                elif isinstance(node, ast.Import):
                    imported = {alias.name for alias in node.names}
                else:
                    continue
                if "surveillance.__main__" in imported:
                    offenders.append(f"{path.relative_to(src).as_posix()}:{node.lineno}")
        assert offenders == []


class TestExitPaths:
    """Every way out saves the config: the Quit action, closing the
    window, and the signals, which used to skip it and lose a setting
    changed in the last second, SIGHUP having no handler at all."""

    @pytest.mark.parametrize("loaded", [True, False])
    def test_exit_now_saves_only_a_loaded_config(
        self, monkeypatch: pytest.MonkeyPatch, loaded: bool
    ) -> None:
        """Before do_startup reads the real config, self.config is the empty
        default, and saving it replaced every profile: Gio returns from run()
        without starting up on an unknown option such as --version, or when
        it hands a second launch over to the running instance."""
        import os
        from types import SimpleNamespace

        import surveillance.config
        from surveillance.app import SurveillanceApp

        order: list[str] = []
        monkeypatch.setattr(surveillance.config, "save_config_now", lambda c: order.append("save"))
        monkeypatch.setattr(logfile, "mark_complete", lambda: order.append("complete"))

        def _exit(code: int) -> None:
            order.append(f"exit {code}")
            raise SystemExit(code)

        monkeypatch.setattr(os, "_exit", _exit)
        app = SimpleNamespace(config=object(), _config_loaded=loaded)
        with pytest.raises(SystemExit):
            SurveillanceApp.exit_now(app)  # type: ignore[arg-type]
        expected = ["save", "complete", "exit 0"] if loaded else ["complete", "exit 0"]
        assert order == expected

    @pytest.mark.parametrize("name", ["SIGHUP", "SIGTERM", "SIGINT"])
    def test_each_signal_reaches_the_app(self, tmp_path: Path, name: str) -> None:
        import os
        import subprocess
        import sys

        marker = tmp_path / "exited"
        child = (
            "import os, signal, time\n"
            "from surveillance.__main__ import _install_exit_signals\n"
            "class App:\n"
            "    def exit_now(self):\n"
            f"        open({str(marker)!r}, 'w').write('saved')\n"
            "        os._exit(0)\n"
            "_install_exit_signals([App()])\n"
            f"os.kill(os.getpid(), signal.{name})\n"
            "time.sleep(5)\n"
            "os._exit(3)\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run(  # noqa: S603 (this interpreter, a fixed script)
            [sys.executable, "-c", child], env=env, timeout=30, check=False
        )
        assert result.returncode == 0
        assert marker.read_text() == "saved"

    def test_a_signal_started_ignored_stays_ignored(self, tmp_path: Path) -> None:
        """nohup starts the app with SIGHUP ignored so it outlives the
        terminal; installing a handler over that made it exit anyway."""
        import os
        import subprocess
        import sys

        marker = tmp_path / "exited"
        child = (
            "import os, signal, time\n"
            "signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
            "from surveillance.__main__ import _install_exit_signals\n"
            "class App:\n"
            "    def exit_now(self):\n"
            f"        open({str(marker)!r}, 'w').write('exited')\n"
            "        os._exit(0)\n"
            "_install_exit_signals([App()])\n"
            "os.kill(os.getpid(), signal.SIGHUP)\n"
            "time.sleep(0.5)\n"
            "os._exit(3)\n"
        )
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        result = subprocess.run(  # noqa: S603 (this interpreter, a fixed script)
            [sys.executable, "-c", child], env=env, timeout=30, check=False
        )
        assert result.returncode == 3, "still running after the hangup"
        assert not marker.exists()
