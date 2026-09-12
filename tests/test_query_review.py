from __future__ import annotations

import copy
import csv
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from tender_downloader.classify import LLMClassifier, RuleClassifier
from tender_downloader.config import AppConfig
from tender_downloader.database import Database
from tender_downloader.http_client import HttpClient
from tender_downloader.models import Coverage, Notice
from tender_downloader.query import normalise_query, query_config
from tender_downloader.query_review import QueryReviewer
from tender_downloader.sources.national_ggzy import NationalGGZYSource
from tender_downloader.sources.ccgp_search import CCGPSearchSource
from tender_downloader.storage import ImmutableStore
from tender_downloader.webui.server import ConfigWebApp
from test_metadata_catalog_workflow import _CatalogHandler, _LocalOfficialCatalogPipeline, NOTICE_BODY

CRITERIA = {"start_date": "2026-08-01", "end_date": "2026-08-31", "city": "长春",
            "industry": "医疗卫生", "keyword": "等级保护", "notice_type": "",
            "mode": "ai_recall", "max_candidates": 10}
QUOTE = "网络安全等级保护测评、安全运营和应急响应服务"


class _ReviewHandler(_CatalogHandler):
    count = 1
    calls = 0
    fail_model = False
    missing_detail = False
    verdict = "match"
    searches = []
    baseline_count = 0

    def do_GET(self):
        if self.path.startswith("/item-"):
            if type(self).missing_detail:
                self.send_error(404)
                return
            body = NOTICE_BODY.replace("长春市医院网络安全服务项目".encode(), "公共服务能力提升项目".encode())
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self):
        raw = self.rfile.read(int(self.headers["Content-Length"]))
        cls = type(self)
        if self.path == "/search":
            fields = parse_qs(raw.decode())
            cls.searches.append(fields)
            records = [
                {"id": str(i), "url": f"http://127.0.0.1:{self.server.server_port}/item-{i}.html",
                 "title": "公共服务能力提升项目", "publishTime": "2026-08-18", "provinceText": "吉林"}
                for i in range(cls.baseline_count if fields.get("FINDTXT") else cls.count)]
            payload = {"code": 200, "data": {"pages": 1 if records else 0, "records": records}}
        elif self.path == "/model":
            cls.calls += 1
            if cls.fail_model:
                self.send_error(503, "fixture provider failure")
                return
            request = json.loads(raw)
            user = json.loads(request["messages"][1]["content"])
            evidence = {"city": "长春市人民医院", "industry": "长春市人民医院", "keyword": QUOTE}
            result = {"checks": {key: {"verdict": cls.verdict, "confidence": 0.97,
                       "evidence": [evidence[key]]} for key in user["客户条件"]},
                      "reason": "测试模型按正文逐字证据复核"}
            payload = {"choices": [{"message": {"content": json.dumps(result, ensure_ascii=False)}}]}
        else:
            self.send_error(404)
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QueryReviewIntegrationTests(unittest.TestCase):
    def setUp(self):
        _ReviewHandler.count = 1
        _ReviewHandler.calls = 0
        _ReviewHandler.searches = []
        _ReviewHandler.baseline_count = 0
        _ReviewHandler.fail_model = False
        _ReviewHandler.missing_detail = False
        _ReviewHandler.verdict = "match"
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), _ReviewHandler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.base = f"http://127.0.0.1:{self.http.server_port}"
        self.client = HttpClient(user_agent="review-test", delay_seconds=0, max_retries=0,
                                 timeout_seconds=3, allow_private_hosts=True)
        self.saved = AppConfig(self.root / "config.json", {
            "start_date": "2026-08-01", "end_date": "2026-08-31",
            "output_dir": str(self.root / "output"), "database": str(self.root / "state.sqlite"),
            "sources": [{"type": "national_ggzy", "deal_types": ["01"], "source_types": ["1"]}],
            "http": {"allow_private_hosts": True, "delay_seconds": 0},
            "ai": {"enabled": False, "endpoint": self.base + "/model", "model": "fixture",
                   "api_key_env": "TENDER_AI_TEST_KEY"},
        })
        self.saved.path.write_text(json.dumps(self.saved.data))
        self.db = Database(self.saved.database_path)

    def tearDown(self):
        self.db.close()
        self.client.close()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(3)
        self.tmp.cleanup()

    def run_query(self, query_id="a" * 32):
        config = query_config(self.saved, CRITERIA, query_id)
        model = LLMClassifier(self.client, config.data["ai"], api_key="test-only")
        source = NationalGGZYSource(self.client, config.data["sources"][0])
        source.endpoint = self.base + "/search"
        pipeline = _LocalOfficialCatalogPipeline(
            config=config, client=self.client, db=self.db, store=ImmutableStore(config.output_dir),
            classifier=RuleClassifier(), sources=[source], query_reviewer=QueryReviewer(model, CRITERIA))
        result = pipeline.run()
        return result, self.db.list_notices(query_id=query_id)["items"]

    def test_broad_real_http_query_finds_notice_that_title_search_misses(self):
        regular = query_config(self.saved, {**CRITERIA, "mode": "standard"}, "b" * 32)
        source = NationalGGZYSource(self.client, regular.data["sources"][0])
        source.endpoint = self.base + "/search"
        self.assertEqual([], list(source.iter_notices(date(2026, 8, 1), date(2026, 8, 31), Coverage(source.name))))
        result, rows = self.run_query()
        self.assertEqual(1, len(rows))
        self.assertEqual("match", rows[0]["query_review"]["status"])
        self.assertIn(QUOTE, rows[0]["query_review"]["evidence"])
        self.assertEqual(1, _ReviewHandler.calls)
        self.assertNotIn("FINDTXT", _ReviewHandler.searches[-1])
        self.assertEqual("2026-08-01", _ReviewHandler.searches[-1]["TIMEBEGIN"][0])
        self.assertFalse(self.saved.data["ai"]["enabled"])
        self.assertEqual(0, rows[0]["downloaded_file_count"])

    def test_limit_marks_partial_and_never_claims_full_coverage(self):
        _ReviewHandler.count = 11
        result, rows = self.run_query()
        self.assertEqual(10, len(rows))
        self.assertEqual(10, _ReviewHandler.calls)
        self.assertEqual(1, result["partial_sources"])
        coverage = dict(self.db.latest_coverage_rows()[0])
        self.assertTrue(coverage["truncated"])
        self.assertIn("10 条处理上限", coverage["message"])

    def test_provider_failure_opens_circuit_and_keeps_every_row_for_review(self):
        _ReviewHandler.count = 5
        _ReviewHandler.fail_model = True
        result, rows = self.run_query()
        self.assertEqual(5, len(rows))
        self.assertEqual(3, _ReviewHandler.calls)
        states = [row["query_review"]["status"] for row in rows]
        self.assertEqual(3, states.count("error"))
        self.assertEqual(2, states.count("not_run"))
        self.assertEqual(1, result["partial_sources"])
        self.assertNotIn("test-only", json.dumps(rows))

    def test_baseline_survives_both_extra_search_and_model_budgets(self):
        for baseline, total, expected, extra in ((12, 14, 14, 2), (2, 20, 12, 10)):
            _ReviewHandler.baseline_count = baseline
            _ReviewHandler.count = total
            _ReviewHandler.calls = 0
            result, rows = self.run_query(query_id=f"{baseline:032x}")
            self.assertEqual(expected, len(rows))
            self.assertEqual(baseline, sum(row["query_review"]["origin"] == "baseline" for row in rows))
            self.assertEqual(extra, sum(row["query_review"]["origin"] == "broad" for row in rows))
            self.assertEqual(10, _ReviewHandler.calls, "AI calls have an independent per-source budget")
            self.assertEqual(expected - 10, sum(row["query_review"]["status"] == "not_run" for row in rows))
            self.assertEqual(1, result["partial_sources"])

    def test_failed_detail_is_visible_and_not_sent_to_model(self):
        _ReviewHandler.missing_detail = True
        with self.assertLogs("tender_downloader.pipeline", level="ERROR"):
            _, rows = self.run_query()
        self.assertEqual(1, len(rows))
        self.assertEqual("unread", rows[0]["query_review"]["status"])
        self.assertEqual(0, _ReviewHandler.calls)

    def test_negative_review_remains_auditable_and_exports_evidence(self):
        _ReviewHandler.verdict = "no_match"
        _, rows = self.run_query()
        self.assertEqual("no_match", rows[0]["query_review"]["status"])
        app = ConfigWebApp(self.saved.path)
        try:
            exported = list(csv.DictReader(io.StringIO(app.export_notices(
                {"query_id": ["a" * 32]}, [rows[0]["identity"]]).decode("utf-8-sig"))))
            self.assertEqual("AI 判断不匹配", exported[0]["AI需求复核"])
            self.assertEqual("测试模型按正文逐字证据复核", exported[0]["AI复核说明"])
            self.assertEqual(self.db.get_notice(rows[0]["identity"]).url, exported[0]["原公告URL"])
            # Evidence/issue columns were dropped from the sheet per product
            # decision; auditability now rests on the reason plus 原公告URL.
            self.assertNotIn("AI原文证据", exported[0])
            self.assertNotIn("文件线索缺口", exported[0])
        finally:
            app.close()

    def test_key_only_goes_to_child_environment_not_query_or_saved_config(self):
        app = ConfigWebApp(self.saved.path)
        try:
            with patch.object(app.runner, "start") as start:
                query = app.start_query(CRITERIA, api_key="ephemeral-key")
                kwargs = start.call_args.kwargs
                self.assertEqual("ephemeral-key", kwargs["api_key"])
                self.assertEqual("TENDER_AI_TEST_KEY", kwargs["api_key_env"])
                self.assertNotIn("ephemeral-key", json.dumps(query))
                self.assertNotIn("ephemeral-key", self.saved.path.read_text())
                self.assertNotIn("ephemeral-key", (self.root / "output/last_query.json").read_text())
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, "填写或保存 Key"):
                    app.start_query(CRITERIA)
        finally:
            app.close()

    def test_all_three_platforms_accept_broad_query_and_ccgp_sends_blank_keyword(self):
        self.saved.data["sources"] = [{"type": kind} for kind in
            ("jilin_ggzy", "ccgp_search", "national_ggzy", "ccgp_archive")]
        self.saved.path.write_text(json.dumps(self.saved.data))
        app = ConfigWebApp(self.saved.path)
        try:
            with patch.object(app.runner, "start"):
                query = app.start_query(CRITERIA, api_key="test-only")
            config = query_config(self.saved, CRITERIA, query["id"])
            source = CCGPSearchSource(self.client, config.data["sources"][1])
            class RequestCaptured(Exception):
                pass
            with patch.object(self.client, "request", side_effect=RequestCaptured) as request:
                with self.assertRaises(RequestCaptured):
                    list(source.iter_notices(date(2026, 8, 1), date(2026, 8, 31), Coverage(source.name)))
            from urllib.parse import urlsplit
            sent = parse_qs(urlsplit(request.call_args.args[0]).query, keep_blank_values=True)
            self.assertEqual([""], sent["kw"])
            self.assertEqual("ai_recall", query["criteria"]["mode"])
            self.assertEqual(4, len(query["sources"]))
        finally:
            app.close()


class ReviewValidationTests(unittest.TestCase):
    def raw(self, verdict="match", confidence=0.99, quote="原文医院"):
        return {"checks": {"industry": {"verdict": verdict, "confidence": confidence,
                                       "evidence": [quote]}}, "reason": "fixture"}

    def test_invented_evidence_and_low_confidence_are_not_confirmations(self):
        for raw in (self.raw(quote="不存在的银行"), self.raw(confidence=0.89)):
            result = QueryReviewer._validate(raw, {"industry": "医疗卫生"}, "原文医院", False)
            self.assertEqual("suspect", result["status"])

    def test_truncated_body_cannot_support_automatic_negative(self):
        result = QueryReviewer._validate(self.raw("no_match"), {"industry": "金融"}, "原文医院", True)
        self.assertEqual("suspect", result["status"])

    def test_missing_criteria_and_invalid_numbers_fail_validation(self):
        for raw in ({"checks": {}}, self.raw(confidence=float("nan")), self.raw(confidence=True)):
            with self.assertRaises(ValueError):
                QueryReviewer._validate(raw, {"industry": "金融"}, "原文医院", False)

    def test_modes_and_budgets_are_validated(self):
        for changes in ({"mode": "bogus"}, {"max_candidates": 0}, {"max_candidates": 501},
                        {"max_candidates": True}, {"max_candidates": 10.5}):
            with self.assertRaises(ValueError):
                normalise_query({**CRITERIA, **changes})


if __name__ == "__main__":
    unittest.main()
