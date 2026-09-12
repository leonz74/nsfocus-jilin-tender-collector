from __future__ import annotations

import csv
import io
import tempfile
import unittest
from http.client import IncompleteRead
from pathlib import Path
from unittest.mock import MagicMock, patch

from tender_downloader.database import Database
from tender_downloader.content_gate import ContentGateInput, evaluate_content
from tender_downloader.htmlparse import parse_html_document
from tender_downloader.export import DOWNLOAD_LINK_FIELDS, download_links_csv
from tender_downloader.field_extract import _amount_minor
from tender_downloader.http_client import HttpClient, HttpResult, FetchError
from tender_downloader.models import AttachmentRef, Notice
from tender_downloader.official_registry import DEFAULT_SOURCE_REGISTRY
from tender_downloader.sources.national_ggzy import NationalGGZYSource
from tender_downloader.webui.browser_login import _browser_candidates


class DownloadLinkExportTests(unittest.TestCase):
    def test_observed_attachment_and_missing_link_remain_distinct_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            db = Database(Path(directory) / "state.sqlite3")
            try:
                notice = Notice(source="fixture", authority_rank=100, external_id="1",
                                title="=测试项目", published_at="2026-09-10",
                                url="https://www.jl.gov.cn/ggzy/notice.html",
                                publication_url="https://www.jl.gov.cn/ggzy/notice.html",
                                source_role="official", origin_verified=True)
                db.upsert_notice(notice, status="review")
                db.record_artifact_ref(notice, AttachmentRef(
                    url="https://www.jl.gov.cn/files/tender.pdf", original_name="采购文件.pdf",
                    label="采购文件", access="public_direct"), status="failed", error="HTTP 403")
                missing = Notice(source="fixture", authority_rank=50, external_id="2",
                                 title="没有公开附件", published_at="", url="https://portal.example/notice/2")
                db.upsert_notice(missing, status="unresolved_official_source")
                with patch.object(HttpClient, "request", side_effect=AssertionError("export used network")):
                    blob = download_links_csv(db)
                    selected = download_links_csv(db, [missing.identity])
                    empty = download_links_csv(db, [])
                rows = list(csv.DictReader(io.StringIO(blob.decode("utf-8-sig"))))
                self.assertEqual(2, len(rows))
                self.assertEqual("'=测试项目", rows[0]["项目名称"])
                self.assertEqual("https://www.jl.gov.cn/files/tender.pdf", rows[0]["下载URL"])
                self.assertEqual("下载失败", rows[0]["下载状态"])
                self.assertEqual("HTTP 403", rows[0]["失败或待办原因"])
                same_ref = AttachmentRef(url=rows[0]["下载URL"], original_name="采购文件.pdf", label="采购文件", access="public_direct")
                db.record_artifact_ref(notice, same_ref, status="available")
                refreshed = list(csv.DictReader(io.StringIO(download_links_csv(db, [notice.identity]).decode("utf-8-sig"))))
                self.assertEqual("下载失败", refreshed[0]["下载状态"])
                self.assertEqual("HTTP 403", refreshed[0]["失败或待办原因"])
                db.record_artifact_ref(notice, same_ref, status="downloaded")
                self.assertEqual("", db.artifact_ref_rows()[0]["error"])
                self.assertEqual("", rows[0]["SHA256"])
                self.assertEqual("", rows[1]["下载URL"])
                self.assertEqual("公告入口", rows[1]["链接类型"])
                self.assertEqual("https://portal.example/notice/2", rows[1]["公告URL"])
                self.assertEqual(1, len(list(csv.DictReader(io.StringIO(selected.decode("utf-8-sig"))))))
                self.assertEqual([], list(csv.DictReader(io.StringIO(empty.decode("utf-8-sig")))))
                self.assertEqual(list(DOWNLOAD_LINK_FIELDS), next(csv.reader(io.StringIO(empty.decode("utf-8-sig")))))
            finally:
                db.close()

    def test_invalid_large_amount_does_not_abort_a_notice(self):
        for number in ("9" * 1000, "NaN", "Infinity", "-1"):
            self.assertIsNone(_amount_minor(number, "万元"))
        self.assertEqual(82460000, _amount_minor("82.46", "万元"))

    def test_metadata_only_review_survives_reopening_without_becoming_legacy_delivery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            db = Database(path)
            notice = Notice(source="fixture", authority_rank=100, external_id="review", title="待复核公告",
                            published_at="2026-09-10", url="https://www.ccgp.gov.cn/notice/1",
                            source_role="official", origin_verified=True, metadata={"scan_sha256": "a" * 64})
            db.upsert_notice(notice, status="review")
            db.close()
            reopened = Database(path)
            self.assertEqual("review", reopened.connection.execute("SELECT status FROM notices").fetchone()[0])
            reopened.close()

    def test_central_procurement_storage_is_scoped_to_ccgp_parent_and_exact_bucket(self):
        url = "https://zycgdzmc-pro.obs.cn-north1.ctyun.cn/tender.pdf"
        parent = "https://www.ccgp.gov.cn/notice/1"
        self.assertIsNotNone(DEFAULT_SOURCE_REGISTRY.official_attachment_registration(url, parent))
        self.assertIsNone(DEFAULT_SOURCE_REGISTRY.official_attachment_registration(url, "https://www.ggzy.gov.cn/notice/1"))
        self.assertIsNone(DEFAULT_SOURCE_REGISTRY.official_attachment_registration(url.replace("zycgdzmc-pro", "other-bucket"), parent))

    def test_truncated_http_response_is_retried_without_accepting_partial_bytes(self):
        client = HttpClient(user_agent="fixture", delay_seconds=0, max_retries=1,
                            allow_private_hosts=True, sleeper=lambda _: None)
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"complete-body"
        response.status = 200
        response.headers = {"Content-Type": "text/plain"}
        response.geturl.return_value = "http://127.0.0.1/fixture"
        try:
            with patch.object(client._opener, "open", side_effect=[IncompleteRead(b"partial"), response]) as opener:
                result = client.request("http://127.0.0.1/fixture")
            self.assertEqual(b"complete-body", result.body)
            self.assertEqual(2, opener.call_count)
        finally:
            client.close()

    def test_macos_user_application_bundle_is_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
            executable.parent.mkdir(parents=True)
            executable.write_text("fixture")
            with patch("tender_downloader.webui.browser_login.sys.platform", "darwin"), patch.object(Path, "home", return_value=Path(directory)), patch("tender_downloader.webui.browser_login.shutil.which", return_value=None):
                self.assertIn(executable, _browser_candidates())


class NationalDetailTests(unittest.TestCase):
    outer = "https://www.ggzy.gov.cn/information/deal/html/a/220000/0201/20260910/0022abc.html"
    inner = "https://www.ggzy.gov.cn/information/deal/html/b/220000/0201/20260910/0022abc.html"

    def test_follows_observed_matching_body_and_retains_discovery_url(self):
        client = MagicMock()
        client.request.side_effect = [
            HttpResult(self.outer, 200, {"content-type": "text/html"}, f"<script>var firstLastUrl='{self.inner}';</script>".encode()),
            HttpResult(self.inner, 200, {"content-type": "text/html"}, b"original procurement response"),
        ]
        notice = Notice(source="national", authority_rank=65, external_id="0022abc", title="test", published_at="", url=self.outer)
        detail = NationalGGZYSource(client, {}).fetch_detail(notice)
        self.assertEqual(b"original procurement response", detail.body)
        self.assertEqual(self.inner, notice.url)
        self.assertEqual(self.outer, notice.discovery_url)
        client.request.assert_called_with(self.inner, headers={"Referer": self.outer})

    def test_rejects_cross_origin_and_other_announcement_body(self):
        for inner in (self.inner.replace("www.ggzy.gov.cn", "untrusted.example"), self.inner.replace("0022abc", "0022other")):
            client = MagicMock()
            client.request.return_value = HttpResult(self.outer, 200, {}, f"var firstLastUrl='{inner}';".encode())
            notice = Notice(source="national", authority_rank=65, external_id="0022abc", title="test", published_at="", url=self.outer)
            with self.assertRaises(FetchError):
                NationalGGZYSource(client, {}).fetch_detail(notice)
            self.assertEqual(1, client.request.call_count)

    def test_national_notice_can_reference_observed_exact_procurement_storage(self):
        url = "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/1024FPA/file.doc"
        self.assertIsNotNone(DEFAULT_SOURCE_REGISTRY.official_attachment_registration(url, self.inner))
        self.assertIsNone(DEFAULT_SOURCE_REGISTRY.official_attachment_registration(url.replace("zcy-gov-open-doc", "other-bucket"), self.inner))

    def test_plain_html_compatibility_is_limited_to_real_national_body_endpoint(self):
        body = ("<html><body><h1>网络安全采购项目招标公告</h1>"
                "<p>采购人：吉林省某单位。项目编号：JL-2026-001。预算金额：100万元。"
                "采购需求：网络安全等级保护测评及应急响应服务。获取招标文件时间为2026年9月1日至9月10日。"
                "开标时间为2026年9月20日。联系人：张先生，联系电话：0431-12345678。</p></body></html>").encode()
        allowed = evaluate_content(ContentGateInput(source_role="official", origin=self.inner,
            final_url=self.inner, content_type="text/plain;charset=UTF-8", body=body))
        self.assertTrue(allowed.allowed, allowed.reasons)
        for url in (self.outer, "https://www.jl.gov.cn/ggzy/notice.html"):
            rejected = evaluate_content(ContentGateInput(source_role="official", origin=url,
                final_url=url, content_type="text/plain", body=body))
            self.assertFalse(rejected.allowed)
        login = evaluate_content(ContentGateInput(source_role="official", origin=self.inner,
            final_url=self.inner, content_type="text/plain", body=b'<html><body>Please login<input type="password"></body></html>'))
        self.assertFalse(login.allowed)

    def test_jilin_navigation_does_not_pollute_notice_text_or_attachments(self):
        body = b'<nav>school hospital<a href="/guide.pdf">download</a></nav><div class="ewb-article"><h3>tender</h3><div>actual notice<a href="/file.docx">file</a></div></div><footer>education</footer>'
        doc = parse_html_document(body, "https://www.jl.gov.cn/ggzy/notice.html")
        self.assertIn("actual notice", doc.text)
        self.assertNotIn("hospital", doc.text)
        self.assertNotIn("education", doc.text)
        self.assertEqual(["https://www.jl.gov.cn/file.docx"], [ref.url for ref in doc.attachments])
