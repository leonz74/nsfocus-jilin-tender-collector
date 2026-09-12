from __future__ import annotations

import json
import unittest
from datetime import date

from tender_downloader.http_client import HttpResult
from tender_downloader.models import Coverage, Notice, RawDocument
from tender_downloader.sources.ccgp_archive import CCGPArchiveSource
from tender_downloader.sources.ccgp_search import CCGPSearchSource
from tender_downloader.sources.jilin_ggzy import JilinGGZYSource
from tender_downloader.sources.national_ggzy import NationalGGZYSource


class _FakeClient:
    def __init__(self, get_results=None, post_results=None):
        self.get_results = list(get_results or [])
        self.post_results = list(post_results or [])

    def request(self, url, **kwargs):
        return self.get_results.pop(0)

    def post_form(self, url, fields, headers=None):
        return self.post_results.pop(0)


class SourceParserTests(unittest.TestCase):
    def test_ccgp_archive_filters_jilin(self) -> None:
        html = """
        <li><a href="./202608/t20260818_123.htm" target="_blank" title="吉林网络安全项目">吉林...</a>
        发布时间：<em>2026-08-18 09:30</em> 地域：<em>吉林</em> 采购人：<em>吉林某大学</em></li>
        <li><a href="./202608/t20260818_124.htm" title="外省项目">外省项目</a>
        发布时间：<em>2026-08-18 09:20</em> 地域：<em>辽宁</em> 采购人：<em>某单位</em></li>
        """.encode("utf-8")
        client = _FakeClient([HttpResult("u", 200, {"content-type": "text/html; charset=utf-8"}, html)])
        source = CCGPArchiveSource(client, {
            "authority_rank": 95,
            "scopes": ["zygg"],
            "categories": ["gkzb"],
            "max_pages_per_category": 1,
        })
        coverage = Coverage(source=source.name)
        notices = list(source.iter_notices(date(2026, 1, 1), date(2026, 8, 18), coverage))
        self.assertEqual(1, len(notices))
        self.assertEqual("吉林网络安全项目", notices[0].title)
        self.assertEqual("吉林某大学", notices[0].buyer)

    def test_jilin_jsonp(self) -> None:
        payload = {
            "recordnum": 1,
            "datas": [{
                "title": "网络安全服务采购",
                "timestamp": "2026.08.18 09:00",
                "docpuburl": "http://www.jl.gov.cn/ggzy/test.html",
                "area": "吉林省",
            }],
        }
        response = HttpResult("u", 200, {}, f"result({json.dumps(payload, ensure_ascii=False)})".encode())
        # 每个频道会请求一次；复制足量的空结果，首频道先返回样本。
        empty = HttpResult("u", 200, {}, b'result({"recordnum":0,"datas":[]})')
        client = _FakeClient([response] + [empty] * 20)
        source = JilinGGZYSource(client, {"authority_rank": 100, "max_pages_per_channel": 1})
        coverage = Coverage(source=source.name)
        notices = list(source.iter_notices(date(2026, 1, 1), date(2026, 8, 18), coverage))
        self.assertEqual(1, len(notices))
        self.assertEqual("网络安全服务采购", notices[0].title)
        self.assertEqual("https://www.jl.gov.cn/ggzy/test.html", notices[0].url)

    def test_jilin_https_upgrade_is_limited_to_exact_official_hosts(self) -> None:
        html = b"""
        <a href="http://www.jl.gov.cn/ggzy/files/original.doc">official.doc</a>
        <a href="http://www.jl.gov.cn.evil.example/files/lookalike.doc">lookalike.doc</a>
        <a href="http://files.example.com/third-party.doc">third-party.doc</a>
        """
        source = JilinGGZYSource(_FakeClient(), {"authority_rank": 100})
        notice = Notice(
            source=source.name,
            authority_rank=100,
            external_id="candidate",
            title="网络安全服务采购",
            published_at="2026-08-18",
            url="https://www.jl.gov.cn/ggzy/test.html",
        )
        detail = RawDocument(
            body=html,
            url="https://www.jl.gov.cn/ggzy/test.html",
            headers={"content-type": "text/html; charset=utf-8"},
        )

        artifacts = source.discover_artifacts(notice, detail)

        self.assertEqual(
            "https://www.jl.gov.cn/ggzy/files/original.doc",
            artifacts[0].url,
        )
        self.assertEqual(
            "http://www.jl.gov.cn.evil.example/files/lookalike.doc",
            artifacts[1].url,
        )
        self.assertEqual(
            "http://files.example.com/third-party.doc",
            artifacts[2].url,
        )

    def test_ccgp_search_date_window_and_buyer(self) -> None:
        page = """
        <ul class="vT-srch-result-list-bid"><li>
          <a href="https://www.ccgp.gov.cn/cggg/dfgg/gkzb/202608/t20260818_456.htm">
            吉林大学等保测评服务
          </a>
          <p>投标截止时间为2026-09-11 09:30，请按时提交。</p>
          <span>2026.08.18 10:00:00 | 采购人：吉林大学 | 代理机构：某公司</span>
        </li></ul>
        <div>共找到 <span style="color:#c00000">1</span> 条内容</div>
        <script>Pager({ size: 1, current: 0, prefix: 'data2', suffix: '.jsp&' });</script>
        """.encode("utf-8")
        response = HttpResult(
            "https://search.ccgp.gov.cn/bxsearch",
            200,
            {"content-type": "text/html; charset=utf-8"},
            page,
        )
        client = _FakeClient([response])
        source = CCGPSearchSource(client, {
            "authority_rank": 95,
            "keywords": ["等保"],
            "window_days": 31,
            "max_pages_per_query": 1,
            "expected_page_size": 20,
        })
        coverage = Coverage(source=source.name)
        notices = list(source.iter_notices(date(2026, 8, 18), date(2026, 8, 18), coverage))
        self.assertEqual(1, len(notices))
        self.assertEqual("吉林大学", notices[0].buyer)
        self.assertEqual("2026-08-18", notices[0].published_at)
        self.assertTrue(coverage.reached_start)

    def test_ccgp_search_zero_results_without_pager_is_complete(self) -> None:
        page = """
        <div>共找到 <span style="color:#c00000">0</span> 条内容</div>
        <ul class="vT-srch-result-list-bid"></ul>
        """.encode("utf-8")
        response = HttpResult(
            "https://search.ccgp.gov.cn/bxsearch", 200,
            {"content-type": "text/html; charset=utf-8"}, page,
        )
        source = CCGPSearchSource(_FakeClient([response]), {
            "keywords": ["等保"], "window_days": 1, "max_pages_per_query": 1,
        })
        coverage = Coverage(source=source.name)
        notices = list(source.iter_notices(date(2026, 8, 18), date(2026, 8, 18), coverage))
        self.assertEqual([], notices)
        self.assertTrue(coverage.reached_start)

    def test_national_api_record(self) -> None:
        payload = {
            "code": 200,
            "data": {
                "records": [{
                    "id": "abc",
                    "title": "数据安全项目",
                    "publishTime": "2026-08-18",
                    "url": "/information/test.shtml",
                    "provinceText": "吉林省",
                    "businessTypeText": "政府采购",
                    "informationTypeText": "采购公告",
                }],
                "pages": 1,
            },
        }
        response = HttpResult("u", 200, {}, json.dumps(payload, ensure_ascii=False).encode())
        # 两种来源类型中的合法组合共三次。
        client = _FakeClient(post_results=[response, response, response])
        source = NationalGGZYSource(client, {
            "authority_rank": 90,
            "source_types": ["1", "3"],
            "deal_types": ["01", "02"],
            "window_days": 30,
            "max_pages_per_window": 1,
        })
        coverage = Coverage(source=source.name)
        notices = list(source.iter_notices(date(2026, 8, 18), date(2026, 8, 18), coverage))
        self.assertEqual(1, len(notices))
        self.assertEqual("https://www.ggzy.gov.cn/information/test.shtml", notices[0].url)


if __name__ == "__main__":
    unittest.main()
