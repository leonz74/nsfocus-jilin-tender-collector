from __future__ import annotations

import copy
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tender_downloader.webui.server import (
    MAX_REQUEST_BYTES,
    ProcessManager,
    SOURCE_CREDENTIALS_ENV,
    WebUIAlreadyRunningError,
    create_server,
)


class WebUITests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "config.json"
        self.config = self._valid_config()
        self.config_path.write_text(
            json.dumps(self.config, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        self.server, self.app = create_server(self.config_path, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.host = str(self.server.server_address[0])
        self.port = int(self.server.server_address[1])

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary_directory.cleanup()

    def _valid_config(self) -> dict[str, object]:
        return {
            "start_date": "2026-01-01",
            "end_date": "2026-08-20",
            "output_dir": "output",
            "database": "output/state.sqlite3",
            "http": {
                "user_agent": "TenderWebUITest/1.0 (+mailto:test@example.com)",
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
                    "type": "jilin_ggzy",
                    "name": "fixture-official-source",
                    "enabled": True,
                    "authority_rank": 100,
                    "page_size": 10,
                    "max_pages_per_channel": 1,
                }
            ],
            "ai": {
                "enabled": False,
                "endpoint": "https://example.invalid/v1/chat/completions",
                "model": "unused-test-model",
                "api_key_env": "TENDER_WEBUI_TEST_KEY",
                "auto_accept_threshold": 0.85,
                "second_review_threshold": 0.60,
                "reclassify": False,
                "timeout_seconds": 5,
            },
            "recall": {"mode": "p0_complete"},
        }

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        headers: dict[str, str] | None = None,
        csrf: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        body = None
        request_headers = dict(headers or {})
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if csrf:
            request_headers["X-CSRF-Token"] = self.app.csrf_token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {key.lower(): value for key, value in response.getheaders()}
            return response.status, response_headers, response_body
        finally:
            connection.close()

    def _json_request(self, *args: object, **kwargs: object) -> tuple[int, dict[str, object]]:
        status, _, body = self._request(*args, **kwargs)
        return status, json.loads(body.decode("utf-8"))

    def _wait_for_runner(self, runner: ProcessManager, timeout: float = 10) -> dict[str, object]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = runner.status()
            if not status["running"] and status["state"] not in {"idle", "starting"}:
                return status
            time.sleep(0.025)
        self.fail(f"子进程未在 {timeout} 秒内结束：{runner.status()!r}")

    def test_create_server_binds_only_ipv4_loopback(self) -> None:
        self.assertEqual("127.0.0.1", self.server.server_address[0])
        self.assertEqual("127.0.0.1", self.server.socket.getsockname()[0])

    def test_second_dashboard_cannot_bind_the_same_live_port(self) -> None:
        with self.assertRaisesRegex(
            WebUIAlreadyRunningError,
            rf"端口 {self.port} 已被占用",
        ):
            create_server(self.config_path, port=self.port)

    def test_sample_api_needs_no_ai_key_and_preserves_saved_config(self) -> None:
        self.config["ai"]["enabled"] = True
        self.config_path.write_text(json.dumps(self.config),encoding="utf-8")
        before = self.config_path.read_bytes()
        with patch.object(self.app.runner,"start") as start:
            status, result = self._json_request("POST","/api/sample",{},csrf=True)
            self.assertEqual(202,status)
            self.assertTrue(result["ok"])
            self.assertEqual("sample",start.call_args.args[0])
            self.assertEqual("",start.call_args.kwargs["api_key_env"])
            self.assertEqual({},start.call_args.kwargs["source_credentials"])
        self.assertEqual(before,self.config_path.read_bytes())
        self.assertEqual(403,self._request("POST","/api/sample",{})[0])

    def test_platform_query_leases_browser_session_and_releases_on_finish_and_failure(self):
        self.config["sources"].append({"type":"custom_web", "id":"okcis", "adapter":"okcis",
            "name":"导航网", "enabled":True, "source_role":"commercial_lead",
            "start_urls":["https://www.okcis.cn/search/"], "auth":{"mode":"browser",
            "login_url":"https://www.okcis.cn/login/", "check_url":"https://www.okcis.cn/search/"}})
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")
        criteria={"start_date":"2026-01-01","end_date":"2026-09-11","keyword":"网络安全"}
        sessions={"okcis":{"browser_transport":{"kind":"fixture"}}}
        with patch.object(self.app.browser_logins,"lease_for_sources",return_value=sessions) as lease, \
             patch.object(self.app.browser_logins,"release_leases") as release, \
             patch.object(self.app.runner,"start") as start:
            self.app.start_query(criteria)
            self.assertEqual(["okcis"],lease.call_args.args[0])
            self.assertEqual(sessions,start.call_args.kwargs["source_sessions"])
            start.call_args.kwargs["finished_callback"]()
            self.assertTrue(release.call_args.kwargs["restore"])
            self.assertEqual(lease.call_args.kwargs["lease_token"],release.call_args.kwargs["lease_token"])
            start.side_effect=RuntimeError("start failed")
            with self.assertRaisesRegex(RuntimeError,"start failed"):
                self.app.start_query(criteria)
            self.assertEqual(2,release.call_count)

    def test_query_never_starts_without_ready_browser_session(self):
        self.config["sources"].append({"type":"custom_web", "id":"okcis", "adapter":"okcis",
            "name":"导航网", "source_role":"commercial_lead", "start_urls":["https://www.okcis.cn/search/"],
            "auth":{"mode":"browser","login_url":"https://www.okcis.cn/login/"}})
        self.config_path.write_text(json.dumps(self.config),encoding="utf-8")
        with patch.object(self.app.browser_logins,"lease_for_sources",side_effect=ValueError("等待验证码")), \
             patch.object(self.app.runner,"start") as start:
            with self.assertRaisesRegex(ValueError,"等待验证码"):
                self.app.start_query({"start_date":"2026-01-01","end_date":"2026-09-11","keyword":"网络安全"})
            start.assert_not_called()

    def test_each_query_starts_again_and_saves_the_real_source_name(self) -> None:
        criteria = {"start_date": "2026-01-01", "end_date": "2026-09-11",
                    "industry": "金融", "keyword": "金融"}
        before = self.config_path.read_bytes()
        with patch.object(self.app.runner, "start") as start:
            status, first = self._json_request("POST", "/api/query", {"criteria": criteria}, csrf=True)
            self.assertEqual(202, status)
            status, second = self._json_request("POST", "/api/query", {"criteria": criteria}, csrf=True)
            self.assertEqual(202, status)
            self.assertNotEqual(first["query"]["id"], second["query"]["id"])
            self.assertEqual(2, start.call_count)
            self.assertEqual("query", start.call_args.args[0])
            self.assertEqual("", second["query"]["criteria"]["keyword"])
            self.assertEqual(["fixture-official-source"], second["query"]["sources"])
        self.assertEqual(before, self.config_path.read_bytes())
        self.assertEqual(403, self._request("POST", "/api/query", {"criteria": criteria})[0])
        status, result = self._json_request("POST", "/api/notices/export",
            {"query_id": first["query"]["id"], "notice_ids": ["old"]}, csrf=True)
        self.assertEqual(422, status)
        self.assertIn("查询结果已更新", result["error"])

    def test_empty_export_api_never_saves_a_header_only_file(self) -> None:
        for endpoint in ("/api/notices/export", "/api/notices/links"):
            for ids in ([], ["missing"]):
                status, result = self._json_request("POST", endpoint, {"notice_ids": ids}, csrf=True)
                self.assertEqual(422, status)
                self.assertIn("没有可导出", result["error"])
        self.assertFalse((self.root / "output" / "exports").exists())

    def test_export_saves_exact_filtered_rows_and_requires_csrf(self) -> None:
        import csv
        import io
        sample = {"pages": 1, "items": [
            {"notice_id": "one", "title": "所选中学项目", "industry": "教育"},
            {"notice_id": "two", "title": "未选其他项目", "industry": "金融"},
        ]}
        with patch.object(self.app, "list_notices", return_value=sample):
            status, _, body = self._request("POST", "/api/notices/export",
                {"notice_ids": ["one"]}, csrf=True)
        self.assertEqual(200, status)
        rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        self.assertEqual(["one"], [row["标讯ID"] for row in rows])
        saved = list((self.root / "output" / "exports").glob("*.csv"))
        self.assertEqual(1, len(saved))
        self.assertEqual(body, saved[0].read_bytes())
        self.assertEqual(403, self._request("POST", "/api/notices/export",
            {"notice_ids": ["one"]})[0])

    def test_export_feature_uses_query_scope_and_returns_every_attachment_with_counts(self) -> None:
        import csv
        import io
        from tender_downloader.database import Database
        from tender_downloader.http_client import HttpClient
        from tender_downloader.models import AttachmentRef, Notice
        query_id = "q" * 32
        db = Database(self.root / "output/state.sqlite3")
        notices = []
        for ident in ("files", "page", "old"):
            notice = Notice("fixture", 100, ident, f"测试采购-{ident}", "2026-06-01",
                f"https://www.jl.gov.cn/{ident}", buyer="测试采购单位",
                metadata={"query_id": query_id if ident != "old" else "previous"})
            db.upsert_notice(notice)
            notices.append(notice)
        for name in ("采购文件.pdf", "清单.xlsx"):
            db.record_artifact_ref(notices[0], AttachmentRef(
                "https://www.jl.gov.cn/files/" + name, original_name=name,
                access="login_required"), status="available")
        db.close()
        with patch.object(self.app, "query_status", return_value={"query": {"id": query_id}}), \
             patch.object(HttpClient, "request", side_effect=AssertionError("export must not download")):
            status, headers, body = self._request("POST", "/api/notices/export", {
                "query_id": query_id, "notice_ids": [n.identity for n in notices]}, csrf=True)
            self.assertEqual(200, status)
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
            self.assertEqual(3, len(rows))
            self.assertEqual({n.identity for n in notices[:2]}, {r["标讯ID"] for r in rows})
            self.assertEqual(("2", "3", "2"), (headers["x-export-notices"], headers["x-export-rows"], headers["x-export-url-rows"]))
            linked = [r for r in rows if r["下载URL"]]
            self.assertEqual({"采购文件.pdf", "清单.xlsx"}, {r["文件名称"] for r in linked})
            self.assertTrue(all("下载URL" in r for r in linked))
            self.assertTrue(all(r["客户名称"] == "测试采购单位" for r in rows))
            page = next(r for r in rows if not r["下载URL"])
            self.assertEqual("https://www.jl.gov.cn/page", page["原公告URL"])
            self.assertEqual("", page["下载URL"])
            saved = list((self.root / "output/exports").glob("*.csv"))
            self.assertEqual([body], [p.read_bytes() for p in saved])
            status, _, selected_body = self._request("POST", "/api/notices/export", {
                "query_id": query_id, "notice_ids": [notices[0].identity]}, csrf=True)
            self.assertEqual(200, status)
            self.assertEqual(2, len(list(csv.DictReader(io.StringIO(selected_body.decode("utf-8-sig"))))))

    def test_windows_cmd_launcher_is_ascii_and_uses_crlf(self) -> None:
        launcher = Path(__file__).resolve().parents[1] / "start-ui.cmd"
        raw = launcher.read_bytes()
        raw.decode("ascii")
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        text = raw.decode("ascii")
        self.assertIn("chcp 65001", text)
        self.assertIn("PYTHONUTF8=1", text)
        self.assertIn("%~dp0", text)

    def test_home_injects_csrf_without_embedding_configuration(self) -> None:
        canary = "HOME-MUST-NOT-LEAK-51db5e"
        configured = copy.deepcopy(self.config)
        configured["metadata"] = {"label": canary, "password": "legacy-secret"}
        self.config_path.write_text(
            json.dumps(configured, ensure_ascii=False),
            encoding="utf-8",
        )

        status, headers, body = self._request("GET", "/")
        html = body.decode("utf-8")

        self.assertEqual(200, status)
        self.assertIn("text/html", headers["content-type"])
        self.assertEqual("no-store", headers["cache-control"])
        self.assertNotIn("__CSRF_TOKEN__", html)
        match = re.search(r'<meta name="csrf-token" content="([^"]+)">', html)
        self.assertIsNotNone(match)
        self.assertEqual(self.app.csrf_token, match.group(1) if match else None)
        self.assertNotIn(canary, html)
        self.assertNotIn("legacy-secret", html)
        self.assertIn('id="ai-protocol"', html)
        self.assertIn('value="anthropic"', html)
        self.assertIn('value="gemini"', html)
        self.assertIn('id="add-custom-source"', html)

    def test_static_javascript_is_not_cached_across_service_restarts(self) -> None:
        status, headers, body = self._request("GET", "/static/app.js")

        self.assertEqual(200, status)
        self.assertIn("text/javascript", headers["content-type"])
        self.assertEqual("no-store", headers["cache-control"])
        self.assertTrue(body)

    def test_get_config_returns_configuration_but_strips_legacy_secrets(self) -> None:
        configured = copy.deepcopy(self.config)
        configured["metadata"] = {
            "label": "safe-label",
            "password": "legacy-secret",
            "nested": {"access_token": "legacy-token", "keep": True},
        }
        self.config_path.write_text(
            json.dumps(configured, ensure_ascii=False),
            encoding="utf-8",
        )

        status, result = self._json_request("GET", "/api/config")

        self.assertEqual(200, status)
        self.assertEqual("2026-01-01", result["start_date"])
        self.assertEqual("safe-label", result["metadata"]["label"])
        self.assertNotIn("password", result["metadata"])
        self.assertNotIn("access_token", result["metadata"]["nested"])
        self.assertTrue(result["metadata"]["nested"]["keep"])

    def test_ai_presets_are_available_without_credentials(self) -> None:
        status, result = self._json_request("GET", "/api/ai-presets")
        self.assertEqual(200, status)
        self.assertGreaterEqual(len(result["presets"]), 5)
        for preset in result["presets"]:
            self.assertNotIn("api_key", preset)
            self.assertNotIn("password", preset)
            self.assertNotIn("token", preset)
            self.assertIn("api_key_env", preset)

    def test_ai_connection_test_uses_transient_key_without_persisting_it(self) -> None:
        secret = "transient-ai-test-key-36df"
        configured = copy.deepcopy(self.config)
        configured["ai"]["enabled"] = True
        configured["ai"]["provider"] = "deepseek"
        configured["ai"]["protocol"] = "openai_compatible"
        configured["ai"]["endpoint"] = "https://api.deepseek.com/chat/completions"
        configured["ai"]["model"] = "deepseek-v4-flash"
        self.config_path.write_text(
            json.dumps(configured, ensure_ascii=False),
            encoding="utf-8",
        )
        original = self.config_path.read_bytes()

        def fake_probe(_client: object, ai: dict[str, object], key: str) -> dict[str, object]:
            self.assertEqual(secret, key)
            return {
                "ok": True,
                "provider": ai["provider"],
                "model": ai["model"],
                "latency_ms": 7,
            }

        with patch(
            "tender_downloader.webui.server.test_ai_connection",
            side_effect=fake_probe,
        ):
            status, result = self._json_request(
                "POST", "/api/ai-test", {"api_key": secret}, csrf=True
            )

        self.assertEqual(200, status)
        self.assertTrue(result["ok"])
        self.assertEqual("deepseek", result["provider"])
        self.assertEqual(7, result["latency_ms"])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(original, self.config_path.read_bytes())

    def test_ai_connection_test_uses_draft_ai_http_but_saved_sources(self) -> None:
        secret = "draft-ai-test-key-a8fd"
        original = self.config_path.read_bytes()
        draft = copy.deepcopy(self.config)
        draft["start_date"] = "not-a-date"
        draft["sources"] = [
            {
                "type": "custom_web",
                "id": "unfinished-source",
                "name": "未填写网址的网站草稿",
                "enabled": True,
                "start_urls": [],
            }
        ]
        draft["http"] = {
            **draft["http"],
            "user_agent": "TemporaryAITest/2.0",
            "timeout_seconds": 17,
            "max_retries": 1,
            "allow_benchmark_proxy_hosts": True,
        }
        draft["ai"] = {
            **draft["ai"],
            "enabled": True,
            "provider": "deepseek",
            "protocol": "openai_compatible",
            "endpoint": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-chat",
            "timeout_seconds": 23,
        }

        def fake_probe(client: object, ai: dict[str, object], key: str) -> dict[str, object]:
            self.assertEqual(secret, key)
            self.assertEqual("deepseek", ai["provider"])
            self.assertEqual("deepseek-chat", ai["model"])
            self.assertEqual("TemporaryAITest/2.0", client.user_agent)
            self.assertEqual(23, client.timeout_seconds)
            self.assertEqual(1, client.max_retries)
            self.assertTrue(client.allow_benchmark_proxy_hosts)
            return {
                "ok": True,
                "provider": ai["provider"],
                "model": ai["model"],
                "latency_ms": 9,
            }

        with patch(
            "tender_downloader.webui.server.test_ai_connection",
            side_effect=fake_probe,
        ):
            status, result = self._json_request(
                "POST",
                "/api/ai-test",
                {
                    "api_key": secret,
                    "ai": draft["ai"],
                    "http": draft["http"],
                    # A full unfinished page draft, if supplied by an older
                    # caller, must never replace the saved source context.
                    "config": draft,
                },
                csrf=True,
            )

        self.assertEqual(200, status)
        self.assertTrue(result["ok"])
        self.assertEqual("deepseek-chat", result["model"])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(original, self.config_path.read_bytes())

    def test_ai_connection_test_rejects_key_embedded_in_draft_config(self) -> None:
        embedded_secret = "must-not-echo-draft-secret-5ad4"
        original = self.config_path.read_bytes()
        temporary_ai = {
            **self.config["ai"],
            "enabled": True,
            "endpoint": "https://api.deepseek.com/chat/completions",
            "model": "deepseek-chat",
            "api_key": embedded_secret,
        }

        status, result = self._json_request(
            "POST",
            "/api/ai-test",
            {"api_key": "separate-key", "ai": temporary_ai},
            csrf=True,
        )

        self.assertEqual(422, status)
        self.assertFalse(result["ok"])
        self.assertIn("单独填写", result["error"])
        self.assertNotIn(embedded_secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(original, self.config_path.read_bytes())

    def test_write_without_csrf_is_forbidden_and_does_not_change_file(self) -> None:
        original = self.config_path.read_bytes()
        modified = copy.deepcopy(self.config)
        modified["end_date"] = "2026-08-19"

        status, result = self._json_request("PUT", "/api/config", modified)

        self.assertEqual(403, status)
        self.assertFalse(result["ok"])
        self.assertEqual(original, self.config_path.read_bytes())

    def test_malicious_host_and_cross_origin_are_rejected(self) -> None:
        host_status, host_result = self._json_request(
            "GET",
            "/api/config",
            headers={"Host": "attacker.example"},
        )
        origin_status, origin_result = self._json_request(
            "POST",
            "/api/validate",
            {},
            headers={"Origin": "https://attacker.example"},
            csrf=True,
        )

        self.assertEqual(400, host_status)
        self.assertIn("Host", host_result["error"])
        self.assertEqual(403, origin_status)
        self.assertIn("跨站", origin_result["error"])

    def test_put_atomically_saves_valid_configuration(self) -> None:
        modified = copy.deepcopy(self.config)
        modified["end_date"] = "2026-08-19"

        status, result = self._json_request(
            "PUT", "/api/config", {"config": modified}, csrf=True
        )

        self.assertEqual(200, status)
        self.assertTrue(result["ok"])
        saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        self.assertEqual("2026-08-19", saved["end_date"])
        self.assertEqual([], list(self.root.glob(f".{self.config_path.name}.*.tmp")))

    def test_browser_profile_binding_is_stable_and_not_returned_by_config_api(self) -> None:
        configured = copy.deepcopy(self.config)
        configured["sources"].append(
            {
                "type": "custom_web",
                "id": "legacy_portal",
                "name": "旧版登录门户",
                "enabled": True,
                "start_urls": ["https://portal.example.com/protected"],
                "auth": {
                    "mode": "browser",
                    "login_url": "https://portal.example.com/login",
                },
            }
        )
        self.config_path.write_text(
            json.dumps(configured, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        first_status, first_result = self._json_request(
            "PUT", "/api/config", configured, csrf=True
        )
        first_saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        first_profile_id = first_saved["sources"][-1]["auth"]["profile_id"]

        renamed = copy.deepcopy(configured)
        renamed["sources"][-1]["id"] = "renamed_portal"
        renamed["sources"][-1]["name"] = "更名后的登录门户"
        second_status, second_result = self._json_request(
            "PUT", "/api/config", renamed, csrf=True
        )
        second_saved = json.loads(self.config_path.read_text(encoding="utf-8"))
        get_status, public_config = self._json_request("GET", "/api/config")

        self.assertEqual(200, first_status)
        self.assertEqual("legacy_portal", first_profile_id)
        self.assertEqual(200, second_status)
        self.assertEqual(
            first_profile_id,
            second_saved["sources"][-1]["auth"]["profile_id"],
        )
        self.assertEqual(200, get_status)
        serialized = json.dumps(
            [first_result, second_result, public_config], ensure_ascii=False
        )
        self.assertNotIn("profile_id", serialized)

    def test_atomic_replace_failure_preserves_old_file_and_removes_temp_file(self) -> None:
        original = self.config_path.read_bytes()
        modified = copy.deepcopy(self.config)
        modified["end_date"] = "2026-08-19"

        with patch(
            "tender_downloader.webui.server.os.replace",
            side_effect=OSError("simulated replace failure"),
        ):
            status, result = self._json_request(
                "PUT", "/api/config", modified, csrf=True
            )

        self.assertEqual(500, status)
        self.assertFalse(result["ok"])
        self.assertEqual(original, self.config_path.read_bytes())
        self.assertEqual([], list(self.root.glob(f".{self.config_path.name}.*.tmp")))

    def test_plaintext_api_key_is_rejected_and_old_file_is_unchanged(self) -> None:
        original = self.config_path.read_bytes()
        modified = copy.deepcopy(self.config)
        modified["ai"]["api_key"] = "plaintext-key-must-not-be-saved"

        status, result = self._json_request(
            "PUT", "/api/config", modified, csrf=True
        )

        self.assertEqual(422, status)
        self.assertIn("明文密钥", result["error"])
        self.assertEqual(original, self.config_path.read_bytes())
        self.assertNotIn(b"plaintext-key-must-not-be-saved", self.config_path.read_bytes())

    def test_validate_accepts_ready_config_without_user_agent(self) -> None:
        valid_status, valid_result = self._json_request(
            "POST", "/api/validate", {"config": self.config}, csrf=True
        )
        without_user_agent = copy.deepcopy(self.config)
        del without_user_agent["http"]["user_agent"]
        default_status, default_result = self._json_request(
            "POST", "/api/validate", {"config": without_user_agent}, csrf=True
        )

        self.assertEqual(200, valid_status)
        self.assertTrue(valid_result["valid"])
        self.assertEqual(200, default_status)
        self.assertTrue(default_result["valid"])

    def test_request_body_larger_than_limit_is_rejected_before_body_read(self) -> None:
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.putrequest("POST", "/api/validate")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("X-CSRF-Token", self.app.csrf_token)
            connection.putheader("Content-Length", str(MAX_REQUEST_BYTES + 1))
            connection.endheaders()
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
        finally:
            connection.close()

        self.assertEqual(413, response.status)
        self.assertIn("过大", body["error"])

    def test_static_path_traversal_does_not_expose_files(self) -> None:
        for path in ("/../config.json", "/%2e%2e/config.json", "/static/../config.json"):
            with self.subTest(path=path):
                status, result = self._json_request("GET", path)
                self.assertEqual(404, status)
                self.assertFalse(result["ok"])

    def test_process_manager_runs_real_verify_and_reports_logs(self) -> None:
        manager = ProcessManager(self.config_path)
        try:
            manager.start("verify")
            status = self._wait_for_runner(manager)
        finally:
            manager.close()

        self.assertEqual("verify", status["operation"])
        self.assertEqual("failed", status["state"])
        self.assertEqual(1, status["exit_code"])
        messages = "\n".join(row["message"] for row in status["logs"])
        self.assertIn("开始校验原件哈希", messages)
        self.assertIn("已校验 0 个文件路径及官方来源门禁", messages)
        self.assertIn("没有当前有效的 HTTPS 官方原件", messages)
        self.assertIn("任务失败，退出码 1", messages)
        self.assertTrue((self.root / "output" / "state.sqlite3").exists())

    def test_api_key_is_child_env_only_and_never_appears_in_config_logs_or_argv(self) -> None:
        secret = "webui-secret-7ce1d1a1"
        env_name = "TENDER_WEBUI_ISOLATION_TEST_KEY"
        original_config = self.config_path.read_bytes()
        original_popen = subprocess.Popen
        captured: dict[str, object] = {}

        def intercept_popen(**kwargs: object) -> subprocess.Popen[str]:
            captured["original_args"] = copy.deepcopy(kwargs["args"])
            child_environment = kwargs["env"]
            self.assertIsInstance(child_environment, dict)
            captured["child_secret"] = child_environment.get(env_name)
            replacement = dict(kwargs)
            replacement["args"] = [
                sys.executable,
                "-u",
                "-c",
                f"import os; print(os.environ[{env_name!r}])",
            ]
            return original_popen(**replacement)

        os.environ.pop(env_name, None)
        manager = ProcessManager(self.config_path)
        try:
            with patch(
                "tender_downloader.webui.server.subprocess.Popen",
                side_effect=intercept_popen,
            ):
                manager.start("verify", api_key=secret, api_key_env=env_name)
                status = self._wait_for_runner(manager)
        finally:
            manager.close()
            os.environ.pop(env_name, None)

        serialized_status = json.dumps(status, ensure_ascii=False)
        self.assertEqual("completed", status["state"])
        self.assertEqual(secret, captured["child_secret"])
        self.assertNotIn(secret, repr(captured["original_args"]))
        self.assertNotIn(secret, serialized_status)
        self.assertIn("[已隐藏密钥]", serialized_status)
        self.assertEqual(original_config, self.config_path.read_bytes())
        self.assertNotIn(secret.encode("utf-8"), self.config_path.read_bytes())
        self.assertNotIn(env_name, os.environ)

    def test_inherited_ai_key_is_redacted_from_child_output(self) -> None:
        secret = "inherited-ai-key-must-be-redacted-1e8f"
        env_name = "TENDER_WEBUI_INHERITED_TEST_KEY"
        original_popen = subprocess.Popen

        def intercept_popen(**kwargs: object) -> subprocess.Popen[str]:
            replacement = dict(kwargs)
            replacement["args"] = [
                sys.executable,
                "-u",
                "-c",
                f"import os; print(os.environ[{env_name!r}])",
            ]
            return original_popen(**replacement)

        os.environ[env_name] = secret
        manager = ProcessManager(self.config_path)
        try:
            with patch(
                "tender_downloader.webui.server.subprocess.Popen",
                side_effect=intercept_popen,
            ):
                manager.start("verify", api_key_env=env_name)
                status = self._wait_for_runner(manager)
        finally:
            manager.close()
            os.environ.pop(env_name, None)

        rendered = json.dumps(status, ensure_ascii=False)
        self.assertNotIn(secret, rendered)
        self.assertIn("[已隐藏密钥]", rendered)

    def test_source_login_is_child_env_only_and_redacted_from_logs(self) -> None:
        username = "portal-user-44c2"
        password = "portal-password-89d7"
        credentials = {
            "hospital_portal": {"username": username, "password": password}
        }
        original_popen = subprocess.Popen
        captured: dict[str, object] = {}

        def intercept_popen(**kwargs: object) -> subprocess.Popen[str]:
            child_environment = kwargs["env"]
            self.assertIsInstance(child_environment, dict)
            captured["credential_json"] = child_environment.get(SOURCE_CREDENTIALS_ENV)
            captured["original_args"] = copy.deepcopy(kwargs["args"])
            replacement = dict(kwargs)
            replacement["args"] = [
                sys.executable,
                "-u",
                "-c",
                f"import os; print(os.environ[{SOURCE_CREDENTIALS_ENV!r}])",
            ]
            return original_popen(**replacement)

        os.environ.pop(SOURCE_CREDENTIALS_ENV, None)
        manager = ProcessManager(self.config_path)
        try:
            with patch(
                "tender_downloader.webui.server.subprocess.Popen",
                side_effect=intercept_popen,
            ):
                manager.start("verify", source_credentials=credentials)
                status = self._wait_for_runner(manager)
        finally:
            manager.close()
            os.environ.pop(SOURCE_CREDENTIALS_ENV, None)

        credential_json = str(captured["credential_json"])
        status_json = json.dumps(status, ensure_ascii=False)
        self.assertIn(username, credential_json)
        self.assertIn(password, credential_json)
        self.assertNotIn(username, repr(captured["original_args"]))
        self.assertNotIn(password, repr(captured["original_args"]))
        self.assertNotIn(username, status_json)
        self.assertNotIn(password, status_json)
        self.assertIn("[已隐藏密钥]", status_json)
        self.assertNotIn(SOURCE_CREDENTIALS_ENV, os.environ)


if __name__ == "__main__":
    unittest.main()
