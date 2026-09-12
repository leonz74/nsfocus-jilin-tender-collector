from __future__ import annotations

import base64
import tempfile
import unittest
from pathlib import Path
from typing import Any

from tender_downloader.browser_cdp import (
    BrowserCdpTransport,
    BrowserTransportError,
)
from tender_downloader.webui.browser_login import (
    BrowserHandle,
    BrowserLoginManager,
)


class FakeCdpSocket:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        target_url: str = "https://example.com/project/1",
        navigate_final_url: str | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.target_url = target_url
        self.navigate_final_url = navigate_final_url
        self.responses: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.closed = False

    def send_json(self, payload: dict[str, Any]) -> None:
        self.commands.append(payload)
        command_id = payload["id"]
        method = payload["method"]
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {
                        "type": "page",
                        "url": self.target_url,
                        "targetId": "target-1",
                    }
                ]
            }
        elif method == "Target.attachToTarget":
            result = {"sessionId": "session-1"}
        elif method == "Page.getFrameTree":
            result = {
                "frameTree": {
                    "frame": {"id": "frame-1", "url": self.target_url}
                }
            }
        elif method == "Page.navigate":
            self.target_url = self.navigate_final_url or payload["params"]["url"]
            result = {"frameId": "frame-1"}
        elif method == "Page.getResourceTree":
            result = {
                "frameTree": {
                    "frame": {
                        "id": "frame-1",
                        "url": self.target_url,
                        "mimeType": "text/html; charset=utf-8",
                    }
                }
            }
        elif method == "Page.getResourceContent":
            self.responses.append(
                {
                    "id": command_id,
                    "error": {"message": "use network fallback"},
                }
            )
            return
        elif method == "Runtime.evaluate":
            expression = payload.get("params", {}).get("expression")
            if expression == "document.readyState":
                result = {"result": {"type": "string", "value": "complete"}}
            elif expression == "document.documentElement.outerHTML.length":
                result = {"result": {"type": "number", "value": 128}}
            else:
                result = {
                    "result": {
                        "type": "string",
                        "value": "<html><body>fallback navigated</body></html>",
                    }
                }
        elif method == "Network.loadNetworkResource":
            result = {
                "resource": {
                    "success": True,
                    "httpStatusCode": 200,
                    "headers": {
                        "Content-Type": "application/octet-stream",
                        "X-Test": "yes",
                    },
                    "stream": "stream-1",
                }
            }
        elif method == "IO.read":
            value = self.chunks.pop(0) if self.chunks else b""
            result = {
                "data": base64.b64encode(value).decode("ascii"),
                "base64Encoded": True,
                "eof": not self.chunks,
            }
        elif method in {"IO.close", "Target.detachFromTarget"}:
            result = {}
        else:  # pragma: no cover - makes unexpected protocol growth obvious
            raise AssertionError(method)
        self.responses.append({"id": command_id, "result": result})

    def receive_json(self) -> dict[str, Any]:
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeCurrentPageCdpSocket(FakeCdpSocket):
    def __init__(
        self,
        content: bytes,
        *,
        content_error: bool = False,
        analysis_html: str = "<html><body>rendered marker</body></html>",
        dom_lengths: list[int] | None = None,
    ) -> None:
        super().__init__([b"network-fallback"])
        self.content = content
        self.content_error = content_error
        self.analysis_html = analysis_html
        self.dom_lengths = list(dom_lengths or [len(analysis_html)])
        self.last_dom_length = self.dom_lengths[-1]

    def send_json(self, payload: dict[str, Any]) -> None:
        self.commands.append(payload)
        command_id = payload["id"]
        method = payload["method"]
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {
                        "type": "page",
                        "url": "https://example.com/other",
                        "targetId": "target-other",
                    },
                    {
                        "type": "page",
                        "url": "https://example.com/project/1#visible-section",
                        "targetId": "target-exact",
                    },
                ]
            }
        elif method == "Target.attachToTarget":
            result = {"sessionId": "session-1"}
        elif method == "Page.getResourceTree":
            result = {
                "frameTree": {
                    "frame": {
                        "id": "frame-current",
                        "url": "https://example.com/project/1#visible-section",
                        "mimeType": "text/html; charset=utf-8",
                    }
                }
            }
        elif method == "Page.reload":
            result = {}
        elif method == "Page.getResourceContent" and self.content_error:
            self.responses.append(
                {
                    "id": command_id,
                    "error": {"message": "resource unavailable"},
                }
            )
            return
        elif method == "Page.getResourceContent":
            result = {
                "content": base64.b64encode(self.content).decode("ascii"),
                "base64Encoded": True,
            }
        elif method == "Runtime.evaluate":
            expression = payload.get("params", {}).get("expression")
            if expression == "document.readyState":
                result = {"result": {"type": "string", "value": "complete"}}
            elif expression == "document.documentElement.outerHTML.length":
                if self.dom_lengths:
                    self.last_dom_length = self.dom_lengths.pop(0)
                result = {
                    "result": {
                        "type": "number",
                        "value": self.last_dom_length,
                    }
                }
            else:
                result = {
                    "result": {
                        "type": "string",
                        "value": self.analysis_html,
                    }
                }
        elif method == "Page.getFrameTree":
            result = {"frameTree": {"frame": {"id": "frame-1"}}}
        elif method == "Network.loadNetworkResource":
            result = {
                "resource": {
                    "success": True,
                    "httpStatusCode": 200,
                    "headers": {"Content-Type": "text/html"},
                    "stream": "stream-1",
                }
            }
        elif method == "IO.read":
            value = self.chunks.pop(0) if self.chunks else b""
            result = {
                "data": base64.b64encode(value).decode("ascii"),
                "base64Encoded": True,
                "eof": not self.chunks,
            }
        elif method in {"IO.close", "Target.detachFromTarget"}:
            result = {}
        else:  # pragma: no cover - makes unexpected protocol growth obvious
            raise AssertionError(method)
        self.responses.append({"id": command_id, "result": result})


class FakeDuplicateExactPageCdpSocket(FakeCdpSocket):
    """Two exact tabs: an older hidden challenge and the solved front tab."""

    exact_url = "https://example.com/project/1"

    def __init__(self) -> None:
        super().__init__([b"unused-network-fallback"])
        self.sessions = {
            "old-challenge": "session-old",
            "current-solved": "session-current",
        }

    def send_json(self, payload: dict[str, Any]) -> None:
        self.commands.append(payload)
        command_id = payload["id"]
        method = payload["method"]
        session_id = payload.get("sessionId")
        if method == "Target.getTargets":
            result = {
                "targetInfos": [
                    {
                        "type": "page",
                        "url": self.exact_url,
                        "targetId": "old-challenge",
                    },
                    {
                        "type": "page",
                        "url": self.exact_url + "#after-human-verification",
                        "targetId": "current-solved",
                    },
                ]
            }
        elif method == "Target.attachToTarget":
            result = {
                "sessionId": self.sessions[payload["params"]["targetId"]]
            }
        elif method == "Page.getResourceTree":
            result = {
                "frameTree": {
                    "frame": {
                        "id": "frame-current"
                        if session_id == "session-current"
                        else "frame-old",
                        "url": self.exact_url,
                        "mimeType": "text/html; charset=utf-8",
                    }
                }
            }
        elif method == "Runtime.evaluate":
            expression = payload.get("params", {}).get("expression", "")
            if "document.visibilityState" in expression:
                value = (
                    {"visible": True, "focused": True, "challenge": False}
                    if session_id == "session-current"
                    else {"visible": False, "focused": False, "challenge": True}
                )
                result = {"result": {"type": "object", "value": value}}
            elif expression == "document.readyState":
                result = {"result": {"type": "string", "value": "complete"}}
            elif expression == "document.documentElement.outerHTML.length":
                result = {"result": {"type": "number", "value": 256}}
            elif expression == "document.documentElement.outerHTML":
                result = {
                    "result": {
                        "type": "string",
                        "value": "<html><body>notice detail</body></html>",
                    }
                }
            else:  # pragma: no cover - makes protocol growth obvious
                raise AssertionError(expression)
        elif method == "Page.getResourceContent":
            if session_id != "session-current":  # pragma: no cover
                raise AssertionError("challenge tab must not be read")
            result = {
                "content": base64.b64encode(b"solved-page-source").decode("ascii"),
                "base64Encoded": True,
            }
        elif method == "Target.detachFromTarget":
            result = {}
        else:  # pragma: no cover - makes unexpected protocol growth obvious
            raise AssertionError(method)
        self.responses.append({"id": command_id, "result": result})


class BrowserCdpTransportTests(unittest.TestCase):
    def _transport(
        self,
        socket: FakeCdpSocket,
        *,
        clock: FakeClock | None = None,
    ) -> BrowserCdpTransport:
        fake_clock = clock or FakeClock()
        return BrowserCdpTransport(
            "ws://127.0.0.1:9222/devtools/browser/random-capability",
            "https://example.com",
            socket_factory=lambda _url, _timeout: socket,
            clock=fake_clock.monotonic,
            sleeper=fake_clock.sleep,
        )

    def test_streams_raw_bytes_with_credentials_and_exact_origin(self) -> None:
        first = b"\x00\xffbinary"
        second = "标书".encode("utf-8")
        socket = FakeCdpSocket([first, second])
        transport = self._transport(socket)

        result = transport.request(
            "https://example.com/files/bid.bin#ignored",
            max_bytes=1024,
        )

        self.assertEqual(first + second, result.body)
        self.assertEqual("application/octet-stream", result.headers["content-type"])
        load = next(
            command
            for command in socket.commands
            if command["method"] == "Network.loadNetworkResource"
        )
        self.assertEqual("https://example.com/files/bid.bin", load["params"]["url"])
        self.assertTrue(load["params"]["options"]["includeCredentials"])
        self.assertEqual("session-1", load["sessionId"])
        self.assertNotIn(
            "Page.reload",
            [command["method"] for command in socket.commands],
        )
        navigate = next(
            command
            for command in socket.commands
            if command["method"] == "Page.navigate"
        )
        self.assertEqual(
            "https://example.com/files/bid.bin",
            navigate["params"]["url"],
        )
        self.assertEqual("frame-1", navigate["params"]["frameId"])
        self.assertTrue(socket.closed)

    def test_rejects_cross_origin_before_connecting(self) -> None:
        socket = FakeCdpSocket([b"never"])
        transport = self._transport(socket)

        with self.assertRaisesRegex(BrowserTransportError, "精确来源"):
            transport.request("https://cdn.example.com/file", max_bytes=1024)

        self.assertEqual([], socket.commands)

    def test_does_not_attach_or_navigate_a_cross_origin_target(self) -> None:
        socket = FakeCdpSocket(
            [b"never"],
            target_url="https://unrelated.example.net/open-page",
        )
        transport = self._transport(socket)

        with self.assertRaisesRegex(BrowserTransportError, "没有保持当前站点页面"):
            transport.request(
                "https://example.com/project/1",
                max_bytes=1024,
            )

        methods = [command["method"] for command in socket.commands]
        self.assertEqual(["Target.getTargets"], methods)
        self.assertNotIn("Page.navigate", methods)

    def test_size_limit_deletes_partial_download(self) -> None:
        socket = FakeCdpSocket([b"too-large"])
        transport = self._transport(socket)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "file.part"
            with self.assertRaisesRegex(BrowserTransportError, "大小上限"):
                transport.download_to(
                    "https://example.com/file.bin", target, max_bytes=2
                )
            self.assertFalse(target.exists())

    def test_same_origin_fallback_download_never_navigates(self) -> None:
        socket = FakeCdpSocket([b"attachment-bytes"])
        transport = self._transport(socket)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "attachment.part"

            result = transport.download_to(
                "https://example.com/files/attachment.bin",
                target,
                max_bytes=1024,
            )

            self.assertEqual(b"attachment-bytes", target.read_bytes())
            self.assertEqual(len(b"attachment-bytes"), result.size)
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.navigate", methods)
        self.assertNotIn("Page.reload", methods)
        self.assertNotIn("Runtime.evaluate", methods)

    def test_descriptor_must_match_expected_origin(self) -> None:
        with self.assertRaisesRegex(BrowserTransportError, "不匹配"):
            BrowserCdpTransport.from_descriptor(
                {
                    "kind": BrowserCdpTransport.KIND,
                    "websocket_url": (
                        "ws://127.0.0.1:9222/devtools/browser/random-capability"
                    ),
                    "origin": "https://example.com",
                },
                expected_origin="https://other.example.com",
            )

    def test_exact_page_is_prioritized_and_current_bytes_are_reused(self) -> None:
        expected = b"\x00\xffalready-loaded-document"
        rendered = "<html><body>rendered-only marker</body></html>"
        socket = FakeCurrentPageCdpSocket(expected, analysis_html=rendered)
        transport = self._transport(socket)

        result = transport.request(
            "https://example.com/project/1#request-fragment",
            max_bytes=1024,
        )

        self.assertEqual(expected, result.body)
        self.assertEqual(rendered.encode("utf-8"), result.analysis_body)
        self.assertEqual(len(expected), result.size)
        self.assertEqual(
            "text/html; charset=utf-8",
            result.headers["content-type"],
        )
        attach = next(
            command
            for command in socket.commands
            if command["method"] == "Target.attachToTarget"
        )
        self.assertEqual("target-exact", attach["params"]["targetId"])
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.reload", methods)
        self.assertIn("Page.getResourceTree", methods)
        self.assertIn("Page.getResourceContent", methods)
        runtime = next(
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and command["params"]["expression"]
            == "document.documentElement.outerHTML"
        )
        self.assertEqual(
            "document.documentElement.outerHTML",
            runtime["params"]["expression"],
        )
        self.assertIs(True, runtime["params"]["returnByValue"])
        self.assertNotIn("Network.loadNetworkResource", methods)

        ready = next(
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and command["params"]["expression"] == "document.readyState"
        )
        self.assertIs(True, ready["params"]["returnByValue"])

    def test_duplicate_exact_tabs_prefer_focused_clean_page_without_action(self) -> None:
        socket = FakeDuplicateExactPageCdpSocket()
        transport = self._transport(socket)

        result = transport.request(
            "https://example.com/project/1#crawler-fragment",
            max_bytes=1024,
        )

        self.assertEqual(b"solved-page-source", result.body)
        attach_ids = [
            command["params"]["targetId"]
            for command in socket.commands
            if command["method"] == "Target.attachToTarget"
        ]
        self.assertEqual(["old-challenge", "current-solved"], attach_ids)
        content_read = next(
            command
            for command in socket.commands
            if command["method"] == "Page.getResourceContent"
        )
        self.assertEqual("session-current", content_read["sessionId"])
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.navigate", methods)
        self.assertNotIn("Page.reload", methods)

        probes = [
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and "document.visibilityState" in command["params"]["expression"]
        ]
        self.assertEqual(2, len(probes))
        for probe in probes:
            expression = probe["params"]["expression"]
            self.assertNotIn("https://example.com/project/1", expression)
            self.assertNotIn("cookie", expression.lower())
            self.assertIs(True, probe["params"]["returnByValue"])

    def test_same_exact_url_is_settled_only_on_first_request_without_reload(self) -> None:
        socket = FakeCurrentPageCdpSocket(b"stable-document")
        transport = self._transport(socket)

        first = transport.request(
            "https://example.com/project/1#first",
            max_bytes=1024,
        )
        second = transport.request(
            "https://example.com/project/1#second",
            max_bytes=1024,
        )

        self.assertEqual(b"stable-document", first.body)
        self.assertEqual(b"stable-document", second.body)
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.reload", methods)
        ready_evaluations = [
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and command["params"]["expression"] == "document.readyState"
        ]
        self.assertEqual(1, len(ready_evaluations))

    def test_refresh_waits_for_changing_dom_length_to_stabilize(self) -> None:
        socket = FakeCurrentPageCdpSocket(
            b"async-rendered-document",
            dom_lengths=[100, 240, 360, 360, 360],
        )
        clock = FakeClock()
        transport = self._transport(socket, clock=clock)

        result = transport.request(
            "https://example.com/project/1",
            max_bytes=1024,
        )

        self.assertEqual(b"async-rendered-document", result.body)
        self.assertGreaterEqual(sum(clock.sleeps), 2.0)
        self.assertEqual(2.0, clock.sleeps[0])
        length_evaluations = [
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and command["params"]["expression"]
            == "document.documentElement.outerHTML.length"
        ]
        self.assertEqual(5, len(length_evaluations))
        methods = [command["method"] for command in socket.commands]
        self.assertLess(
            socket.commands.index(length_evaluations[-1]),
            methods.index("Page.getResourceContent"),
        )

    def test_unstable_dom_times_out_without_reading_or_reloading(self) -> None:
        socket = FakeCurrentPageCdpSocket(
            b"must-not-be-read",
            dom_lengths=list(range(1, 100)),
        )
        clock = FakeClock()
        transport = self._transport(socket, clock=clock)

        with self.assertRaisesRegex(BrowserTransportError, "DOM 稳定等待超时"):
            transport.request(
                "https://example.com/project/1",
                max_bytes=1024,
            )

        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.reload", methods)
        self.assertNotIn("Page.getResourceContent", methods)
        self.assertNotIn("Network.loadNetworkResource", methods)
        outer_html_evaluations = [
            command
            for command in socket.commands
            if command["method"] == "Runtime.evaluate"
            and command["params"]["expression"]
            == "document.documentElement.outerHTML"
        ]
        self.assertEqual([], outer_html_evaluations)
        self.assertLessEqual(clock.now, 12.0)
        self.assertTrue(socket.closed)

        with self.assertRaisesRegex(BrowserTransportError, "此前加载失败"):
            transport.request(
                "https://example.com/project/1",
                max_bytes=1024,
            )
        self.assertNotIn(
            "Page.reload",
            [command["method"] for command in socket.commands],
        )

    def test_same_origin_fallback_navigates_to_exact_url_only_once(self) -> None:
        socket = FakeCdpSocket([b"fetched-through-fallback"])
        transport = self._transport(socket)

        first = transport.request(
            "https://example.com/not-the-open-page",
            max_bytes=1024,
        )
        transport.request(
            "https://example.com/not-the-open-page#second-phase",
            max_bytes=1024,
        )

        self.assertEqual(b"fetched-through-fallback", first.body)
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Page.reload", methods)
        self.assertEqual(1, methods.count("Page.navigate"))
        navigate = next(
            command
            for command in socket.commands
            if command["method"] == "Page.navigate"
        )
        self.assertEqual(
            "https://example.com/not-the-open-page",
            navigate["params"]["url"],
        )
        self.assertEqual("frame-1", navigate["params"]["frameId"])
        self.assertEqual("session-1", navigate["sessionId"])
        attach = next(
            command
            for command in socket.commands
            if command["method"] == "Target.attachToTarget"
        )
        self.assertEqual("target-1", attach["params"]["targetId"])

    def test_fallback_navigation_rejects_non_exact_redirect(self) -> None:
        socket = FakeCdpSocket(
            [b"must-not-be-read"],
            navigate_final_url="https://example.com/login/",
        )
        transport = self._transport(socket)

        with self.assertRaisesRegex(BrowserTransportError, "精确目标地址"):
            transport.request(
                "https://example.com/project/2",
                max_bytes=1024,
            )

        methods = [command["method"] for command in socket.commands]
        self.assertEqual(1, methods.count("Page.navigate"))
        self.assertNotIn("Page.getResourceContent", methods)
        self.assertNotIn("Network.loadNetworkResource", methods)

    def test_current_document_failure_safely_falls_back_to_network_load(self) -> None:
        socket = FakeCurrentPageCdpSocket(b"unused", content_error=True)
        transport = self._transport(socket)

        result = transport.request(
            "https://example.com/project/1",
            max_bytes=1024,
        )

        self.assertEqual(b"network-fallback", result.body)
        self.assertIsNotNone(result.analysis_body)
        methods = [command["method"] for command in socket.commands]
        self.assertLess(
            methods.index("Page.getResourceContent"),
            methods.index("Network.loadNetworkResource"),
        )

    def test_current_document_size_limit_does_not_issue_second_request(self) -> None:
        socket = FakeCurrentPageCdpSocket(b"over-limit")
        transport = self._transport(socket)

        with self.assertRaisesRegex(BrowserTransportError, "大小上限"):
            transport.request(
                "https://example.com/project/1",
                max_bytes=2,
            )

        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Network.loadNetworkResource", methods)

    def test_rendered_dom_is_bounded_without_changing_source_body(self) -> None:
        socket = FakeCurrentPageCdpSocket(
            b"ok",
            analysis_html="rendered-is-over-the-limit",
        )
        transport = self._transport(socket)

        result = transport.request(
            "https://example.com/project/1",
            max_bytes=2,
        )

        self.assertEqual(b"ok", result.body)
        self.assertIsNone(result.analysis_body)

    def test_download_does_not_evaluate_or_return_rendered_dom(self) -> None:
        socket = FakeCurrentPageCdpSocket(b"exact-download-bytes")
        transport = self._transport(socket)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "source.bin"
            result = transport.download_to(
                "https://example.com/project/1",
                target,
                max_bytes=1024,
            )

            self.assertEqual(b"exact-download-bytes", target.read_bytes())
            self.assertIsNone(result.analysis_body)
        methods = [command["method"] for command in socket.commands]
        self.assertNotIn("Runtime.evaluate", methods)
        self.assertNotIn("Page.reload", methods)
        self.assertNotIn("Page.navigate", methods)


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
        if self.return_code is None:
            self.return_code = 0
        return self.return_code

    def kill(self) -> None:
        self.terminate()


class BrowserLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_directory = tempfile.TemporaryDirectory()
        self.profile_root = Path(self.profile_directory.name) / "profiles" / "v1"

    def tearDown(self) -> None:
        self.profile_directory.cleanup()

    def test_live_browser_is_closed_only_after_child_lease_finishes(self) -> None:
        process = FakeProcess()

        def launcher(_url: str, profile: Path) -> BrowserHandle:
            return BrowserHandle(
                process,
                profile,
                "ws://127.0.0.1:1/devtools/browser/capability",
                live_transport=True,
            )

        manager = BrowserLoginManager(
            launcher=launcher,
            profile_root=self.profile_root,
            page_probe=lambda _handle, _url: "content",
            cookie_reader=lambda _handle: [
                {
                    "name": "SID",
                    "value": "not-exposed",
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        )
        try:
            manager.start("portal", "https://example.com/login")
            manager.complete("portal")
            self.assertFalse(process.terminated)

            payload = manager.lease_for_sources(
                ["portal"],
                expected_login_urls={"portal": "https://example.com/login"},
            )
            self.assertEqual([], manager.status())
            descriptor = payload["portal"]["browser_transport"]
            self.assertEqual(BrowserCdpTransport.KIND, descriptor["kind"])
            self.assertNotIn(
                descriptor["websocket_url"],
                str(manager.status()),
            )

            manager.release_leases(["portal"])
            self.assertTrue(process.terminated)
        finally:
            manager.close()

    def test_completed_failed_and_stopped_runs_restore_same_live_session(self) -> None:
        process = FakeProcess()
        created_profile: Path | None = None

        def launcher(_url: str, profile: Path) -> BrowserHandle:
            nonlocal created_profile
            created_profile = profile
            return BrowserHandle(
                process,
                profile,
                "ws://127.0.0.1:1/devtools/browser/same-capability",
                live_transport=True,
            )

        manager = BrowserLoginManager(
            launcher=launcher,
            profile_root=self.profile_root,
            page_probe=lambda _handle, _url: "content",
            cookie_reader=lambda _handle: [
                {
                    "name": "SID",
                    "value": "still-in-memory",
                    "domain": "example.com",
                    "path": "/",
                    "secure": True,
                }
            ],
        )
        try:
            manager.start("portal", "https://example.com/login")
            manager.complete("portal")
            expected_ws = manager.sessions_for_sources(["portal"])["portal"][
                "browser_transport"
            ]["websocket_url"]

            for outcome in ("completed", "failed", "stopped"):
                token = f"lease-{outcome}"
                leased = manager.lease_for_sources(
                    ["portal"], lease_token=token
                )
                self.assertEqual(expected_ws, leased["portal"]["browser_transport"]["websocket_url"])
                self.assertEqual([], manager.status())
                manager.release_leases(
                    ["portal"], restore=True, lease_token=token
                )
                status = manager.status()
                self.assertEqual("ready", status[0]["state"])
                self.assertEqual(1, status[0]["cookie_count"])
                self.assertFalse(process.terminated)

            # A callback from an older process generation cannot touch the new
            # lease, even when it names the same source.
            manager.lease_for_sources(["portal"], lease_token="new-generation")
            manager.release_leases(
                ["portal"], restore=False, lease_token="old-generation"
            )
            self.assertEqual([], manager.status())
            manager.release_leases(
                ["portal"], restore=True, lease_token="new-generation"
            )

            cancelled = manager.cancel("portal")
            self.assertEqual("closed", cancelled["state"])
            self.assertTrue(process.terminated)
            self.assertIsNotNone(created_profile)
            self.assertTrue(created_profile.exists())
        finally:
            manager.close()


if __name__ == "__main__":
    unittest.main()
