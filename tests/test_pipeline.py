from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tender_downloader.config import AppConfig
from tender_downloader.content_gate import ContentGatePolicy
from tender_downloader.database import Database
from tender_downloader.export import verify_files
from tender_downloader.http_client import HttpClient
from tender_downloader.models import Classification, Coverage, Notice, RawDocument
from tender_downloader.pipeline import Pipeline
from tender_downloader.official_registry import SourceRole
from tender_downloader.sources.base import SourceAdapter
from tender_downloader.storage import ImmutableStore


PDF_BYTES = b"%PDF-1.4\nsource-bytes-must-not-change\n%%EOF"


class _Handler(BaseHTTPRequestHandler):
    missing_requests = 0

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/notice.html":
            body = (
                '<html><body><h1>吉林大学网络安全等保项目</h1>'
                '<p>采购人：吉林大学</p><p>本项目采购网络安全等级保护测评、整改咨询及相关服务，'
                '所有内容均来自测试官方公告原始响应。</p>'
                '<a href="/source.pdf">附件：招标文件.pdf</a></body></html>'
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path == "/notice-with-failure.html":
            body = (
                '<html><body><h1>吉林大学网络安全等保项目</h1>'
                '<p>采购人：吉林大学</p><p>本项目采购网络安全等级保护测评、整改咨询及相关服务，'
                '所有内容均来自测试官方公告原始响应。</p>'
                '<a href="/source.pdf">招标文件.pdf</a>'
                '<a href="/missing.pdf">补充附件.pdf</a></body></html>'
            ).encode("utf-8")
            content_type = "text/html; charset=utf-8"
        elif self.path == "/source.pdf":
            if "notice" not in self.headers.get("Referer", ""):
                self.send_error(403)
                return
            body = PDF_BYTES
            content_type = "application/pdf"
        elif self.path == "/missing.pdf":
            type(self).missing_requests += 1
            self.send_error(404)
            return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        if self.path.endswith(".pdf"):
            self.send_header("Content-Disposition", 'attachment; filename="source.pdf"')
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class _Source(SourceAdapter):
    display_name = "fixture-source"

    def __init__(self, client, url: str) -> None:
        super().__init__(client, {"authority_rank": 100})
        self.url = url

    def iter_notices(self, start: date, end: date, coverage: Coverage):
        coverage.pages += 1
        coverage.notices += 1
        yield Notice(
            source=self.name,
            authority_rank=100,
            external_id="JL-TEST-001",
            title="吉林大学网络安全等保项目",
            published_at="2026-08-18",
            url=self.url,
            buyer="吉林大学",
            notice_type="采购公告",
        )




class _SlowSource(_Source):
    """Two instances overlap on purpose to exercise concurrent collection."""

    def __init__(self, client, url, tag, delay):
        super().__init__(client, url)
        self.tag = tag
        self.delay = delay
        self.overlapped = False

    def iter_notices(self, start, end, coverage):
        import time as _time
        coverage.pages += 1
        coverage.notices += 1
        started = _time.monotonic()
        # Both sources announce themselves while the other is still running;
        # a serial loop could never observe the overlap.
        yield Notice(
            source=self.name, authority_rank=100, external_id=f"ID-{self.tag}",
            title=f"并发源 {self.tag}", published_at="2026-08-18",
            url=f"{self.url}?src={self.tag}", buyer="测试", notice_type="采购公告",
        )
        _time.sleep(self.delay)
        self.overlapped = _time.monotonic() - started < self.delay + 0.2


class _AI:
    def classify(self, notice: Notice, text: str, *, stage: str = "final") -> Classification:
        return Classification(
            relevant=True,
            confidence=0.98,
            industry="教育",
            security_categories=("测评咨询",),
            evidence=("网络安全",),
            reason="明确采购网络安全等保服务",
            method=f"fixture-ai:{stage}",
            ai_confirmed=True,
            needs_review=False,
        )


class _DualBodySource(_Source):
    raw_shell = b"<html><body><div id='app'></div></body></html>"
    rendered = (
        '<html><body><h1>吉林省网络安全设备维保项目</h1>'
        '<p>发布时间：2026-08-18</p><p>采购人：测试单位</p>'
        '</body></html>'
    ).encode("utf-8")

    def fetch_detail(self, notice: Notice) -> RawDocument:
        return RawDocument(
            body=self.raw_shell,
            url=notice.url,
            headers={"content-type": "text/html; charset=utf-8"},
            analysis_body=self.rendered,
        )


class _RecordingAI(_AI):
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def classify(self, notice: Notice, text: str, *, stage: str = "final") -> Classification:
        self.inputs.append(text)
        return super().classify(notice, text, stage=stage)


class _LocalOfficialPipeline(Pipeline):
    """仅测试夹具：把本机 HTTP 服务视为精确官方主机。"""

    def _role_for_url(self, url: str) -> SourceRole:
        if url.startswith("http://127.0.0.1:"):
            return SourceRole.OFFICIAL
        return super()._role_for_url(url)

    def _official_notice_source_allowed(self, url: str) -> bool:
        if url.startswith("http://127.0.0.1:"):
            return True
        return super()._official_notice_source_allowed(url)

    def _gate_policy(self, origin: str, final_url: str) -> ContentGatePolicy:
        base = super()._gate_policy(origin, final_url)
        return ContentGatePolicy(
            official_hosts=base.official_hosts | {"127.0.0.1"},
            allowed_redirect_pairs=base.allowed_redirect_pairs,
            minimum_html_text_chars=base.minimum_html_text_chars,
            insecure_test_hosts={"127.0.0.1"},
        )

    def _official_attachment_source_allowed(
        self, attachment_url: str, official_notice_url: str
    ) -> bool:
        if attachment_url.startswith("http://127.0.0.1:"):
            return True
        return super()._official_attachment_source_allowed(
            attachment_url, official_notice_url
        )


class PipelineTests(unittest.TestCase):
    def test_jilin_cdn_is_an_attachment_only_source_with_parent_context(self) -> None:
        pipeline = object.__new__(Pipeline)
        pipeline.registry = Pipeline._build_source_registry([])
        cdn_url = (
            "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/"
            "1024FPA/2026/08/tender-file.pdf"
        )

        # The exact host has an official role for attachment gating, but may
        # not be processed as a standalone notice URL.
        self.assertIs(SourceRole.UNKNOWN, pipeline._role_for_url(cdn_url))
        self.assertFalse(pipeline._official_notice_source_allowed(cdn_url))
        self.assertTrue(pipeline._official_attachment_source_allowed(
            cdn_url,
            "https://www.jl.gov.cn/ggzy/notice/123.html",
        ))
        self.assertIn(
            "zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com",
            pipeline._gate_policy(cdn_url, cdn_url).official_hosts,
        )
        for attachment_url, parent_url in (
            (
                cdn_url.replace("https://", "http://", 1),
                "https://www.jl.gov.cn/ggzy/notice/123.html",
            ),
            (
                cdn_url,
                "https://www.okcis.cn/notice/123.html",
            ),
            (
                cdn_url,
                "https://www.ccgp.gov.cn/cggg/notice/123.html",
            ),
            (
                "https://another.oss-cn-north-2-gov-1.aliyuncs.com/file.pdf",
                "https://www.jl.gov.cn/ggzy/notice/123.html",
            ),
        ):
            with self.subTest(attachment=attachment_url, parent=parent_url):
                self.assertFalse(pipeline._official_attachment_source_allowed(
                    attachment_url,
                    parent_url,
                ))

    def test_registry_builder_does_not_trust_arbitrary_or_commercial_config_hosts(self) -> None:
        registry = Pipeline._build_source_registry(
            [
                {
                    "type": "custom_web",
                    "name": "任意商业站",
                    "source_role": "official",
                    "start_urls": ["https://commercial.example/notices"],
                },
                {
                    "type": "custom_web",
                    "name": "已知线索站",
                    "source_role": "official",
                    "start_urls": ["https://www.okcis.cn/notices"],
                },
            ]
        )
        self.assertIs(
            SourceRole.UNKNOWN,
            registry.role_for_url("https://commercial.example/notices"),
        )
        self.assertIs(
            SourceRole.COMMERCIAL_LEAD,
            registry.role_for_url("https://www.okcis.cn/notices"),
        )

    def test_registry_builder_accepts_https_government_and_education_hosts(self) -> None:
        registry = Pipeline._build_source_registry(
            [
                {
                    "type": "custom_web",
                    "name": "政府采购门户",
                    "source_role": "official",
                    "start_urls": ["https://procurement.example.gov.cn/notices"],
                },
                {
                    "type": "custom_web",
                    "name": "高校采购门户",
                    "source_role": "official",
                    "start_urls": ["https://bidding.example.edu.cn/notices"],
                },
            ]
        )
        self.assertIs(
            SourceRole.OFFICIAL,
            registry.role_for_url("https://procurement.example.gov.cn/notices"),
        )
        self.assertIs(
            SourceRole.OFFICIAL,
            registry.role_for_url("https://bidding.example.edu.cn/notices"),
        )
        self.assertIs(
            SourceRole.UNKNOWN,
            registry.role_for_url(
                "https://unlisted.procurement.example.gov.cn/notices"
            ),
        )

    def test_rendered_lead_without_official_original_is_never_classified_or_delivered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(root / "config.json", {
                "start_date": "2026-01-01",
                "end_date": "2026-08-18",
                "output_dir": str(root / "output"),
                "database": str(root / "output" / "state.sqlite3"),
                "sources": [],
                "ai": {
                    "auto_accept_threshold": 0.85,
                    "second_review_threshold": 0.60,
                },
                "delivery": {"mode": "automatic", "include_notice_html_when_no_attachment": True},
            })
            client = HttpClient(
                user_agent="fixture/1.0 (+mailto:test@example.com)",
                timeout_seconds=5,
                delay_seconds=0,
                max_retries=0,
                allow_private_hosts=True,
            )
            db = Database(config.database_path)
            classifier = _RecordingAI()
            try:
                source = _DualBodySource(client, "https://example.test/notice.html")
                # Keep all recall evidence exclusive to the rendered channel.
                source.iter_notices = lambda _start, _end, coverage: iter((Notice(
                    source=source.name,
                    authority_rank=100,
                    external_id="DUAL-BODY-001",
                    title="普通采购项目",
                    published_at="2026-08-18",
                    url=source.url,
                    buyer="测试单位",
                    notice_type="采购公告",
                ),))
                pipeline = Pipeline(
                    config=config,
                    client=client,
                    db=db,
                    store=ImmutableStore(config.output_dir),
                    classifier=classifier,
                    sources=[source],
                )
                summary = pipeline.run()
                rows = db.artifact_rows()
            finally:
                db.close()

            self.assertEqual(0, summary["delivered"])
            self.assertEqual(1, summary["unresolved"])
            self.assertFalse(classifier.inputs)
            self.assertTrue(rows)
            self.assertTrue(all(not row["delivery_eligible"] for row in rows))
            self.assertTrue(any(row["document_kind"] == "rendered_snapshot" for row in rows))


    def test_sources_are_collected_concurrently(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = AppConfig(root / "config.json", {
                    "start_date": "2026-01-01", "end_date": "2026-08-18",
                    "output_dir": str(root / "output"),
                    "database": str(root / "output" / "state.sqlite3"),
                    "sources": [], "ai": {}, "delivery": {"mode": "automatic"},
                })
                client = HttpClient(user_agent="fixture/1.0", timeout_seconds=5,
                                    delay_seconds=0, max_retries=0, allow_private_hosts=True)
                db = Database(config.database_path)
                try:
                    slow = _SlowSource(client, f"http://127.0.0.1:{server.server_port}/notice.html", "slow", 0.9)
                    fast = _SlowSource(client, f"http://127.0.0.1:{server.server_port}/notice.html", "fast", 0.1)
                    pipeline = _LocalOfficialPipeline(
                        config=config, client=client, db=db,
                        store=ImmutableStore(config.output_dir), classifier=_AI(),
                        sources=[slow, fast],
                    )
                    summary = pipeline.run()
                    self.assertEqual(2, summary["sources"])
                    self.assertEqual(2, summary["notices"])
                    # The slow source must still have been running while the
                    # fast one finished; only true with concurrent collection.
                    self.assertTrue(slow.overlapped, "sources did not overlap")
                finally:
                    db.close()
        finally:
            server.shutdown()

    def test_end_to_end_delivers_original_bytes(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = AppConfig(root / "config.json", {
                    "start_date": "2026-01-01",
                    "end_date": "2026-08-18",
                    "output_dir": str(root / "output"),
                    "database": str(root / "output" / "state.sqlite3"),
                    "sources": [],
                    "ai": {"auto_accept_threshold": 0.85, "second_review_threshold": 0.60},
                    "delivery": {"mode": "automatic", "include_notice_html_when_no_attachment": True},
                })
                client = HttpClient(
                    user_agent="fixture/1.0 (+mailto:test@example.com)",
                    timeout_seconds=5,
                    delay_seconds=0,
                    max_retries=0,
                    allow_private_hosts=True,
                )
                db = Database(config.database_path)
                try:
                    pipeline = _LocalOfficialPipeline(
                        config=config,
                        client=client,
                        db=db,
                        store=ImmutableStore(config.output_dir),
                        classifier=_AI(),
                        sources=[_Source(client, f"http://127.0.0.1:{server.server_port}/notice.html")],
                    )
                    summary = pipeline.run()
                    checked, errors = verify_files(db)
                    rows = db.artifact_rows()
                finally:
                    db.close()

                self.assertEqual(1, summary["delivered"])
                self.assertGreaterEqual(checked, 3)  # HTML 原件 + PDF 原件和 PDF 交付副本
                # 该夹具仅用于证明本地流水线保持原字节，显式使用 HTTP
                # test exemption；生产级 verify 必须继续拒绝它。
                self.assertTrue(
                    any("没有当前有效的 HTTPS 官方原件" in error for error in errors)
                )
                self.assertTrue(
                    any("不是安全 HTTPS" in error for error in errors)
                )
                delivered = [Path(row["delivery_path"]) for row in rows if row["delivery_path"]]
                self.assertEqual(2, len(delivered))
                self.assertIn(PDF_BYTES, [path.read_bytes() for path in delivered])
                self.assertTrue(all(row["delivery_eligible"] for row in rows if row["delivery_path"]))
        finally:
            server.shutdown()
            server.server_close()

    def test_failed_public_attachment_forces_review_and_retries(self) -> None:
        _Handler.missing_requests = 0
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = AppConfig(root / "config.json", {
                    "start_date": "2026-01-01",
                    "end_date": "2026-08-18",
                    "output_dir": str(root / "output"),
                    "database": str(root / "output" / "state.sqlite3"),
                    "sources": [],
                    "ai": {"auto_accept_threshold": 0.85, "second_review_threshold": 0.60},
                    "recall": {"mode": "p0_complete"},
                    "delivery": {"mode": "automatic", "include_notice_html_when_no_attachment": True},
                })
                client = HttpClient(
                    user_agent="fixture/1.0 (+mailto:test@example.com)",
                    timeout_seconds=5,
                    delay_seconds=0,
                    max_retries=0,
                    allow_private_hosts=True,
                )
                db = Database(config.database_path)
                try:
                    pipeline = _LocalOfficialPipeline(
                        config=config,
                        client=client,
                        db=db,
                        store=ImmutableStore(config.output_dir),
                        classifier=_AI(),
                        sources=[_Source(
                            client,
                            f"http://127.0.0.1:{server.server_port}/notice-with-failure.html",
                        )],
                    )
                    first = pipeline.run()
                    second = pipeline.run()
                    refs = db.artifact_ref_rows()
                finally:
                    db.close()
                self.assertEqual(1, first["review"])
                self.assertEqual(0, first["delivered"])
                self.assertEqual(1, second["review"])
                self.assertGreaterEqual(_Handler.missing_requests, 2)
                failed = [row for row in refs if row["source_url"].endswith("missing.pdf")]
                self.assertEqual("failed", failed[0]["status"])
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
