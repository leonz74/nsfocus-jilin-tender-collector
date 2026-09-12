import json
import unittest
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from tender_downloader.config import AppConfig
from tender_downloader.geography import infer_location, query_region, region_catalog
from tender_downloader.http_client import HttpResult
from tender_downloader.models import Coverage
from tender_downloader.query import normalise_query, query_config
from tender_downloader.sources.ccgp_search import CCGPSearchSource
from tender_downloader.sources.national_ggzy import NationalGGZYSource
from tender_downloader.sources.okcis import search_action

BASE = {"start_date": "2026-09-01", "end_date": "2026-09-11", "city": "", "keyword": "", "industry": "", "notice_type": ""}


class GeographyTests(unittest.TestCase):
    def test_national_does_not_inherit_jilin_and_old_queries_still_do(self):
        self.assertEqual("吉林省", query_region(normalise_query(BASE))["province"])
        nation = normalise_query({**BASE, "province": ""})
        self.assertEqual("", nation["province"])
        self.assertEqual("", query_region(nation)["province_code"])
        self.assertEqual("长春市", query_region({**BASE, "city": "长春"})["city"])
        self.assertEqual("", query_region({**BASE, "city": "吉林省"})["city"])

    def test_catalogue_has_all_provinces_and_real_nested_cities_and_counties(self):
        catalog = region_catalog()
        self.assertEqual(34, len(catalog))
        gd = next(p for p in catalog if p["name"] == "广东省")
        sz = next(c for c in gd["children"] if c["name"] == "深圳市")
        self.assertIn("南山区", [d["name"] for d in sz["children"]])
        bj = next(p for p in catalog if p["name"] == "北京市")
        self.assertTrue(bj["municipality"])
        self.assertIn("海淀区", [d["name"] for d in bj["children"][0]["children"]])

    def test_invalid_parent_child_pairs_are_rejected(self):
        for changes in [
            {"province": "", "city": "深圳市"},
            {"province": "广东省", "city": "长春市"},
            {"province": "广东省", "district": "南山区"},
            {"province": "广东省", "city": "深圳市", "district": "海淀区"},
            {"province": "假省份"},
        ]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalise_query({**BASE, **changes})

    def test_location_uses_selected_province_and_does_not_guess_ambiguous_counties(self):
        self.assertEqual({"province": "广东省", "city": "深圳市", "district": "南山区"},
                         infer_location("广东-深圳", "", "深圳市南山区某学校"))
        self.assertEqual({"province": "北京市", "city": "北京市", "district": "朝阳区"},
                         infer_location("北京市", "", "朝阳区某学校"))
        self.assertEqual("", infer_location("", "朝阳区")["province"])
        self.assertEqual("长春市", infer_location("吉林省", "长春")["city"])
        self.assertEqual("吉林市", infer_location("吉林-吉林")["city"])

    def test_province_query_skips_jilin_only_source_without_modifying_saved_configuration(self):
        data = {"sources": [{"type": "jilin_ggzy"}, {"type": "ccgp_search"}, {"type": "national_ggzy"}]}
        effective = query_config(AppConfig(Path("/tmp/config.json"), data),
                                 {**BASE, "province": "广东省", "city": "深圳市", "district": "南山区"}, "a" * 32)
        self.assertIn("_query_unsupported", effective.data["sources"][0])
        self.assertEqual("440000", effective.data["sources"][1]["_query_region"]["province_code"])
        self.assertEqual("440300", effective.data["sources"][2]["_query_region"]["city_code"])
        self.assertNotIn("_query_region", data["sources"][0])

    def test_national_platform_sends_province_and_city_or_omits_them_for_nationwide(self):
        class Client:
            calls = []
            def post_form(self, url, fields, headers=None):
                self.calls.append(dict(fields))
                payload = {"code": 200, "data": {"pages": 1, "records": [{
                    "url": "/notice.html", "publishTime": "2026-09-10", "provinceText": "广东",
                    "cityText": "深圳", "title": "项目", "id": "one",
                }]}}
                return HttpResult(url, 200, {}, json.dumps(payload).encode())
        for province, city in [("广东省", "深圳市"), ("", "")]:
            client = Client(); client.calls = []
            source = NationalGGZYSource(client, {"source_types": ["1"], "deal_types": ["01"],
                "window_days": 366, "_query_region": query_region({"province": province, "city": city})})
            notices = list(source.iter_notices(date(2026, 9, 1), date(2026, 9, 11), Coverage(source.name)))
            self.assertEqual(1, len(notices))
            if province:
                self.assertEqual("440000", client.calls[0]["DEAL_PROVINCE"])
                self.assertEqual("440300", client.calls[0]["DEAL_CITY"])
            else:
                self.assertNotIn("DEAL_PROVINCE", client.calls[0])
                self.assertNotIn("DEAL_CITY", client.calls[0])

    def test_ccgp_search_changes_zone_and_omits_jilin_for_nationwide(self):
        class Client:
            def request(self, url, **kwargs):
                self.url = url
                body = '<div class="vT-srch-result-list-bid"></div>共找到<span>0</span>条内容'
                return HttpResult(url, 200, {"content-type": "text/html; charset=utf-8"}, body.encode())
        for province in ["广东省", ""]:
            client = Client()
            source = CCGPSearchSource(client, {"_query_terms": [""], "window_days": 366,
                "_query_region": query_region({"province": province})})
            self.assertEqual([], list(source.iter_notices(date(2026, 9, 1), date(2026, 9, 11), Coverage(source.name))))
            fields = parse_qs(urlsplit(client.url).query)
            if province:
                self.assertEqual(["44"], fields["zoneId"])
                self.assertEqual(["广东省"], fields["displayZone"])
            else:
                self.assertNotIn("zoneId", fields)
                self.assertNotIn("displayZone", fields)

    def test_okcis_uses_selected_region_and_has_an_explicit_national_option(self):
        action = search_action("测试", date(2026, 9, 1), date(2026, 9, 11), region_code="440000")
        self.assertIn('"440000"', action)
        self.assertNotIn('"220000"', action)
        self.assertIn('#city-result-city-input-quanguo', action)
