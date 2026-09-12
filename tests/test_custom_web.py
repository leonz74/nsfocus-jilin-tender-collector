from __future__ import annotations

import base64
import json
import os
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs

from tender_downloader.http_client import HttpClient, HttpResult, SourceBlocked
from tender_downloader.models import AttachmentRef, Coverage, Notice, RawDocument
from tender_downloader.sources.custom_web import CustomWebSource


NOTICE_HTML = """<!doctype html><html><head><title>网络安全服务采购公告</title></head>
<body><h1>网络安全服务采购公告</h1><p>发布时间：2026-08-18</p>
<p>项目名称：网络安全服务</p><p>采购人：吉林测试单位</p>
<a href="/files/source.pdf">招标文件下载</a></body></html>""".encode()

BEIJING_NOTICE_HTML = """<!doctype html><html><head><title>北京市机房服务采购公告</title></head>
<body><h1>北京市机房服务采购公告</h1><p>发布时间：2026-08-17</p>
<p>项目名称：机房服务</p><p>采购人：北京测试单位</p></body></html>""".encode()

DIRECT_SEED_HTML = """<!doctype html><html><head><title>网络设备采购公告</title></head>
<body><h1>网络设备采购公告</h1><p>发布时间：2026-08-12</p>
<p>详细公告内容。</p><a href="/notice/654321">相关公告</a></body></html>""".encode()


class _FormHandler(BaseHTTPRequestHandler):
    login_fields: dict[str, list[str]] = {}
    notice_cookie = ""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode())
        type(self).login_fields = fields
        if fields.get("account") != ["alice"] or fields.get("secret") != ["correct"]:
            body = "用户名或密码错误".encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        body = b"login success"
        self.send_response(200)
        self.send_header("Set-Cookie", "session=valid; Path=/; HttpOnly")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).notice_cookie = self.headers.get("Cookie", "")
        if "session=valid" not in type(self).notice_cookie:
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(NOTICE_HTML)))
        self.end_headers()
        self.wfile.write(NOTICE_HTML)

    def log_message(self, format: str, *args) -> None:
        return


class _RedirectTargetHandler(BaseHTTPRequestHandler):
    authorization = "not-set"
    cookie = "not-set"
    x_api_key = "not-set"

    def do_GET(self) -> None:  # noqa: N802
        type(self).authorization = self.headers.get("Authorization", "")
        type(self).cookie = self.headers.get("Cookie", "")
        type(self).x_api_key = self.headers.get("X-Goog-API-Key", "")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(NOTICE_HTML)))
        self.end_headers()
        self.wfile.write(NOTICE_HTML)

    def log_message(self, format: str, *args) -> None:
        return


class _RedirectSourceHandler(BaseHTTPRequestHandler):
    target_url = ""
    authorization = ""

    def do_GET(self) -> None:  # noqa: N802
        type(self).authorization = self.headers.get("Authorization", "")
        self.send_response(302)
        self.send_header("Location", type(self).target_url)
        self.send_header("Set-Cookie", "source_session=must-not-cross-origin; Path=/")
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


class _PublicListHandler(BaseHTTPRequestHandler):
    authorization = "not-set"

    def do_GET(self) -> None:  # noqa: N802
        type(self).authorization = self.headers.get("Authorization", "")
        path = self.path.split("?", 1)[0]
        if path == "/list":
            body = (
                '<html><head><title>招标公告列表</title></head><body>'
                '<a href="/detail/123456">网络安全服务采购公告</a>'
                '</body></html>'
            ).encode()
        elif path == "/regional-list":
            body = (
                '<html><head><title>全国招标公告列表</title></head><body>'
                '<a href="/detail/123456">网络安全服务采购公告</a>'
                '<a href="/detail/654321">机房服务采购公告</a>'
                '</body></html>'
            ).encode()
        elif path == "/empty-list":
            body = (
                '<html><head><title>项目信息列表</title></head><body>'
                '<a href="/goods-documents">货物类招标文件</a>'
                '</body></html>'
            ).encode()
        elif path == "/channel":
            body = (
                '<html><head><title>招投标信息频道</title></head><body>'
                '<a href="/purchasing-channel">采购信息</a>'
                '</body></html>'
            ).encode()
        elif path == "/detail/654321":
            body = BEIJING_NOTICE_HTML
        elif path == "/20260812-n2-security.html":
            body = DIRECT_SEED_HTML
        else:
            body = NOTICE_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class _AggregatePortalHandler(BaseHTTPRequestHandler):
    paths: list[str] = []

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        type(self).paths.append(path)
        if path == "/search/":
            self.send_response(302)
            self.send_header("Location", "/nc/")
            self.end_headers()
            return
        if path == "/nc/":
            body = (
                '<html><head><title>招标中心</title></head><body><h1>招标中心</h1>'
                '<a href="/download">文件下载</a>'
                '<a href="/goods-documents">货物类招标文件</a>'
                '<a href="/purchasing-channel">采购信息</a>'
                '<a href="/notice/123456">网络安全服务采购公告</a>'
                '</body></html>'
            ).encode()
        else:
            # Navigation pages intentionally look somewhat notice-like.  They
            # must never be fetched as candidates in the regression below.
            body = NOTICE_HTML
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


def _start(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


class CustomWebSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        _FormHandler.login_fields = {}
        _FormHandler.notice_cookie = ""
        _RedirectTargetHandler.authorization = "not-set"
        _RedirectTargetHandler.cookie = "not-set"
        _RedirectTargetHandler.x_api_key = "not-set"
        _AggregatePortalHandler.paths = []

    @staticmethod
    def _client() -> HttpClient:
        return HttpClient(
            user_agent="tests/1.0",
            timeout_seconds=3,
            delay_seconds=0,
            max_retries=0,
            allow_private_hosts=True,
        )

    def test_buyer_strips_escaped_table_markup(self) -> None:
        text = (
            '采购人：</td><td style="background:#fff;padding:0 10px">'
            '中国人民银行吉林省分行 </td></tr> <tr> <td style="bac'
        )

        self.assertEqual(
            "中国人民银行吉林省分行",
            CustomWebSource._buyer(text),
        )

    def test_form_login_reuses_cookie_without_changing_source_bytes(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            source = CustomWebSource(client, {
                "id": "partner_form",
                "name": "授权测试站",
                "start_urls": [f"{root}/notice"],
                "auth": {
                    "mode": "form",
                    "login_url": f"{root}/login",
                    "username_field": "account",
                    "password_field": "secret",
                    "extra_fields": {"action": "login"},
                },
            })
            credentials = json.dumps({
                "partner_form": {"username": "alice", "password": "correct"}
            })
            with patch.dict(os.environ, {"TENDER_SOURCE_CREDENTIALS_JSON": credentials}):
                coverage = Coverage(source=source.name)
                notices = list(source.iter_notices(
                    date(2026, 1, 1), date(2026, 12, 31), coverage
                ))
                detail = source.fetch_detail(notices[0])
            self.assertEqual(["alice"], _FormHandler.login_fields["account"])
            self.assertEqual(["login"], _FormHandler.login_fields["action"])
            self.assertIn("session=valid", _FormHandler.notice_cookie)
            self.assertEqual(NOTICE_HTML, detail.body)
            self.assertEqual("网络安全服务采购公告", notices[0].title)
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_browser_login_reuses_httponly_cookie(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        source = None
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            source = CustomWebSource(client, {
                "id": "browser_session",
                "name": "浏览器会话测试站",
                "start_urls": [f"{root}/notice"],
                "auth": {"mode": "browser", "login_url": f"{root}/login"},
            })
            sessions = json.dumps({
                "browser_session": {
                    "captured_at": "2026-08-20T10:00:00Z",
                    "cookies": [{
                        "name": "session",
                        "value": "valid",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }],
                }
            })
            with patch.dict(
                os.environ,
                {"TENDER_SOURCE_SESSIONS_JSON": sessions},
                clear=True,
            ):
                source.prepare()
                notices = list(source.iter_notices(
                    date(2026, 1, 1),
                    date(2026, 12, 31),
                    Coverage(source=source.name),
                ))
            self.assertIn("session=valid", _FormHandler.notice_cookie)
            self.assertEqual(1, len(notices))
            self.assertEqual("网络安全服务采购公告", notices[0].title)
            self.assertTrue(source._prepared)
        finally:
            if source is not None:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_browser_login_missing_session_blocks_before_network(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "browser_missing",
            "name": "未登录站点",
            "start_urls": ["https://example.com/notice"],
            "auth": {"mode": "browser"},
        })
        try:
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(SourceBlocked, "尚未完成浏览器登录"):
                    source.prepare()
        finally:
            source.close()
            client.close()

    def test_browser_login_does_not_import_cross_domain_cookie(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        root = f"http://127.0.0.1:{server.server_port}"
        source = CustomWebSource(client, {
            "id": "browser_tampered",
            "name": "会话篡改测试站",
            "start_urls": [f"{root}/notice"],
            "auth": {"mode": "browser"},
        })
        sessions = json.dumps({
            "browser_tampered": {
                "cookies": [
                    {
                        "name": "session",
                        "value": "valid",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "http_only": True,
                        "secure": False,
                    },
                    {
                        "name": "stolen_session",
                        "value": "must-not-import",
                        "domain": ".attacker.example.net",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": True,
                    },
                ]
            }
        })
        try:
            with patch.dict(
                os.environ,
                {"TENDER_SOURCE_SESSIONS_JSON": sessions},
                clear=True,
            ):
                source.prepare()
            self.assertIn("session=valid", _FormHandler.notice_cookie)
            self.assertEqual(
                "session=valid",
                source.client._cookie_header(f"{root}/notice"),
            )
            self.assertEqual("", source.client._cookie_header("https://attacker.example.net/"))
        finally:
            source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_browser_login_probe_reports_unauthorized_session(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        source = None
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            source = CustomWebSource(client, {
                "id": "browser_expired",
                "name": "过期会话测试站",
                "start_urls": [f"{root}/notice"],
                "auth": {"mode": "browser"},
            })
            sessions = json.dumps({
                "browser_expired": {
                    "cookies": [{
                        "name": "session",
                        "value": "expired",
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": False,
                    }]
                }
            })
            with patch.dict(
                os.environ,
                {"TENDER_SOURCE_SESSIONS_JSON": sessions},
                clear=True,
            ):
                with self.assertRaisesRegex(SourceBlocked, "已失效或无权访问"):
                    source.prepare()
        finally:
            if source is not None:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_form_login_failure_is_explicitly_blocked(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            source = CustomWebSource(client, {
                "id": "bad_login",
                "name": "登录失败测试站",
                "start_urls": [f"{root}/notice"],
                "auth": {
                    "mode": "form",
                    "login_url": f"{root}/login",
                    "username_field": "account",
                    "password_field": "secret",
                },
            })
            credentials = json.dumps({
                "bad_login": {"username": "alice", "password": "wrong"}
            })
            with patch.dict(os.environ, {"TENDER_SOURCE_CREDENTIALS_JSON": credentials}):
                with self.assertRaisesRegex(SourceBlocked, "等待人工复核"):
                    source.prepare()
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_custom_sources_use_isolated_cookie_sessions(self) -> None:
        server, _ = _start(_FormHandler)
        client = self._client()
        authenticated = None
        public = None
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            authenticated = CustomWebSource(client, {
                "id": "isolated_login",
                "name": "隔离登录站",
                "start_urls": [f"{root}/notice"],
                "auth": {
                    "mode": "form",
                    "login_url": f"{root}/login",
                    "username_field": "account",
                    "password_field": "secret",
                },
            })
            public = CustomWebSource(client, {
                "id": "isolated_public",
                "name": "隔离公开站",
                "start_urls": [f"{root}/notice"],
                "auth": {"mode": "none"},
            })
            credentials = json.dumps({
                "isolated_login": {"username": "alice", "password": "correct"}
            })
            with patch.dict(os.environ, {"TENDER_SOURCE_CREDENTIALS_JSON": credentials}):
                authenticated.prepare()
                with self.assertRaises(SourceBlocked):
                    list(public.iter_notices(
                        date(2026, 1, 1), date(2026, 12, 31), Coverage(source=public.name)
                    ))
            self.assertIsNot(authenticated.client, public.client)
            self.assertIsNot(authenticated.client, client)
        finally:
            if authenticated is not None:
                authenticated.close()
            if public is not None:
                public.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_missing_credentials_blocks_before_network(self) -> None:
        source = CustomWebSource(self._client(), {
            "id": "needs_login",
            "start_urls": ["https://example.com/notice"],
            "auth": {"mode": "basic"},
        })
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SourceBlocked, "等待人工复核"):
                source.prepare()
        source.client.close()

    def test_no_auth_discovers_one_level_same_origin_notice(self) -> None:
        server, _ = _start(_PublicListHandler)
        client = self._client()
        try:
            source = CustomWebSource(client, {
                "id": "public_site",
                "start_urls": [f"http://127.0.0.1:{server.server_port}/list"],
                "auth": {"mode": "none"},
            })
            with patch.dict(os.environ, {}, clear=True):
                coverage = Coverage(source=source.name)
                notices = list(source.iter_notices(
                    date(2026, 1, 1), date(2026, 12, 31), coverage
                ))
            self.assertEqual(1, len(notices))
            self.assertTrue(notices[0].url.endswith("/detail/123456"))
            self.assertEqual("", _PublicListHandler.authorization)
            self.assertEqual("partial", coverage.status)
        finally:
            client.close()
            server.shutdown()
            server.server_close()

    def test_center_page_is_not_notice_and_navigation_is_not_candidate(self) -> None:
        server, _ = _start(_AggregatePortalHandler)
        client = self._client()
        source = None
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            source = CustomWebSource(client, {
                "id": "aggregate_portal",
                "start_urls": [f"{root}/search/"],
                "auth": {"mode": "none"},
            })
            notices = list(source.iter_notices(
                date(2026, 1, 1), date(2026, 12, 31), Coverage(source=source.name)
            ))
            self.assertEqual(1, len(notices))
            self.assertTrue(notices[0].url.endswith("/notice/123456"))
            self.assertNotIn("/download", _AggregatePortalHandler.paths)
            self.assertNotIn("/goods-documents", _AggregatePortalHandler.paths)
            self.assertNotIn("/purchasing-channel", _AggregatePortalHandler.paths)
        finally:
            if source is not None:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_artifact_discovery_rejects_navigation_but_keeps_file_apis(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "artifact_filter",
            "start_urls": ["https://example.test/notices"],
            "auth": {"mode": "none"},
        })
        detail = RawDocument(
            body=(
                '<html><body><a href="/download">文件下载</a>'
                '<a href="/goods-documents">货物类招标文件</a>'
                '<a href="/files/spec.pdf">项目附件.pdf</a>'
                '<a href="/attachment/download?fileId=abc123">附件下载</a>'
                '<a href="/api/file/7">下载附件</a></body></html>'
            ).encode(),
            url="https://example.test/notice/123456",
            headers={"content-type": "text/html; charset=utf-8"},
        )
        notice = Notice(
            source=source.name,
            authority_rank=50,
            external_id="artifact-filter-1",
            title="网络安全采购公告",
            published_at="2026-08-20",
            url=detail.url,
        )
        try:
            artifacts = source.discover_artifacts(notice, detail)
        finally:
            source.close()
            client.close()

        urls = [item.url for item in artifacts]
        self.assertEqual([
            "https://example.test/files/spec.pdf",
            "https://example.test/attachment/download?fileId=abc123",
            "https://example.test/api/file/7",
        ], urls)

    def test_known_html_detail_url_remains_a_direct_seed(self) -> None:
        server, _ = _start(_PublicListHandler)
        client = self._client()
        source = None
        try:
            url = (
                f"http://127.0.0.1:{server.server_port}"
                "/20260812-n2-security.html"
            )
            source = CustomWebSource(client, {
                "id": "known_detail_seed",
                "start_urls": [url],
                "auth": {"mode": "none"},
            })
            notices = list(source.iter_notices(
                date(2026, 1, 1), date(2026, 12, 31), Coverage(source=source.name)
            ))
            self.assertEqual(1, len(notices))
            self.assertEqual("网络设备采购公告", notices[0].title)
            self.assertEqual(url, notices[0].url)
        finally:
            if source is not None:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_list_and_channel_seed_pages_are_not_notices(self) -> None:
        server, _ = _start(_PublicListHandler)
        client = self._client()
        sources: list[CustomWebSource] = []
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            for index, path in enumerate(("/empty-list", "/channel"), start=1):
                with self.subTest(path=path):
                    source = CustomWebSource(client, {
                        "id": f"aggregate_seed_{index}",
                        "start_urls": [f"{root}{path}"],
                        "auth": {"mode": "none"},
                    })
                    sources.append(source)
                    notices = list(source.iter_notices(
                        date(2026, 1, 1),
                        date(2026, 12, 31),
                        Coverage(source=source.name),
                    ))
                    self.assertEqual([], notices)
        finally:
            for source in sources:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_required_terms_match_detail_body_or_url_after_discovery(self) -> None:
        server, _ = _start(_PublicListHandler)
        client = self._client()
        sources: list[CustomWebSource] = []
        try:
            root = f"http://127.0.0.1:{server.server_port}"
            for source_id, required_term, expected_suffix in (
                ("regional_body", "吉林", "/detail/123456"),
                ("regional_url", "654321", "/detail/654321"),
            ):
                with self.subTest(required_term=required_term):
                    source = CustomWebSource(client, {
                        "id": source_id,
                        "start_urls": [f"{root}/regional-list"],
                        "required_terms": [required_term],
                        "auth": {"mode": "none"},
                    })
                    sources.append(source)
                    notices = list(source.iter_notices(
                        date(2026, 1, 1),
                        date(2026, 12, 31),
                        Coverage(source=source.name),
                    ))
                    self.assertEqual(1, len(notices))
                    self.assertTrue(notices[0].url.endswith(expected_suffix))
        finally:
            for source in sources:
                source.close()
            client.close()
            server.shutdown()
            server.server_close()

    def test_required_terms_ignore_global_navigation_link_text(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "regional_navigation_guard",
            "start_urls": ["https://example.test/notice/123456"],
            "required_terms": ["吉林"],
            "auth": {"mode": "none"},
        })
        result = HttpResult(
            "https://example.test/notice/123456",
            200,
            {"content-type": "text/html; charset=utf-8"},
            (
                '<html><head><title>北京市机房服务采购公告</title></head><body>'
                '<h1>北京市机房服务采购公告</h1><p>发布时间：2026-08-17</p>'
                '<p>项目名称：机房服务</p><p>采购人：北京测试单位</p>'
                '<footer><a href="/regions/jilin">吉林招标导航</a></footer>'
                '</body></html>'
            ).encode(),
        )
        try:
            notice = source._notice_from_result(
                result,
                label="",
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                explicit_seed=True,
            )
        finally:
            source.close()
            client.close()

        self.assertIsNone(notice)

    def test_all_challenged_seed_pages_are_blocked_and_request_manual_verification(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "all_challenged_seeds",
            "name": "需要验证的测试站",
            "start_urls": [
                "https://example.test/seed-one",
                "https://example.test/seed-two",
            ],
            "required_terms": ["吉林"],
            "auth": {"mode": "none"},
        })

        def challenge_result(url: str, **_kwargs) -> HttpResult:
            body = (
                "<html><head><title>访问验证</title></head><body>"
                "<h1>安全验证</h1><p>请完成验证码后继续访问</p>"
                "</body></html>"
            ).encode()
            return HttpResult(
                url,
                200,
                {"content-type": "text/html; charset=utf-8"},
                body,
            )

        coverage = Coverage(source=source.name)
        try:
            with patch.object(source.client, "request", side_effect=challenge_result):
                with self.assertLogs(
                    "tender_downloader.sources.custom_web", level="WARNING"
                ) as captured:
                    notices = list(source.iter_notices(
                        date(2026, 1, 1), date(2026, 12, 31), coverage
                    ))
        finally:
            source.close()
            client.close()

        self.assertEqual([], notices)
        self.assertEqual(2, coverage.pages)
        self.assertEqual("blocked", coverage.status)
        self.assertEqual(1, coverage.blocked)
        self.assertIn("全部 2 个配置种子页", coverage.message)
        self.assertIn("人工验证后重试", coverage.message)
        self.assertTrue(any("人工验证后重试" in line for line in captured.output))

    def test_one_challenged_seed_does_not_block_a_valid_seed(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "mixed_seed_results",
            "start_urls": [
                "https://example.test/challenge",
                "https://example.test/notice/123456",
            ],
            "required_terms": ["吉林"],
            "auth": {"mode": "none"},
        })

        def mixed_result(url: str, **_kwargs) -> HttpResult:
            if url.endswith("/challenge"):
                body = "<html><body><h1>访问验证</h1>请输入验证码</body></html>".encode()
            else:
                body = NOTICE_HTML
            return HttpResult(
                url,
                200,
                {"content-type": "text/html; charset=utf-8"},
                body,
            )

        coverage = Coverage(source=source.name)
        try:
            with patch.object(source.client, "request", side_effect=mixed_result):
                notices = list(source.iter_notices(
                    date(2026, 1, 1), date(2026, 12, 31), coverage
                ))
        finally:
            source.close()
            client.close()

        self.assertEqual(1, len(notices))
        self.assertEqual("partial", coverage.status)
        self.assertEqual(0, coverage.blocked)
        self.assertIn("页面仍为验证码或访问验证页 1 页", coverage.message)

    def test_security_validation_phrase_in_notice_is_not_access_challenge(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "security_validation_notice",
            "start_urls": ["https://example.test/notice/789012"],
            "required_terms": ["吉林"],
            "auth": {"mode": "none"},
        })
        body = (
            "<html><head><title>网络安全验证服务采购公告</title></head><body>"
            "<h1>网络安全验证服务采购公告</h1>"
            "<p>发布时间：2026-08-20</p><p>项目名称：网络安全验证服务</p>"
            "<p>采购人：吉林测试单位</p></body></html>"
        ).encode()
        result = HttpResult(
            "https://example.test/notice/789012",
            200,
            {"content-type": "text/html; charset=utf-8"},
            body,
        )
        coverage = Coverage(source=source.name)
        try:
            with patch.object(source.client, "request", return_value=result):
                notices = list(source.iter_notices(
                    date(2026, 1, 1), date(2026, 12, 31), coverage
                ))
        finally:
            source.close()
            client.close()

        self.assertEqual(1, len(notices))
        self.assertEqual("partial", coverage.status)
        self.assertEqual(0, coverage.blocked)

    def test_rendered_analysis_drives_notice_and_artifacts_without_replacing_body(self) -> None:
        client = self._client()
        source = CustomWebSource(client, {
            "id": "rendered_detail",
            "start_urls": ["https://example.test/notice/123456"],
            "required_terms": ["吉林"],
            "auth": {"mode": "none"},
        })
        raw_shell = b"<html><body><div id='app'></div></body></html>"
        result = HttpResult(
            "https://example.test/notice/123456",
            200,
            {"content-type": "text/html; charset=utf-8"},
            raw_shell,
            analysis_body=NOTICE_HTML,
        )
        seed_notice = Notice(
            source=source.name,
            authority_rank=50,
            external_id="rendered-seed",
            title="待解析",
            published_at="2026-08-18",
            url=result.url,
        )
        try:
            source._prepared = True
            with patch.object(source.client, "request", return_value=result):
                detail = source.fetch_detail(seed_notice)
            notice = source._notice_from_result(
                result,
                label="",
                start=date(2026, 1, 1),
                end=date(2026, 12, 31),
                explicit_seed=True,
            )
            artifacts = source.discover_artifacts(seed_notice, detail)
        finally:
            source.close()

        self.assertEqual(raw_shell, detail.body)
        self.assertEqual(NOTICE_HTML, detail.analysis_body)
        self.assertIsNotNone(notice)
        self.assertEqual("网络安全服务采购公告", notice.title)
        self.assertEqual("2026-08-18", notice.published_at)
        self.assertEqual(
            ["https://example.test/files/source.pdf"],
            [artifact.url for artifact in artifacts],
        )

    def test_basic_auth_is_scoped_and_removed_on_cross_origin_redirect(self) -> None:
        target, _ = _start(_RedirectTargetHandler)
        source_server, _ = _start(_RedirectSourceHandler)
        client = self._client()
        try:
            target_url = f"http://127.0.0.1:{target.server_port}/notice"
            _RedirectSourceHandler.target_url = target_url
            start_url = f"http://127.0.0.1:{source_server.server_port}/start"
            source = CustomWebSource(client, {
                "id": "basic_site",
                "start_urls": [start_url],
                "auth": {"mode": "basic"},
            })
            credentials = json.dumps({
                "basic_site": {"username": "bob", "password": "s3cret"}
            })
            with patch.dict(os.environ, {"TENDER_SOURCE_CREDENTIALS_JSON": credentials}):
                notices = list(source.iter_notices(
                    date(2026, 1, 1), date(2026, 12, 31), Coverage(source=source.name)
                ))
            expected = "Basic " + base64.b64encode(b"bob:s3cret").decode()
            self.assertEqual(expected, _RedirectSourceHandler.authorization)
            self.assertEqual("", _RedirectTargetHandler.authorization)
            self.assertEqual("", _RedirectTargetHandler.cookie)
            self.assertEqual(1, len(notices))
            external_headers = source.artifact_headers(AttachmentRef(
                target_url,
                referer=f"{start_url}?session_token=must-not-leak",
            ))
            self.assertNotIn("Authorization", external_headers)
            self.assertNotIn("Referer", external_headers)
            self.assertEqual("", external_headers["Cookie"])

            _RedirectTargetHandler.authorization = "not-set"
            client.request(start_url, headers={"X-Goog-API-Key": "must-not-leak"})
            self.assertEqual("", _RedirectTargetHandler.x_api_key)
        finally:
            client.close()
            source_server.shutdown()
            source_server.server_close()
            target.shutdown()
            target.server_close()


if __name__ == "__main__":
    unittest.main()
