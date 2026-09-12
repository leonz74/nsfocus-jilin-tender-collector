from __future__ import annotations

import hashlib
import csv
import http.client
import io
import json
import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tender_downloader.config import AppConfig, validate_config_data
from tender_downloader.content_gate import ContentGatePolicy
from tender_downloader.database import Database
from tender_downloader.field_extract import ExtractedFields
from tender_downloader.http_client import HttpClient
from tender_downloader.models import Classification, Coverage, Notice
from tender_downloader.official_registry import SourceRole
from tender_downloader.pipeline import Pipeline
from tender_downloader.sources.base import SourceAdapter
from tender_downloader.sources.custom_web import CustomWebSource
from tender_downloader.storage import ImmutableStore
from tender_downloader.webui.server import ConfigWebApp, AlreadyRunningError, create_server
from tender_downloader.query import query_state_path


NOTICE_BODY = (
    "<html><body><h1>长春市医院网络安全服务项目</h1>"
    "<p>公告类别：成交公告</p><p>采购人：长春市人民医院</p>"
    "<p>采购人联系人：张三</p><p>采购人联系电话：0431-88886666</p>"
    "<p>本项目为医院采购网络安全等级保护测评、安全运营和应急响应服务。</p>"
    "<p>项目编号：JL-CATALOG-001</p><p>成交金额：82.46万元</p>"
    "<p>成交供应商：长春雅信科技有限责任公司</p>"
    '<a href="/tender.pdf">附件：采购文件.pdf</a>'
    "</body></html>"
).encode("utf-8")
PDF_BYTES = b"%PDF-1.4\nmetadata-catalog-original-bytes\n%%EOF"


class _CatalogHandler(BaseHTTPRequestHandler):
    notice_requests = 0
    attachment_requests = 0
    attachment_started = None
    attachment_release = None

    def do_GET(self) -> None:  # noqa: N802
        if self.path in {"/notice.html", "/second.html"}:
            type(self).notice_requests += 1
            body = NOTICE_BODY
            content_type = "text/html; charset=utf-8"
        elif self.path == "/tender.pdf":
            type(self).attachment_requests += 1
            if type(self).attachment_started is not None:
                type(self).attachment_started.set()
                if not type(self).attachment_release.wait(10):
                    self.send_error(504)
                    return
            if not self.headers.get("Referer", "").endswith("/notice.html"):
                self.send_error(403)
                return
            body = PDF_BYTES
            content_type = "application/pdf"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        if self.path.endswith(".pdf"):
            self.send_header(
                "Content-Disposition", 'attachment; filename="tender.pdf"'
            )
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _CatalogSource(SourceAdapter):
    display_name = "catalog-fixture"

    def __init__(self, client: HttpClient, url: str) -> None:
        super().__init__(client, {"authority_rank": 100})
        self.url = url

    def iter_notices(self, start: date, end: date, coverage: Coverage):
        coverage.pages += 1
        coverage.notices += 1
        coverage.reached_start = True
        coverage.reached_end = True
        yield Notice(
            source=self.name,
            authority_rank=100,
            external_id="JL-CATALOG-001",
            title="长春市医院网络安全服务项目",
            published_at="2026-08-18",
            url=self.url,
            region="长春市",
            buyer="长春市人民医院",
            notice_type="成交公告",
        )


class _CatalogAI:
    def classify(
        self, notice: Notice, text: str, *, stage: str = "final"
    ) -> Classification:
        return Classification(
            relevant=True,
            confidence=0.98,
            industry="医疗卫生",
            security_categories=("安全运营与运维",),
            evidence=("网络安全等级保护测评、安全运营和应急响应服务",),
            reason="公告正文明确为网络安全服务",
            method=f"fixture-ai:{stage}",
            ai_confirmed=True,
            needs_review=False,
        )


class _LocalOfficialCatalogPipeline(Pipeline):
    """单元测试专用：只将本测试的回环 HTTP 精确主机视为官方。"""

    def _role_for_url(self, url: str) -> SourceRole:
        if url.startswith("http://127.0.0.1:"):
            return SourceRole.OFFICIAL
        return super()._role_for_url(url)

    def _official_notice_source_allowed(self, url: str) -> bool:
        if url.startswith("http://127.0.0.1:"):
            return True
        return super()._official_notice_source_allowed(url)

    def _official_attachment_source_allowed(
        self, attachment_url: str, official_notice_url: str
    ) -> bool:
        if attachment_url.startswith("http://127.0.0.1:"):
            return True
        return super()._official_attachment_source_allowed(
            attachment_url, official_notice_url
        )

    def _gate_policy(self, origin: str, final_url: str) -> ContentGatePolicy:
        base = super()._gate_policy(origin, final_url)
        return ContentGatePolicy(
            official_hosts=base.official_hosts | {"127.0.0.1"},
            allowed_redirect_pairs=base.allowed_redirect_pairs,
            minimum_html_text_chars=base.minimum_html_text_chars,
            insecure_test_hosts={"127.0.0.1"},
        )


class MetadataCatalogWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        _CatalogHandler.notice_requests = 0
        _CatalogHandler.attachment_requests = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _CatalogHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config = AppConfig(
            self.root / "config.json",
            {
                "start_date": "2026-01-01",
                "end_date": "2026-08-21",
                "output_dir": str(self.root / "output"),
                "database": str(self.root / "output" / "state.sqlite3"),
                "sources": [],
                "http": {"allow_private_hosts": True},
                "ai": {
                    "auto_accept_threshold": 0.85,
                    "second_review_threshold": 0.60,
                },
                "recall": {"mode": "p0_complete"},
                "delivery": {
                    "mode": "on_demand",
                    "include_notice_html_when_no_attachment": True,
                    "include_official_notice_with_attachments": True,
                },
            },
        )
        self.client = HttpClient(
            user_agent="catalog-fixture/1.0",
            timeout_seconds=5,
            delay_seconds=0,
            max_retries=0,
            allow_private_hosts=True,
        )
        self.db = Database(self.config.database_path)
        self.pipeline = _LocalOfficialCatalogPipeline(
            config=self.config,
            client=self.client,
            db=self.db,
            store=ImmutableStore(self.config.output_dir),
            classifier=_CatalogAI(),
            sources=[
                _CatalogSource(
                    self.client,
                    f"http://127.0.0.1:{self.server.server_port}/notice.html",
                )
            ],
        )

    def tearDown(self) -> None:
        self.db.close()
        self.client.close()
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.temporary_directory.cleanup()

    @staticmethod
    def _files_below(path: Path) -> list[Path]:
        return [candidate for candidate in path.rglob("*") if candidate.is_file()]

    def test_repeated_platform_query_fetches_fresh_details_before_publishing_rows(self) -> None:
        source = self.pipeline.sources[0]
        fetch = source.fetch_detail
        for query_id in ("a" * 32, "b" * 32):
            self.config.data["_query"] = {"id": query_id}
            def fresh_detail(notice):
                self.assertEqual(0, self.db.list_notices(query_id=query_id)["total"])
                return fetch(notice)
            with patch.object(source, "fetch_detail", side_effect=fresh_detail):
                self.pipeline.run()
            self.assertEqual(1, self.db.list_notices(query_id=query_id)["total"])
        self.assertEqual(2, _CatalogHandler.notice_requests)
        self.assertEqual(0, _CatalogHandler.attachment_requests)
        self.assertEqual(0, self.db.list_notices(query_id="a" * 32)["total"])

    def test_delivery_mode_validation_and_safe_default(self) -> None:
        base = {
            "start_date": "2026-01-01",
            "end_date": "2026-08-21",
            "sources": [
                {
                    "type": "jilin_ggzy",
                    "enabled": True,
                    "authority_rank": 100,
                }
            ],
        }
        # Missing delivery.mode is the security-sensitive default: collecting
        # metadata must not unexpectedly download files.
        validate_config_data(base)
        for mode in ("on_demand", "automatic"):
            candidate = {**base, "delivery": {"mode": mode}}
            validate_config_data(candidate)
        with self.assertRaisesRegex(ValueError, "delivery.mode"):
            validate_config_data({**base, "delivery": {"mode": "eager-ish"}})

    def test_download_while_query_continues_and_catalog_export_stays_available(self) -> None:
        query_id = "c" * 32
        self.config.data["_query"] = {"id": query_id}
        self.config.data["http"].update(delay_seconds=0, max_retries=0, timeout_seconds=5)
        self.config.path.write_text(json.dumps(self.config.data), encoding="utf-8")
        query_state_path(self.config).write_text(json.dumps({"id": query_id}), encoding="utf-8")
        first_ready, attachment_started, attachment_release = (
            threading.Event(), threading.Event(), threading.Event())
        second_ready = threading.Event()
        _CatalogHandler.attachment_started = attachment_started
        _CatalogHandler.attachment_release = attachment_release
        errors, downloaded = [], []
        url = f"http://127.0.0.1:{self.server.server_port}/notice.html"
        notice_id = "catalog-fixture:JL-CATALOG-001"

        class ContinuingSource(_CatalogSource):
            def iter_notices(source, start, end, coverage):
                yield from super().iter_notices(start, end, coverage)
                first_ready.set()  # First detail has been committed and published.
                if not attachment_started.wait(8):
                    raise AssertionError("download never reached attachment response")
                coverage.notices += 1
                yield Notice(source=source.name, authority_rank=100,
                             external_id="SECOND", title="长春市医院安全运营项目", 
                             published_at="2026-08-19", url=url.replace("notice", "second"))
                second_ready.set()

        def collect():
            db = Database(self.config.database_path)
            try:
                pipeline = _LocalOfficialCatalogPipeline(
                    config=self.config, client=self.client, db=db,
                    store=ImmutableStore(self.config.output_dir), classifier=_CatalogAI(),
                    sources=[ContinuingSource(self.client, url)])
                pipeline.run()
            except Exception as exc:
                errors.append(exc)
            finally:
                db.close()

        app = ConfigWebApp(self.config.path)
        def download():
            try:
                downloaded.extend(app.download_notices([notice_id]))
            except Exception as exc:
                errors.append(exc)

        collector = threading.Thread(target=collect)
        downloader = threading.Thread(target=download)
        try:
            with patch.object(app.runner, "status", return_value={"running": True, "operation": "query"}), \
                 patch("tender_downloader.webui.server.load_config", return_value=self.config), \
                 patch("tender_downloader.webui.server.Pipeline", _LocalOfficialCatalogPipeline), \
                 patch("tender_downloader.webui.server.build_sources", side_effect=
                       lambda client, *_: [_CatalogSource(client, url)]):
                collector.start()
                self.assertTrue(first_ready.wait(5))
                with self.assertRaisesRegex(AlreadyRunningError, "本轮已完成解析"):
                    app.download_notices(["catalog-fixture:SECOND"])
                downloader.start()
                self.assertTrue(attachment_started.wait(5))
                self.assertTrue(second_ready.wait(5), "collection must advance during download")
                rows = app.list_notices({"query_id": [query_id]})
                self.assertEqual(2, rows["total"])
                self.assertEqual("downloading", next(row["download_status"] for row in
                                 rows["items"] if row["identity"] == notice_id))
                self.assertEqual({notice_id: "downloading"}, app.status()["active_downloads"])
                exported = list(csv.DictReader(io.StringIO(app.export_notices(
                    {"query_id": [query_id]}).decode("utf-8-sig"))))
                self.assertEqual(2, len({row["标讯ID"] for row in exported}))
                with self.assertRaisesRegex(AlreadyRunningError, "重复提交"):
                    app.download_notices([notice_id])
                with patch("tender_downloader.webui.server.validate_config_data"), \
                     self.assertRaisesRegex(AlreadyRunningError, "新的查询"):
                    app.start_query({"start_date": "2026-01-01", "end_date": "2026-08-21"})
                attachment_release.set()
                downloader.join(8)
                collector.join(8)
                self.assertFalse(downloader.is_alive())
                self.assertFalse(collector.is_alive())
                self.assertEqual([], errors)
                self.assertEqual({}, app.status()["active_downloads"])
                pdf = next(file for file in downloaded[0]["files"] if file["kind"] == "attachment")
                self.assertEqual(PDF_BYTES, Path(pdf["path"]).read_bytes())
                self.assertEqual(hashlib.sha256(PDF_BYTES).hexdigest(), pdf["sha256"])
                self.assertEqual(query_id, self.db.get_notice(notice_id).metadata["query_id"])
                self.assertEqual(2, app.list_notices({"query_id": [query_id]})["total"])
        finally:
            attachment_release.set()
            if collector.ident: collector.join(10)
            if downloader.ident: downloader.join(10)
            _CatalogHandler.attachment_started = None
            _CatalogHandler.attachment_release = None
            app.close()

    def test_download_remains_blocked_during_legacy_collection_or_verification(self) -> None:
        app = ConfigWebApp(self.config.path)
        try:
            for operation in ("run", "sample", "verify"):
                with patch.object(app.runner, "status", return_value={"running": True, "operation": operation}):
                    with self.assertRaises(AlreadyRunningError):
                        app.download_notices(["some:notice"])
                    self.assertEqual({}, app.status()["active_downloads"])
        finally:
            app.close()

    def test_default_metadata_run_records_catalog_but_downloads_no_files(self) -> None:
        summary = self.pipeline.run()

        self.assertEqual(1, summary["notices"])
        self.assertEqual(1, _CatalogHandler.notice_requests)
        self.assertEqual(0, _CatalogHandler.attachment_requests)
        self.assertEqual([], self.db.artifact_rows())
        self.assertEqual([], self._files_below(self.config.output_dir / "raw"))
        self.assertEqual([], self._files_below(self.config.output_dir / "delivery_verified"))
        self.assertEqual([], self._files_below(self.config.output_dir / "requested_downloads"))

        refs = self.db.artifact_ref_rows()
        self.assertEqual(1, len(refs))
        self.assertTrue(str(refs[0]["source_url"]).endswith("/tender.pdf"))
        self.assertIn(str(refs[0]["status"]), {"available", "discovered"})

        rows = self.db.current_result_rows()
        self.assertEqual(1, len(rows))
        row = dict(rows[0])
        self.assertEqual(1, row["relevant"])
        self.assertEqual(82_460_000, row["award_amount_minor"])
        self.assertEqual("长春雅信科技有限责任公司", row["winning_vendor"])
        self.assertEqual("医疗卫生", row["industry"])
        self.assertEqual("张三", row["buyer_contact"])
        self.assertEqual("0431-88886666", row["buyer_phone"])
        self.assertEqual("长春", row["city"])
        self.assertEqual(0, row["verified_file_count"])

    def test_on_demand_download_preserves_bytes_and_is_idempotent(self) -> None:
        self.pipeline.run()
        notice_id = "catalog-fixture:JL-CATALOG-001"

        first = self.pipeline.download_notices([notice_id])
        first_files = first[0]["files"]
        self.assertNotEqual("failed", first[0]["status"])
        self.assertGreaterEqual(len(first_files), 2)  # 公告原响应 + 附件
        pdf = next(
            item
            for item in first_files
            if str(item["source_url"]).endswith("/tender.pdf")
        )
        self.assertEqual(hashlib.sha256(PDF_BYTES).hexdigest(), pdf["sha256"])
        self.assertEqual(PDF_BYTES, Path(str(pdf["path"])).read_bytes())
        saved_notice = self.db.get_notice(notice_id)
        saved_notice.metadata["download_error"] = "another attachment failed"
        self.db.upsert_notice(saved_notice)
        from tender_downloader.export import download_links_csv
        import io
        links = list(csv.DictReader(io.StringIO(download_links_csv(self.db).decode("utf-8-sig"))))
        self.assertEqual("已下载", links[0]["下载状态"])
        self.assertEqual("", links[0]["失败或待办原因"])

        delivered_before = {
            path.resolve(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self._files_below(self.config.output_dir / "requested_downloads")
        }
        self.assertGreaterEqual(len(delivered_before), 2)
        second = self.pipeline.download_notices([notice_id])
        delivered_after = {
            path.resolve(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in self._files_below(self.config.output_dir / "requested_downloads")
        }

        self.assertNotEqual("failed", second[0]["status"])
        self.assertEqual(delivered_before, delivered_after)
        self.assertEqual(
            hashlib.sha256(PDF_BYTES).hexdigest(),
            next(
                item["sha256"]
                for item in second[0]["files"]
                if str(item["source_url"]).endswith("/tender.pdf")
            ),
        )

        with self.assertLogs("tender_downloader.pipeline", level="ERROR"):
            batch = self.pipeline.download_notices(
                [notice_id, "missing-source:NOT-FOUND"],
                include_notice=False,
                include_attachments=True,
            )
        self.assertEqual(["downloaded", "failed"], [item["status"] for item in batch])
        self.assertIn("未找到标讯", " ".join(batch[1]["errors"]))

    def test_download_reuses_source_auth_for_notice_and_attachment(self) -> None:
        original = _CatalogHandler.do_GET
        for mode in ("basic", "browser"):
            with self.subTest(mode=mode):
                def protected(handler):
                    permitted = (handler.headers.get("Authorization") == "Basic dTpw" if mode == "basic"
                                 else handler.headers.get("Cookie") == "session=fixture")
                    if not permitted:
                        handler.send_error(401)
                        return
                    original(handler)

                root_url = f"http://127.0.0.1:{self.server.server_port}"
                url = root_url + "/notice.html"
                source = CustomWebSource(self.client, {
                    "id": "portal", "name": "authenticated-" + mode, "start_urls": [url],
                    "auth": {"mode": mode, "login_url": root_url + "/login"},
                })
                if mode == "basic":
                    source.runtime_credentials = {"username": "u", "password": "p"}
                else:
                    source.runtime_browser_session = {"cookies": [{
                        "name": "session", "value": "fixture", "domain": "127.0.0.1",
                        "path": "/", "secure": False, "httpOnly": True,
                    }]}
                notice = Notice(source=source.name, authority_rank=100, external_id="AUTH",
                                title="长春市医院网络安全服务项目", published_at="2026-09-10", url=url, publication_url=url,
                                source_role="official", origin_verified=True)
                self.db.upsert_notice(notice, status="review")
                pipeline = _LocalOfficialCatalogPipeline(
                    config=self.config, client=self.client, db=self.db,
                    store=self.pipeline.store, classifier=_CatalogAI(), sources=[source],
                )
                try:
                    with patch.object(_CatalogHandler, "do_GET", protected):
                        results = pipeline.download_notices([notice.identity])
                    self.assertEqual("downloaded", results[0]["status"], results[0]["errors"])
                    self.assertEqual(2, len(results[0]["files"]))
                    self.assertEqual(b"", self.client._cookie_header(url).encode())
                finally:
                    source.close()

    def test_catalog_list_paginates_filters_and_explains_primary_amount(self) -> None:
        self.pipeline.run()
        second = Notice(
            source="second-source",
            authority_rank=90,
            external_id="JL-CATALOG-002",
            title="=不应被表格执行的合同标题",
            published_at="2026-08-20",
            url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/202608/example.htm",
            region="吉林市",
            buyer="吉林市政务服务局",
            notice_type="合同公告",
            source_role="official",
            origin_verified=True,
            publication_url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/202608/example.htm",
            origin_url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/202608/example.htm",
        )
        self.db.upsert_notice(second, status="metadata_ready")
        self.db.record_structured_fields(
            second,
            ExtractedFields(
                event_type="合同公告",
                project_code="JL-CATALOG-002",
                budget_amount_minor=90_000_000,
                award_amount_minor=88_000_000,
                contract_amount_minor=87_500_000,
                winning_vendor="吉林市测试网安公司",
                city="吉林市",
                buyer_contact="李四",
                buyer_phone="0432-12345678",
                winning_vendor_contact="王五",
                winning_vendor_phone="0432-87654321",
                project_summary="网络安全服务合同",
                published_at="2026-08-20",
            ),
        )
        self.db.record_classification(
            second,
            Classification(
                relevant=True,
                confidence=0.96,
                industry="政府机关",
                security_categories=("安全运营与运维",),
                evidence=("网络安全服务合同",),
                reason="测试列表数据",
                method="fixture-ai",
                ai_confirmed=True,
                needs_review=False,
            ),
        )

        first_page = self.db.list_notices(page=1, page_size=1)
        second_page = self.db.list_notices(page=2, page_size=1)
        self.assertEqual(2, first_page["total"])
        self.assertEqual(2, first_page["pages"])
        self.assertEqual("second-source:JL-CATALOG-002", first_page["items"][0]["notice_id"])
        self.assertEqual("catalog-fixture:JL-CATALOG-001", second_page["items"][0]["notice_id"])

        item = first_page["items"][0]
        self.assertEqual(87_500_000, item["amount_minor"])
        self.assertEqual("contract", item["amount_type"])
        self.assertEqual("not_downloaded", item["download_status"])
        self.assertEqual("李四", item["buyer_contact"])
        self.assertEqual("0432-87654321", item["winning_vendor_phone"])

        filtered = self.db.list_notices(
            page=1,
            page_size=50,
            query="测试网安公司",
            notice_type="合同公告",
            industry="政府机关",
            category="安全运营",
            city="吉林市",
            status="metadata_ready",
            download_status="not_downloaded",
            source="second-source",
            date_from="2026-08-20",
            date_to="2026-08-20",
            relevance="relevant",
        )
        self.assertEqual(1, filtered["total"])
        self.assertEqual("JL-CATALOG-002", filtered["items"][0]["external_id"])


class CatalogApiContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.config_path = self.root / "config.json"
        self.config_path.write_text(
            json.dumps(
                {
                    "start_date": "2026-01-01",
                    "end_date": "2026-08-21",
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
                            "type": "jilin_ggzy",
                            "name": "api-fixture-source",
                            "enabled": True,
                            "authority_rank": 100,
                            "page_size": 10,
                            "max_pages_per_channel": 1,
                        }
                    ],
                    "ai": {"enabled": False},
                    "delivery": {"mode": "on_demand"},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        db = Database(self.root / "output" / "state.sqlite3")
        try:
            notice = Notice(
                source="api-fixture-source",
                authority_rank=100,
                external_id="API-001",
                title='=HYPERLINK("https://invalid.example", "不应执行")',
                published_at="2026-08-21",
                url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/example.htm",
                region="长春市",
                buyer="长春市测试医院",
                notice_type="合同公告",
                source_role="official",
                origin_verified=True,
                publication_url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/example.htm",
                origin_url="https://www.ccgp.gov.cn/cggg/dfgg/htgg/example.htm",
            )
            db.upsert_notice(notice, status="metadata_ready")
            db.record_structured_fields(
                notice,
                ExtractedFields(
                    event_type="合同公告",
                    contract_amount_minor=87_500_000,
                    winning_vendor="长春市网安公司",
                    city="长春市",
                    buyer_contact="张三",
                    buyer_phone="0431-12345678",
                    winning_vendor_contact="李四",
                    winning_vendor_phone="0431-87654321",
                    project_summary="网络安全服务合同",
                    published_at="2026-08-21",
                ),
            )
            db.record_classification(
                notice,
                Classification(
                    relevant=True,
                    confidence=0.97,
                    industry="医疗卫生",
                    security_categories=("安全运营与运维",),
                    evidence=("网络安全服务合同",),
                    reason="测试",
                    method="fixture-ai",
                    ai_confirmed=True,
                ),
            )
        finally:
            db.close()

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

    def _request(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        csrf: bool = False,
    ) -> tuple[int, dict[str, str], bytes]:
        body = None
        headers: dict[str, str] = {}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if csrf:
            headers["X-CSRF-Token"] = self.app.csrf_token
        connection = http.client.HTTPConnection(self.host, self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            response_body = response.read()
            response_headers = {
                key.lower(): value for key, value in response.getheaders()
            }
            return response.status, response_headers, response_body
        finally:
            connection.close()

    def test_list_api_and_csv_export_match_catalog_contract(self) -> None:
        status, _, body = self._request(
            "GET",
            "/api/notices?page=1&page_size=1&industry=%E5%8C%BB%E7%96%97%E5%8D%AB%E7%94%9F",
        )
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertEqual(1, payload["total"])
        self.assertEqual("api-fixture-source:API-001", payload["items"][0]["notice_id"])
        self.assertEqual("contract", payload["items"][0]["amount_type"])
        self.assertEqual("not_downloaded", payload["items"][0]["download_status"])

        export_status, headers, export_body = self._request(
            "GET", "/api/notices/export?relevance=relevant"
        )
        self.assertEqual(200, export_status)
        self.assertTrue(export_body.startswith(b"\xef\xbb\xbf"))
        self.assertIn("text/csv", headers["content-type"])
        rows = list(csv.DictReader(io.StringIO(export_body.decode("utf-8-sig"))))
        self.assertEqual(1, len(rows))
        self.assertTrue(rows[0]["数据标题"].startswith("'="))
        self.assertEqual("875000.00", rows[0]["金额"])
        self.assertEqual("合同", rows[0]["金额类型"])
        self.assertEqual("李四", rows[0]["中标单位联系人-企业公示"])
        self.assertEqual("0431-87654321", rows[0]["中标单位电话-企业公示"])

    def test_download_api_validates_batch_and_supports_identity_alias(self) -> None:
        invalid_status, _, invalid_body = self._request(
            "POST", "/api/notices/download", {"notice_ids": []}, csrf=True
        )
        self.assertEqual(422, invalid_status)
        self.assertIn("1 到 200", invalid_body.decode("utf-8"))

        returned = [
            {
                "notice_id": "api-fixture-source:API-001",
                "status": "downloaded",
                "files": [{"path": "safe-local-path", "sha256": "a" * 64}],
                "errors": [],
            },
            {
                "notice_id": "missing:API-404",
                "status": "failed",
                "files": [],
                "errors": ["未找到标讯"],
            },
        ]
        with patch.object(self.app, "download_notices", return_value=returned) as mocked:
            status, _, body = self._request(
                "POST",
                "/api/notices/download",
                {
                    "identities": [
                        "api-fixture-source:API-001",
                        "missing:API-404",
                    ],
                    "include_notice": True,
                    "include_attachments": True,
                },
                csrf=True,
            )

        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(200, status)
        self.assertFalse(payload["ok"])
        self.assertEqual(1, payload["summary"]["downloaded"])
        self.assertEqual(1, payload["summary"]["failed"])
        self.assertEqual(2, len(payload["items"]))
        mocked.assert_called_once_with(
            ["api-fixture-source:API-001", "missing:API-404"],
            include_notice=True,
            include_attachments=True,
        )

    def test_download_link_export_api_does_not_request_sources(self) -> None:
        with patch.object(HttpClient, "request", side_effect=AssertionError("export must be local")):
            status, _, body = self._request("POST", "/api/notices/links",
                {"notice_ids": ["api-fixture-source:API-001"]}, csrf=True)
            self.assertEqual(200, status)
            rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
            self.assertEqual(1, len(rows))
            self.assertEqual("", rows[0]["下载URL"])
            self.assertTrue(rows[0]["公告URL"].startswith("https://www.ccgp.gov.cn/"))
            denied, _, _ = self._request("POST", "/api/notices/links", {"notice_ids": []})
            self.assertEqual(403, denied)


if __name__ == "__main__":
    unittest.main()
