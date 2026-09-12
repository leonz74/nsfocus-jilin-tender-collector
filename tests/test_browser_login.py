from __future__ import annotations

import base64
import copy
import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tender_downloader.webui import browser_login as browser_login_module
from tender_downloader.webui.browser_login import (
    BrowserHandle,
    BrowserLoginBusyError,
    BrowserLoginError,
    BrowserLoginManager,
    PENDING_CLEANUP_MARKER,
    PERSISTENT_PROFILE_MARKER,
    PROFILE_PREFIX,
    _PAGE_LOGIN_STATE_EXPRESSION,
    _WINDOWS_PROFILE_ARGUMENT_PATTERN,
    _read_debug_endpoint,
    _remove_profile_after_browser_exit,
    _resolve_tool_profile,
    _safe_remove_profile,
    _stop_process,
    _terminate_windows_profile_processes,
    focus_browser_page,
    probe_browser_page,
)
from tender_downloader.webui.server import (
    ProcessManager,
    SOURCE_SESSIONS_ENV,
    create_server,
)


class FakeProcess:
    def __init__(self) -> None:
        self.return_code: int | None = None
        self.terminated = False

    def poll(self) -> int | None:
        return self.return_code

    def terminate(self) -> None:
        self.terminated = True
        self.return_code = 0

    def wait(self, timeout: float | None = None) -> int:
        del timeout
        return self.return_code or 0

    def kill(self) -> None:
        self.return_code = -9


class LoopbackCDPBrowser:
    """Small browser-level CDP stand-in that uses a real loopback WebSocket."""

    def __init__(self) -> None:
        self.path = "/devtools/browser/loopback-test"
        self.close_requested = threading.Event()
        self._stopped = threading.Event()
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self.port = int(self._listener.getsockname()[1])
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def websocket_url(self) -> str:
        return f"ws://127.0.0.1:{self.port}{self.path}"

    def write_active_port(self, profile_dir: Path) -> None:
        (profile_dir / "DevToolsActivePort").write_text(
            f"{self.port}\n{self.path}\n",
            encoding="utf-8",
        )

    @staticmethod
    def _receive_exact(connection: socket.socket, length: int) -> bytes:
        result = bytearray()
        while len(result) < length:
            chunk = connection.recv(length - len(result))
            if not chunk:
                raise ConnectionError("client disconnected")
            result.extend(chunk)
        return bytes(result)

    @classmethod
    def _receive_json(cls, connection: socket.socket) -> dict[str, object]:
        first, second = cls._receive_exact(connection, 2)
        del first
        length = second & 0x7F
        if length == 126:
            length = int.from_bytes(cls._receive_exact(connection, 2), "big")
        elif length == 127:
            length = int.from_bytes(cls._receive_exact(connection, 8), "big")
        mask = cls._receive_exact(connection, 4) if second & 0x80 else b""
        payload = cls._receive_exact(connection, length)
        if mask:
            payload = bytes(
                value ^ mask[index % 4] for index, value in enumerate(payload)
            )
        decoded = json.loads(payload.decode("utf-8"))
        if not isinstance(decoded, dict):
            raise ValueError("CDP command is not an object")
        return decoded

    @staticmethod
    def _send_json(connection: socket.socket, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        if len(encoded) < 126:
            header = bytes((0x81, len(encoded)))
        elif len(encoded) <= 0xFFFF:
            header = bytes((0x81, 126)) + len(encoded).to_bytes(2, "big")
        else:
            header = bytes((0x81, 127)) + len(encoded).to_bytes(8, "big")
        connection.sendall(header + encoded)

    def _handle(self, connection: socket.socket) -> None:
        request = bytearray()
        while b"\r\n\r\n" not in request:
            chunk = connection.recv(4096)
            if not chunk:
                return
            request.extend(chunk)
        headers: dict[str, str] = {}
        for line in request.decode("iso-8859-1").split("\r\n")[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        key = headers["sec-websocket-key"]
        accepted = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        connection.sendall(
            (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accepted}\r\n\r\n"
            ).encode("ascii")
        )
        command = self._receive_json(connection)
        method = command.get("method")
        result: dict[str, object] = {}
        if method == "Storage.getCookies":
            result["cookies"] = [
                {
                    "name": "SESSION",
                    "value": "loopback-browser-cookie",
                    "domain": ".example.com",
                    "path": "/",
                    "secure": True,
                    "httpOnly": True,
                }
            ]
        self._send_json(connection, {"id": command.get("id"), "result": result})
        if method == "Browser.close":
            self.close_requested.set()
            self._stop_listener()

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._listener.accept()
            except (OSError, socket.timeout):
                continue
            try:
                connection.settimeout(1)
                self._handle(connection)
            except (ConnectionError, KeyError, OSError, ValueError):
                pass
            finally:
                connection.close()

    def _stop_listener(self) -> None:
        self._stopped.set()
        try:
            self._listener.close()
        except OSError:
            pass

    def close(self) -> None:
        self._stop_listener()
        self.thread.join(timeout=2)


class BrowserLoginManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_data = tempfile.TemporaryDirectory()
        self.local_app_data = patch.dict(
            os.environ,
            {"LOCALAPPDATA": self.profile_data.name},
        )
        self.local_app_data.start()
        self.processes: list[FakeProcess] = []
        self.profile_dirs: list[Path] = []
        self.launch_urls: list[str] = []

        def launcher(url: str, profile_dir: Path) -> BrowserHandle:
            self.assertTrue(url.startswith("https://"))
            self.assertTrue(profile_dir.exists())
            self.launch_urls.append(url)
            self.profile_dirs.append(profile_dir)
            process = FakeProcess()
            self.processes.append(process)
            return BrowserHandle(process, profile_dir, "ws://127.0.0.1:12345/devtools/browser/test")

        self.launcher = launcher

    def tearDown(self) -> None:
        self.local_app_data.stop()
        self.profile_data.cleanup()

    @staticmethod
    def cookies(handle: BrowserHandle) -> list[dict[str, object]]:
        del handle
        return [
            {
                "name": "SESSION",
                "value": "captured-cookie-secret-5ad1",
                "domain": ".example.com",
                "path": "/",
                "secure": True,
                "httpOnly": True,
                "expires": 1_900_000_000,
                "sameSite": "Lax",
            }
        ]

    def test_visual_login_lifecycle_exposes_only_metadata(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            waiting = manager.start("portal", "https://example.com/login")
            process = self.processes[0]
            profile = self.profile_dirs[0]
            self.assertEqual("waiting", waiting["state"])
            self.assertEqual(0, waiting["cookie_count"])

            ready = manager.complete("portal")
            serialized_status = json.dumps(manager.status(), ensure_ascii=False)
            sessions = manager.sessions_for_sources(["portal"])

            self.assertEqual("ready", ready["state"])
            self.assertEqual(1, ready["cookie_count"])
            self.assertNotIn("captured-cookie-secret-5ad1", serialized_status)
            self.assertEqual(
                "captured-cookie-secret-5ad1",
                sessions["portal"]["cookies"][0]["value"],
            )
            self.assertTrue(process.terminated)
            self.assertTrue(profile.exists())

            manager.consume(["portal"])
            self.assertEqual([], manager.status())
            self.assertTrue(profile.exists())
            with self.assertRaisesRegex(BrowserLoginError, "等待登录"):
                manager.sessions_for_sources(["portal"])
        finally:
            manager.close()

    def test_each_source_and_origin_gets_an_isolated_profile(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("one", "https://example.com/login")
            manager.start("two", "https://example.org/login")
            self.assertNotEqual(self.profile_dirs[0], self.profile_dirs[1])
            self.assertRegex(self.profile_dirs[0].name, r"^[0-9a-f]{64}$")
            self.assertTrue((self.profile_dirs[0] / PERSISTENT_PROFILE_MARKER).is_file())
            # The random parent temporary directory can coincidentally contain
            # "one"; only the generated profile name encodes source identity.
            self.assertNotIn("one", self.profile_dirs[0].name)
            self.assertNotIn("example.com", self.profile_dirs[0].name)
            cancelled = manager.cancel("one")
            self.assertEqual("closed", cancelled["state"])
            self.assertTrue(self.processes[0].terminated)
            second = manager.start("two", "https://example.org/login")
            self.assertEqual("waiting", second["state"])
        finally:
            manager.close()

    def test_profile_survives_restart_but_does_not_imply_ready(self) -> None:
        first = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        first.start("portal", "https://example.com/login")
        first_path = self.profile_dirs[-1]
        first.cancel("portal")
        first.close()
        self.assertTrue(first_path.exists())

        second = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            self.assertEqual([], second.status())
            checkpoint = second.checkpoint(
                {"portal": "https://example.com/another-path"}
            )
            self.assertEqual("waiting_for_user", checkpoint["state"])
            self.assertTrue(checkpoint["pending"][0]["profile_saved"])
            with self.assertRaisesRegex(BrowserLoginError, "等待登录"):
                second.sessions_for_sources(["portal"])
            waiting = second.start("portal", "https://example.com/other-path")
            self.assertEqual("waiting", waiting["state"])
            self.assertEqual(first_path, self.profile_dirs[-1])
        finally:
            second.close()

    def test_stable_profile_id_reuses_legacy_path_after_source_id_rename(self) -> None:
        first = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        first.start(
            "legacy-source",
            "https://example.com/login",
            check_url="https://example.com/protected/notices",
        )
        legacy_path = self.profile_dirs[-1]
        (legacy_path / "Default").mkdir()
        first.cancel("legacy-source")
        first.close()

        second = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            checkpoint = second.checkpoint(
                {"renamed-source": "https://example.com/login"},
                profile_ids={"renamed-source": "legacy-source"},
            )
            self.assertTrue(checkpoint["pending"][0]["profile_saved"])

            second.start(
                "renamed-source",
                "https://example.com/login",
                profile_id="legacy-source",
                check_url="https://example.com/protected/notices",
            )
            self.assertEqual(legacy_path, self.profile_dirs[-1])
            self.assertEqual(
                "https://example.com/protected/notices", self.launch_urls[-1]
            )
        finally:
            second.close()

    def test_profile_and_check_url_inputs_are_validated_before_launch(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            with self.assertRaisesRegex(BrowserLoginError, "配置标识无效"):
                manager.start(
                    "portal",
                    "https://example.com/login",
                    profile_id="../escape",
                )
            with self.assertRaisesRegex(BrowserLoginError, "同一来源"):
                manager.start(
                    "portal",
                    "https://example.com/login",
                    check_url="https://other.example/protected",
                )
            self.assertEqual([], self.profile_dirs)
        finally:
            manager.close()

    def test_public_metadata_never_exposes_stable_profile_id(self) -> None:
        canary = "profile_canary_7d29"
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            started = manager.start(
                "portal",
                "https://example.com/login",
                profile_id=canary,
                check_url="https://example.com/protected",
            )
            checkpoint = manager.checkpoint(
                {"portal": "https://example.com/login"},
                profile_ids={"portal": canary},
            )
            serialized = json.dumps(
                [started, manager.status(), checkpoint], ensure_ascii=False
            )
            self.assertNotIn(canary, serialized)
            self.assertNotIn("profile_id", serialized)
        finally:
            manager.close()

    def test_automatic_probe_waits_for_manual_challenge_then_signals_ready(self) -> None:
        page_state = {"value": "challenge"}

        def launcher(url: str, profile_dir: Path) -> BrowserHandle:
            del url
            process = FakeProcess()
            self.processes.append(process)
            self.profile_dirs.append(profile_dir)
            return BrowserHandle(
                process,
                profile_dir,
                "ws://127.0.0.1:12345/devtools/browser/test",
                live_transport=True,
            )

        manager = BrowserLoginManager(
            launcher=launcher,
            cookie_reader=self.cookies,
            page_probe=lambda _handle, _url: page_state["value"],
        )
        try:
            manager.start("portal", "https://example.com/login")
            challenged = manager.status()[0]
            self.assertEqual("challenge_required", challenged["state"])
            self.assertTrue(challenged["requires_user_action"])
            self.assertFalse(manager.wait_for_sources(["portal"], timeout=0))

            page_state["value"] = "content"
            ready = manager.status()[0]
            self.assertEqual("ready", ready["state"])
            self.assertTrue(manager.wait_for_sources(["portal"], timeout=0.05))
            checkpoint = manager.checkpoint(
                {"portal": "https://example.com/notices"}
            )
            self.assertEqual("ready", checkpoint["state"])
            self.assertEqual(["portal"], checkpoint["ready"])

            page_state["value"] = "challenge"
            with self.assertRaisesRegex(BrowserLoginError, "等待登录或验证码"):
                manager.sessions_for_sources(["portal"])
            self.assertEqual("challenge_required", manager.status()[0]["state"])
        finally:
            manager.close()

    def test_page_probe_checks_title_for_graphical_challenge_pages(self) -> None:
        self.assertIn("document.title", _PAGE_LOGIN_STATE_EXPRESSION)
        self.assertIn("${titleText}\\n${bodyText}", _PAGE_LOGIN_STATE_EXPRESSION)
        self.assertIn("验证码", _PAGE_LOGIN_STATE_EXPRESSION)

    def test_page_probe_uses_activity_ranking_when_exact_check_page_is_absent(self) -> None:
        handle = BrowserHandle(
            FakeProcess(),
            Path(self.profile_data.name),
            "ws://127.0.0.1:12345/devtools/browser/test",
            live_transport=True,
        )

        class DummySocket:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def close(self) -> None:
                pass

        target_sessions = {"old": "session-old", "current": "session-current"}

        def command(
            _socket: object,
            _command_id: int,
            method: str,
            *,
            params: dict[str, object] | None = None,
            session_id: str | None = None,
        ) -> dict[str, object]:
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {"type": "page", "targetId": "old", "url": "https://example.com/login"},
                        {"type": "page", "targetId": "current", "url": "https://example.com/home"},
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": target_sessions[str(params["targetId"])]}
            if method == "Runtime.evaluate":
                value = (
                    {"challenge": True, "login": False, "visible": False, "focused": False, "ready": True}
                    if session_id == "session-old"
                    else {"challenge": False, "login": False, "visible": True, "focused": True, "ready": True}
                )
                return {"result": {"value": value}}
            if method == "Target.detachFromTarget":
                return {}
            raise AssertionError(method)

        with (
            patch("tender_downloader.webui.browser_login._WebSocket", DummySocket),
            patch("tender_downloader.webui.browser_login._cdp_command", side_effect=command),
        ):
            self.assertEqual(
                "content",
                probe_browser_page(handle, "https://example.com/protected"),
            )

    def test_page_probe_exact_challenge_beats_focused_same_origin_content(self) -> None:
        handle = BrowserHandle(
            FakeProcess(),
            Path(self.profile_data.name),
            "ws://127.0.0.1:12345/devtools/browser/test",
            live_transport=True,
        )

        class DummySocket:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def close(self) -> None:
                pass

        target_sessions = {"check": "session-check", "home": "session-home"}

        def command(
            _socket: object,
            _command_id: int,
            method: str,
            *,
            params: dict[str, object] | None = None,
            session_id: str | None = None,
        ) -> dict[str, object]:
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "check",
                            "url": "https://example.com/protected#captcha",
                        },
                        {
                            "type": "page",
                            "targetId": "home",
                            "url": "https://example.com/home",
                        },
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": target_sessions[str(params["targetId"])]}
            if method == "Runtime.evaluate":
                value = (
                    {
                        "challenge": True,
                        "login": False,
                        "visible": False,
                        "focused": False,
                        "ready": True,
                    }
                    if session_id == "session-check"
                    else {
                        "challenge": False,
                        "login": False,
                        "visible": True,
                        "focused": True,
                        "ready": True,
                    }
                )
                return {"result": {"value": value}}
            if method == "Target.detachFromTarget":
                return {}
            raise AssertionError(method)

        with (
            patch("tender_downloader.webui.browser_login._WebSocket", DummySocket),
            patch(
                "tender_downloader.webui.browser_login._cdp_command",
                side_effect=command,
            ),
        ):
            self.assertEqual(
                "challenge",
                probe_browser_page(handle, "https://example.com/protected"),
            )

    def test_page_probe_exact_content_beats_focused_same_origin_challenge(self) -> None:
        handle = BrowserHandle(
            FakeProcess(),
            Path(self.profile_data.name),
            "ws://127.0.0.1:12345/devtools/browser/test",
            live_transport=True,
        )

        class DummySocket:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def close(self) -> None:
                pass

        target_sessions = {"check": "session-check", "login": "session-login"}

        def command(
            _socket: object,
            _command_id: int,
            method: str,
            *,
            params: dict[str, object] | None = None,
            session_id: str | None = None,
        ) -> dict[str, object]:
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "check",
                            "url": "https://example.com/protected?scope=all",
                        },
                        {
                            "type": "page",
                            "targetId": "login",
                            "url": "https://example.com/login",
                        },
                    ]
                }
            if method == "Target.attachToTarget":
                return {"sessionId": target_sessions[str(params["targetId"])]}
            if method == "Runtime.evaluate":
                value = (
                    {
                        "challenge": False,
                        "login": False,
                        "visible": False,
                        "focused": False,
                        "ready": True,
                    }
                    if session_id == "session-check"
                    else {
                        "challenge": True,
                        "login": False,
                        "visible": True,
                        "focused": True,
                        "ready": True,
                    }
                )
                return {"result": {"value": value}}
            if method == "Target.detachFromTarget":
                return {}
            raise AssertionError(method)

        with (
            patch("tender_downloader.webui.browser_login._WebSocket", DummySocket),
            patch(
                "tender_downloader.webui.browser_login._cdp_command",
                side_effect=command,
            ),
        ):
            self.assertEqual(
                "content",
                probe_browser_page(
                    handle,
                    "https://example.com/protected?scope=all#ignored",
                ),
            )

    def test_focus_activates_existing_sso_page_without_navigating(self) -> None:
        handle = BrowserHandle(
            FakeProcess(),
            Path(self.profile_data.name),
            "ws://127.0.0.1:12345/devtools/browser/test",
            live_transport=True,
        )
        methods: list[str] = []

        class DummySocket:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            def close(self) -> None:
                pass

        def command(
            _socket: object,
            _command_id: int,
            method: str,
            *,
            params: dict[str, object] | None = None,
            session_id: str | None = None,
        ) -> dict[str, object]:
            del session_id
            methods.append(method)
            if method == "Target.getTargets":
                return {
                    "targetInfos": [
                        {
                            "type": "page",
                            "targetId": "sso-page",
                            "url": "https://identity.example.net/challenge",
                        }
                    ]
                }
            if method == "Target.activateTarget":
                self.assertEqual("sso-page", params["targetId"])
                return {}
            raise AssertionError(method)

        with (
            patch("tender_downloader.webui.browser_login._WebSocket", DummySocket),
            patch("tender_downloader.webui.browser_login._cdp_command", side_effect=command),
        ):
            self.assertTrue(
                focus_browser_page(handle, "https://example.com/login")
            )
        self.assertEqual(
            ["Target.getTargets", "Target.activateTarget"], methods
        )

    def test_explicit_clear_deletes_only_exact_source_origin_profile(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("one", "https://example.com/login")
            first_path = self.profile_dirs[-1]
            manager.start("two", "https://example.org/login")
            second_path = self.profile_dirs[-1]
            manager.cancel("one")
            manager.cancel("two")

            cleared = manager.clear("one", "https://example.com/another-path")
            self.assertEqual("cleared", cleared["state"])
            self.assertFalse(cleared["profile_saved"])
            self.assertFalse(first_path.exists())
            self.assertTrue(second_path.exists())
            self.assertTrue(second_path.parent.exists())
            self.assertIsNone(
                _resolve_tool_profile(second_path.parent, persistent_root=second_path.parent)
            )
        finally:
            manager.close()

    def test_orphan_cleanup_reaches_changed_or_deleted_sources_only(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("old-source", "https://old.example.com/login")
            old_profile = self.profile_dirs[-1]
            manager.cancel("old-source")
            manager.start("kept-source", "https://kept.example.com/login")
            kept_profile = self.profile_dirs[-1]
            manager.cancel("kept-source")

            markerless = kept_profile.parent / ("f" * 64)
            markerless.mkdir()
            (markerless / "must-remain.txt").write_text("not owned", encoding="utf-8")
            expected = {"kept-source": "https://kept.example.com/another-path"}

            self.assertEqual(
                {"orphaned_profile_count": 1},
                manager.orphaned_profile_summary(expected),
            )
            with patch(
                "tender_downloader.webui.browser_login._remove_profile_after_browser_exit",
                side_effect=lambda path, persistent_root=None: _safe_remove_profile(
                    path, persistent_root=persistent_root
                ),
            ):
                result = manager.clear_orphaned_profiles(expected)

            self.assertEqual(1, result["removed_count"])
            self.assertEqual(0, result["failed_count"])
            self.assertEqual(0, result["orphaned_profile_count"])
            self.assertFalse(old_profile.exists())
            self.assertTrue(kept_profile.exists())
            self.assertTrue(markerless.is_dir())
            serialized = json.dumps(result, ensure_ascii=False)
            self.assertNotIn(str(old_profile), serialized)
            self.assertNotIn("old.example.com", serialized)
            self.assertNotIn("devtools/browser", serialized)
        finally:
            manager.close()

    def test_orphan_cleanup_refuses_a_profile_leased_to_a_run(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("removed-source", "https://example.com/login")
            manager.complete("removed-source")
            manager.lease_for_sources(["removed-source"], lease_token="lease-test")

            with self.assertRaisesRegex(BrowserLoginBusyError, "采集任务正在使用"):
                manager.clear_orphaned_profiles({})
        finally:
            manager.close()

    def test_public_status_never_exposes_profile_or_cdp_capability(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("portal", "https://example.com/login")
            serialized = json.dumps(manager.status(), ensure_ascii=False)
            self.assertNotIn("devtools/browser", serialized)
            self.assertNotIn(str(self.profile_dirs[-1]), serialized)
            self.assertNotIn("captured-cookie-secret", serialized)
        finally:
            manager.close()

    def test_launcher_cannot_substitute_profile_or_remote_cdp_capability(self) -> None:
        processes: list[FakeProcess] = []

        def wrong_profile(_url: str, profile: Path) -> BrowserHandle:
            process = FakeProcess()
            processes.append(process)
            return BrowserHandle(
                process,
                profile.parent / "unrelated-profile",
                "ws://127.0.0.1:12345/devtools/browser/test",
            )

        manager = BrowserLoginManager(
            launcher=wrong_profile,
            cookie_reader=self.cookies,
        )
        try:
            with self.assertRaisesRegex(BrowserLoginError, "无效的本机浏览器句柄"):
                manager.start("portal", "https://example.com/login")
            self.assertTrue(processes[0].terminated)
        finally:
            manager.close()

        def remote_capability(_url: str, profile: Path) -> BrowserHandle:
            process = FakeProcess()
            processes.append(process)
            return BrowserHandle(
                process,
                profile,
                "ws://example.com:9222/devtools/browser/remote",
            )

        manager = BrowserLoginManager(
            launcher=remote_capability,
            cookie_reader=self.cookies,
        )
        try:
            with self.assertRaisesRegex(BrowserLoginError, "无效的本机浏览器句柄"):
                manager.start("portal-2", "https://example.com/login")
            self.assertTrue(processes[-1].terminated)
        finally:
            manager.close()

    def test_complete_requires_at_least_one_cookie(self) -> None:
        manager = BrowserLoginManager(
            launcher=self.launcher,
            cookie_reader=lambda handle: [],
        )
        try:
            manager.start("portal", "https://example.com/login")
            with self.assertRaisesRegex(BrowserLoginError, "没有捕获"):
                manager.complete("portal")
            status = manager.status()[0]
            self.assertEqual("failed", status["state"])
            self.assertEqual(0, status["cookie_count"])
        finally:
            manager.close()

    def test_unrelated_sso_and_tracker_cookies_are_discarded_before_limits(self) -> None:
        unrelated = [
            {
                "name": f"TRACKER_{index}",
                "value": "x" * 100,
                "domain": "tracker.invalid",
                "path": "/",
            }
            for index in range(600)
        ]
        manager = BrowserLoginManager(
            launcher=self.launcher,
            cookie_reader=lambda handle: unrelated + self.cookies(handle),
        )
        try:
            manager.start("portal", "https://example.com/login")
            ready = manager.complete("portal")
            session = manager.sessions_for_sources(["portal"])

            self.assertEqual(1, ready["cookie_count"])
            self.assertEqual("SESSION", session["portal"]["cookies"][0]["name"])
        finally:
            manager.close()

    def test_captured_session_is_bound_to_configured_login_origin(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("portal", "https://example.com/login")
            manager.complete("portal")

            with self.assertRaisesRegex(BrowserLoginError, "地址已改变"):
                manager.sessions_for_sources(
                    ["portal"],
                    expected_login_urls={"portal": "https://other.example/login"},
                )
        finally:
            manager.close()

    def test_profile_cleanup_retries_after_temporary_windows_lock(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX)).resolve()
        original_rmtree = shutil.rmtree
        attempts = 0

        def flaky_rmtree(path: Path) -> None:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise PermissionError("simulated browser lock")
            original_rmtree(path)

        with patch(
            "tender_downloader.webui.browser_login.shutil.rmtree",
            side_effect=flaky_rmtree,
        ):
            self.assertTrue(_safe_remove_profile(profile))

        self.assertGreaterEqual(attempts, 2)
        self.assertFalse(profile.exists())

    def test_windows_cleanup_removes_profile_recreated_after_first_delete(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX)).resolve()
        removals = 0

        def recreate_once(path: Path) -> bool:
            nonlocal removals
            removals += 1
            shutil.rmtree(path, ignore_errors=True)
            if removals == 1:
                path.mkdir()
                (path / "late-edge-write").write_text("late", encoding="ascii")
            return True

        try:
            with (
                patch("tender_downloader.webui.browser_login.os.name", "nt"),
                patch("tender_downloader.webui.browser_login._resolve_tool_profile", return_value=profile),
                patch(
                    "tender_downloader.webui.browser_login._terminate_windows_profile_processes",
                    return_value=True,
                ) as terminate_profile,
                patch(
                    "tender_downloader.webui.browser_login._safe_remove_profile",
                    side_effect=recreate_once,
                ),
                patch("tender_downloader.webui.browser_login.time.sleep"),
            ):
                self.assertTrue(_remove_profile_after_browser_exit(profile))

            self.assertEqual(2, removals)
            self.assertGreaterEqual(terminate_profile.call_count, 3)
            self.assertFalse(profile.exists())
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def test_windows_cleanup_never_deletes_before_exact_profile_scan_succeeds(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX)).resolve()
        try:
            with (
                patch("tender_downloader.webui.browser_login.os.name", "nt"),
                patch("tender_downloader.webui.browser_login._resolve_tool_profile", return_value=profile),
                patch(
                    "tender_downloader.webui.browser_login._terminate_windows_profile_processes",
                    return_value=False,
                ),
                patch(
                    "tender_downloader.webui.browser_login._safe_remove_profile"
                ) as remove_profile,
            ):
                self.assertFalse(_remove_profile_after_browser_exit(profile))

            remove_profile.assert_not_called()
            self.assertTrue(profile.exists())
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def test_profile_guard_rejects_non_tool_and_unsuffixed_directories(self) -> None:
        temporary_root = Path(tempfile.gettempdir()).resolve()

        self.assertIsNone(_resolve_tool_profile(temporary_root / "unrelated-profile"))
        self.assertIsNone(_resolve_tool_profile(temporary_root / PROFILE_PREFIX))
        self.assertIsNone(_resolve_tool_profile(temporary_root.parent / f"{PROFILE_PREFIX}x"))

        persistent_root = Path(self.profile_data.name) / "profiles" / "v1"
        persistent_root.mkdir(parents=True)
        unmarked = persistent_root / ("a" * 64)
        unmarked.mkdir()
        (unmarked / "unrelated.txt").write_text("keep", encoding="ascii")
        self.assertIsNone(
            _resolve_tool_profile(unmarked, persistent_root=persistent_root)
        )
        self.assertFalse(
            _safe_remove_profile(unmarked, persistent_root=persistent_root)
        )
        self.assertTrue((unmarked / "unrelated.txt").is_file())

    def test_windows_fallback_passes_exact_profile_only_through_environment(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX)).resolve()
        fake_powershell = profile.parent / "powershell.exe"
        completed = subprocess.CompletedProcess([], 0)
        try:
            with (
                patch("tender_downloader.webui.browser_login.os.name", "nt"),
                patch(
                    "tender_downloader.webui.browser_login._resolve_tool_profile",
                    return_value=profile,
                ),
                patch(
                    "tender_downloader.webui.browser_login._windows_powershell_executable",
                    return_value=fake_powershell,
                ),
                patch(
                    "tender_downloader.webui.browser_login.subprocess.run",
                    return_value=completed,
                ) as run,
            ):
                self.assertTrue(_terminate_windows_profile_processes(profile))

            positional, keyword = run.call_args
            command = positional[0]
            self.assertNotIn(str(profile), command)
            self.assertEqual(str(profile), keyword["env"]["TENDER_BROWSER_PROFILE_PATH"])
            self.assertFalse(keyword["shell"])
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def test_windows_profile_argument_match_is_bounded_and_supports_spaces(self) -> None:
        target = r"C:\Users\Jane Doe\Temp\tender-browser-login-abc"

        def captured(command_line: str) -> str | None:
            match = re.search(_WINDOWS_PROFILE_ARGUMENT_PATTERN, command_line)
            if match is None:
                return None
            return next((value for value in match.groups() if value is not None), None)

        self.assertEqual(
            target,
            captured(f'msedge.exe "--user-data-dir={target}" --new-window'),
        )
        self.assertEqual(
            target,
            captured(f'msedge.exe --user-data-dir="{target}" --new-window'),
        )
        self.assertEqual(
            target,
            captured(f'msedge.exe --user-data-dir "{target}" --new-window'),
        )
        self.assertIsNone(
            captured(f'msedge.exe --user-data-dir="{target}"suffix --new-window')
        )
        self.assertEqual(
            f"{target}-ordinary",
            captured(f'msedge.exe "--user-data-dir={target}-ordinary" --new-window'),
        )

    def test_exited_launcher_uses_exact_profile_fallback_when_cdp_close_fails(self) -> None:
        process = FakeProcess()
        process.return_code = 0
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        websocket_url = "ws://127.0.0.1:12345/devtools/browser/test"
        try:
            with (
                patch("tender_downloader.webui.browser_login.os.name", "nt"),
                patch(
                    "tender_downloader.webui.browser_login._close_browser_endpoint",
                    return_value=False,
                ),
                patch(
                    "tender_downloader.webui.browser_login._terminate_windows_profile_processes",
                    return_value=True,
                ) as terminate_profile,
                patch(
                    "tender_downloader.webui.browser_login._cdp_endpoint_alive",
                    return_value=False,
                ),
            ):
                self.assertTrue(_stop_process(process, websocket_url, profile))

            terminate_profile.assert_called_once_with(profile)
        finally:
            shutil.rmtree(profile, ignore_errors=True)

    def test_ordinary_close_never_deletes_persistent_profile(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        handle = BrowserHandle(
            FakeProcess(),
            profile,
            "ws://127.0.0.1:12345/devtools/browser/test",
        )
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            with (
                patch("tender_downloader.webui.browser_login.os.name", "nt"),
                patch(
                    "tender_downloader.webui.browser_login._stop_process",
                    return_value=True,
                ),
                patch.object(manager, "_remove_profile") as remove_profile,
                patch(
                    "tender_downloader.webui.browser_login._terminate_windows_profile_processes",
                    return_value=True,
                ) as terminate_profile,
            ):
                self.assertTrue(manager._close_handle(handle))

            remove_profile.assert_not_called()
            terminate_profile.assert_not_called()
        finally:
            manager.close()
            shutil.rmtree(profile, ignore_errors=True)

    def test_next_manager_cleans_only_profiles_marked_pending(self) -> None:
        profile = Path(tempfile.mkdtemp(prefix=PROFILE_PREFIX))
        (profile / PENDING_CLEANUP_MARKER).write_text("", encoding="ascii")

        manager = BrowserLoginManager(
            launcher=self.launcher,
            cookie_reader=self.cookies,
        )
        try:
            self.assertFalse(profile.exists())
        finally:
            manager.close()

    def test_closed_window_keeps_profile_and_changes_session_to_closed(self) -> None:
        manager = BrowserLoginManager(launcher=self.launcher, cookie_reader=self.cookies)
        try:
            manager.start("portal", "https://example.com/login")
            self.processes[0].return_code = 0
            status = manager.status()[0]
            self.assertEqual("closed", status["state"])
            self.assertIn("已关闭", status["error"])
        finally:
            manager.close()

    def test_exited_windows_launcher_uses_live_cdp_until_browser_close(self) -> None:
        browser = LoopbackCDPBrowser()
        launcher_process = FakeProcess()
        launcher_process.return_code = 0
        profile: Path | None = None

        def launcher(url: str, profile_dir: Path) -> BrowserHandle:
            nonlocal profile
            del url
            profile = profile_dir
            browser.write_active_port(profile_dir)
            websocket_url = _read_debug_endpoint(
                profile_dir,
                launcher_process,
                timeout=1,
            )
            return BrowserHandle(launcher_process, profile_dir, websocket_url)

        manager = BrowserLoginManager(launcher=launcher)
        try:
            waiting = manager.start("portal", "https://example.com/login")
            status = manager.status()[0]
            ready = manager.complete("portal")

            self.assertEqual("waiting", waiting["state"])
            self.assertEqual("waiting", status["state"])
            self.assertEqual("ready", ready["state"])
            self.assertTrue(browser.close_requested.wait(timeout=1))
            self.assertIsNotNone(profile)
            self.assertTrue(profile.exists())
        finally:
            manager.close()
            browser.close()


class BrowserLoginWebAPITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.local_app_data = patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.root / "local-app-data")},
        )
        self.local_app_data.start()
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(self._config(), ensure_ascii=False),
            encoding="utf-8",
        )
        self.processes: list[FakeProcess] = []
        self.profile_dirs: list[Path] = []
        self.launch_urls: list[str] = []

        def launcher(url: str, profile_dir: Path) -> BrowserHandle:
            self.launch_urls.append(url)
            self.profile_dirs.append(profile_dir)
            (profile_dir / "Default").mkdir(exist_ok=True)
            process = FakeProcess()
            self.processes.append(process)
            return BrowserHandle(process, profile_dir, "ws://127.0.0.1:1/devtools/browser/test")

        def reader(handle: BrowserHandle) -> list[dict[str, object]]:
            del handle
            return [
                {
                    "name": "PORTAL_SID",
                    "value": "web-api-cookie-secret-75aa",
                    "domain": "portal.example.com",
                    "path": "/",
                    "secure": True,
                }
            ]

        login_manager = BrowserLoginManager(launcher=launcher, cookie_reader=reader)
        self.server, self.app = create_server(
            self.config_path,
            port=0,
            browser_login_manager=login_manager,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = str(self.server.server_address[0])
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.local_app_data.stop()
        self.temporary_directory.cleanup()

    @staticmethod
    def _config() -> dict[str, object]:
        return {
            "start_date": "2026-01-01",
            "end_date": "2026-08-20",
            "output_dir": "output",
            "database": "output/state.sqlite3",
            "http": {
                "timeout_seconds": 5,
                "delay_seconds": 0,
                "max_retries": 0,
                "max_response_mb": 1,
                "max_download_mb": 1,
                "download_timeout_seconds": 5,
                "allow_private_hosts": False,
            },
            "sources": [
                {
                    "type": "custom_web",
                    "id": "portal",
                    "name": "测试登录网站",
                    "enabled": True,
                    "authority_rank": 50,
                    "start_urls": ["https://portal.example.com/notices"],
                    "auth": {
                        "mode": "browser",
                        "login_url": "https://portal.example.com/login",
                        "profile_id": "internal_profile_canary_4c21",
                    },
                }
            ],
            "ai": {"enabled": False},
            "recall": {"mode": "fast"},
        }

    def _request(self, method: str, path: str, payload: object | None = None) -> tuple[int, dict]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-CSRF-Token"] = self.app.csrf_token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            result = json.loads(response.read().decode("utf-8"))
            return response.status, result
        finally:
            connection.close()

    def test_start_complete_and_status_api_never_return_cookie_value(self) -> None:
        start_status, started = self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        ready_status, completed = self._request(
            "POST", "/api/browser-login/complete", {"source_id": "portal"}
        )
        get_status, status = self._request("GET", "/api/browser-login/status")
        checkpoint_status, checkpoint = self._request(
            "GET", "/api/browser-login/checkpoint"
        )

        self.assertEqual(202, start_status)
        self.assertEqual("waiting", started["session"]["state"])
        self.assertEqual("https://portal.example.com/login", self.launch_urls[-1])
        self.assertEqual(200, ready_status)
        self.assertEqual("ready", completed["session"]["state"])
        self.assertEqual(200, get_status)
        self.assertEqual(200, checkpoint_status)
        self.assertEqual(1, status["sessions"][0]["cookie_count"])
        self.assertNotIn(
            "web-api-cookie-secret-75aa",
            json.dumps([started, completed, status], ensure_ascii=False),
        )
        self.assertEqual("ready", status["checkpoint"]["state"])
        self.assertEqual("ready", checkpoint["checkpoint"]["state"])
        resumed_status, resumed = self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        self.assertEqual(202, resumed_status)
        self.assertEqual("waiting", resumed["session"]["state"])
        self.assertEqual("https://portal.example.com/notices", self.launch_urls[-1])
        serialized = json.dumps(
            [started, completed, status, checkpoint, resumed], ensure_ascii=False
        )
        self.assertNotIn("internal_profile_canary_4c21", serialized)
        self.assertNotIn("profile_id", serialized)

    def test_probe_focus_and_explicit_clear_api_expose_no_capability(self) -> None:
        _, started = self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        probe_status, probed = self._request(
            "POST", "/api/browser-login/probe", {"source_id": "portal"}
        )
        with patch(
            "tender_downloader.webui.browser_login.focus_browser_page",
            return_value=True,
        ):
            focus_status, focused = self._request(
                "POST", "/api/browser-login/focus", {"source_id": "portal"}
            )
        clear_status, cleared = self._request(
            "POST", "/api/browser-login/clear", {"source_id": "portal"}
        )

        self.assertEqual("waiting", started["session"]["state"])
        self.assertEqual(200, probe_status)
        self.assertEqual("waiting", probed["session"]["state"])
        self.assertEqual(200, focus_status)
        self.assertEqual("waiting", focused["session"]["state"])
        self.assertEqual(200, clear_status)
        self.assertEqual("cleared", cleared["session"]["state"])
        serialized = json.dumps([started, probed, focused, cleared], ensure_ascii=False)
        self.assertNotIn("devtools/browser", serialized)
        self.assertNotIn("web-api-cookie-secret", serialized)

    def test_wait_api_returns_credential_free_checkpoint(self) -> None:
        self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        waiting_status, waiting = self._request(
            "POST", "/api/browser-login/wait", {"timeout_seconds": 0}
        )
        self._request(
            "POST", "/api/browser-login/complete", {"source_id": "portal"}
        )
        ready_status, ready = self._request(
            "POST", "/api/browser-login/wait", {"timeout_seconds": 0}
        )

        self.assertEqual(200, waiting_status)
        self.assertFalse(waiting["ready"])
        self.assertEqual("waiting_for_user", waiting["checkpoint"]["state"])
        self.assertEqual(200, ready_status)
        self.assertTrue(ready["ready"])
        self.assertEqual("ready", ready["checkpoint"]["state"])
        serialized = json.dumps([waiting, ready], ensure_ascii=False)
        self.assertNotIn("web-api-cookie-secret", serialized)
        self.assertNotIn("devtools/browser", serialized)

    def test_run_requires_ready_login_and_consumes_it_after_spawn(self) -> None:
        with self.assertRaisesRegex(BrowserLoginError, "等待登录"):
            self.app.start("run")
        self.app.start_browser_login("portal")
        self.app.complete_browser_login("portal")

        captured: dict[str, object] = {}

        def fake_start(operation: str, **kwargs: object) -> None:
            captured["operation"] = operation
            captured.update(kwargs)

        with patch.object(self.app.runner, "start", side_effect=fake_start):
            self.app.start("run")

        self.assertEqual("run", captured["operation"])
        sessions = captured["source_sessions"]
        self.assertEqual(
            "web-api-cookie-secret-75aa",
            sessions["portal"]["cookies"][0]["value"],
        )
        self.assertEqual([], self.app.browser_logins.status())

        # The process watcher invokes this for success, partial, failure and
        # stop alike. The same in-memory login becomes ready for another run.
        finished_callback = captured["finished_callback"]
        self.assertTrue(callable(finished_callback))
        finished_callback()
        restored = self.app.browser_logins.status()
        self.assertEqual("ready", restored[0]["state"])
        self.assertEqual(1, restored[0]["cookie_count"])

        captured_again: dict[str, object] = {}

        def fake_second_start(operation: str, **kwargs: object) -> None:
            captured_again["operation"] = operation
            captured_again.update(kwargs)

        with patch.object(self.app.runner, "start", side_effect=fake_second_start):
            self.app.start("run")
        second_sessions = captured_again["source_sessions"]
        self.assertEqual(
            "web-api-cookie-secret-75aa",
            second_sessions["portal"]["cookies"][0]["value"],
        )

    def test_clear_reports_failure_when_exact_profile_cannot_be_removed(self) -> None:
        start_status, _ = self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        with patch(
            "tender_downloader.webui.browser_login._safe_remove_profile",
            return_value=False,
        ):
            cancel_status, cancelled = self._request(
                "POST", "/api/browser-login/clear", {"source_id": "portal"}
            )

        self.assertEqual(202, start_status)
        self.assertEqual(422, cancel_status)
        self.assertFalse(cancelled["ok"])
        self.assertIn("清理失败", cancelled["error"])
        session = self.app.browser_logins.status()[0]
        self.assertEqual("failed", session["state"])

    def test_orphan_cleanup_api_uses_current_config_and_exposes_counts_only(self) -> None:
        start_status, _ = self._request(
            "POST", "/api/browser-login/start", {"source_id": "portal"}
        )
        close_status, _ = self._request(
            "POST", "/api/browser-login/cancel", {"source_id": "portal"}
        )
        self.assertEqual(202, start_status)
        self.assertEqual(200, close_status)

        changed = self._config()
        changed["sources"][0]["auth"] = {"mode": "none"}
        self.config_path.write_text(
            json.dumps(changed, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        status_code, status = self._request("GET", "/api/browser-login/status")
        self.assertEqual(200, status_code)
        self.assertEqual(1, status["maintenance"]["orphaned_profile_count"])
        with patch(
            "tender_downloader.webui.browser_login._remove_profile_after_browser_exit",
            side_effect=lambda path, persistent_root=None: _safe_remove_profile(
                path, persistent_root=persistent_root
            ),
        ):
            clear_status, cleared = self._request(
                "POST", "/api/browser-login/clear-orphans", {}
            )

        self.assertEqual(200, clear_status)
        self.assertTrue(cleared["ok"])
        self.assertEqual(1, cleared["maintenance"]["removed_count"])
        self.assertEqual(0, cleared["maintenance"]["orphaned_profile_count"])
        serialized = json.dumps([status, cleared], ensure_ascii=False)
        self.assertNotIn("portal.example.com", serialized)
        self.assertNotIn("login_url", serialized)
        self.assertNotIn("profile_dir", serialized.lower())
        self.assertNotIn(str(self.profile_dirs[-1]), serialized)
        self.assertNotIn("devtools/browser", serialized)
        self.assertNotIn("web-api-cookie-secret", serialized)


class BrowserSessionEnvironmentTests(unittest.TestCase):
    def test_cookies_are_child_env_only_and_redacted_from_logs(self) -> None:
        secret = "child-cookie-secret-9b31"
        sessions = {
            "portal": {
                "cookies": [
                    {
                        "name": "SID",
                        "value": secret,
                        "domain": "example.com",
                        "path": "/",
                    }
                ]
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.json"
            config_path.write_text("{}", encoding="utf-8")
            original_popen = subprocess.Popen
            captured: dict[str, object] = {}

            def intercept_popen(**kwargs: object) -> subprocess.Popen[str]:
                environment = kwargs["env"]
                captured["session_json"] = environment.get(SOURCE_SESSIONS_ENV)
                captured["original_args"] = copy.deepcopy(kwargs["args"])
                replacement = dict(kwargs)
                replacement["args"] = [
                    sys.executable,
                    "-u",
                    "-c",
                    f"import os; print(os.environ[{SOURCE_SESSIONS_ENV!r}])",
                ]
                return original_popen(**replacement)

            os.environ.pop(SOURCE_SESSIONS_ENV, None)
            manager = ProcessManager(config_path)
            try:
                with patch(
                    "tender_downloader.webui.server.subprocess.Popen",
                    side_effect=intercept_popen,
                ):
                    manager.start("verify", source_sessions=sessions)
                    deadline = time.monotonic() + 10
                    while manager.status()["running"] and time.monotonic() < deadline:
                        time.sleep(0.025)
                    status = manager.status()
            finally:
                manager.close()
                os.environ.pop(SOURCE_SESSIONS_ENV, None)

        status_json = json.dumps(status, ensure_ascii=False)
        self.assertIn(secret, str(captured["session_json"]))
        self.assertNotIn(secret, repr(captured["original_args"]))
        self.assertNotIn(secret, status_json)
        self.assertIn("[已隐藏密钥]", status_json)
        self.assertNotIn(SOURCE_SESSIONS_ENV, os.environ)


class SliderChallengeAnswerTests(unittest.TestCase):
    """The manager runs an adapter-supplied slider answer inside the source's
    own isolated page and then re-probes; it never generates answers itself."""

    def setUp(self) -> None:
        self.profile_data = tempfile.TemporaryDirectory()
        env = patch.dict(os.environ, {"LOCALAPPDATA": self.profile_data.name})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self.profile_data.cleanup)
        self.handle_holder = {}

        def launcher(url: str, profile_dir: Path) -> BrowserHandle:
            handle = BrowserHandle(FakeProcess(), profile_dir, "ws://127.0.0.1:12345/devtools/browser/test")
            self.handle_holder["handle"] = handle
            return handle

        def cookies(handle: BrowserHandle) -> list[dict[str, object]]:
            return [{"name": "SESSION", "value": "v", "domain": ".example.com", "path": "/",
                     "secure": True, "httpOnly": True, "expires": 1_900_000_000, "sameSite": "Lax"}]

        self.manager = BrowserLoginManager(launcher=launcher, cookie_reader=cookies)
        self.addCleanup(self.manager.close)
        self.manager.start("portal", "https://example.com/login")

    def test_answer_solved_healthy_request_and_captured_cookies_set_ready(self) -> None:
        with patch.object(browser_login_module, "_browser_handle_alive", return_value=True), \
             patch.object(browser_login_module, "_evaluate_in_source_page",
                          side_effect=[{"solved": True}, {"healthy": True}]) as run:
            self.assertTrue(self.manager.answer_slider_challenge("portal", "(async()=>({solved:true}))()"))
        self.assertEqual(2, run.call_count)  # answer, then fresh-request verdict
        status = self.manager.status()
        self.assertEqual("ready", status[0]["state"])
        self.assertEqual(1, status[0]["cookie_count"])

    def test_answer_solved_but_unhealthy_request_returns_false(self) -> None:
        with patch.object(browser_login_module, "_browser_handle_alive", return_value=True), \
             patch.object(browser_login_module, "_evaluate_in_source_page",
                          side_effect=[{"solved": True}, {"healthy": False}]):
            self.assertFalse(self.manager.answer_slider_challenge("portal", "expr"))

    def test_unsolved_verdict_skips_reprobe_and_returns_false(self) -> None:
        for verdict in ({}, {"solved": False, "reason": "no_hole"}):
            with patch.object(browser_login_module, "_browser_handle_alive", return_value=True), \
                 patch.object(browser_login_module, "_evaluate_in_source_page",
                              return_value=verdict) as run, \
                 patch.object(self.manager, "probe") as probe:
                self.assertFalse(self.manager.answer_slider_challenge("portal", "expr"))
            run.assert_called_once()
            probe.assert_not_called()

    def test_unknown_source_never_touches_the_browser(self) -> None:
        with patch.object(browser_login_module, "_evaluate_in_source_page") as run:
            self.assertFalse(self.manager.answer_slider_challenge("missing", "expr"))
            run.assert_not_called()

    def test_dead_browser_handle_returns_false(self) -> None:
        with patch.object(browser_login_module, "_browser_handle_alive", return_value=False), \
             patch.object(browser_login_module, "_evaluate_in_source_page") as run:
            self.assertFalse(self.manager.answer_slider_challenge("portal", "expr"))
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
