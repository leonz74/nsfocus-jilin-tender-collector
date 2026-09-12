from __future__ import annotations

import unittest
from pathlib import Path

from tender_downloader.field_extract import (
    extract_structured_fields,
    scope_text_for_notice,
)
from tender_downloader.htmlparse import parse_html_document
from tender_downloader.models import Notice


FIXTURES = Path(__file__).with_name("fixtures")


class FieldExtractTests(unittest.TestCase):
    @staticmethod
    def _notice(title: str, notice_type: str = "") -> Notice:
        return Notice(
            source="fixture",
            authority_rank=100,
            external_id="1",
            title=title,
            published_at="2026-08-12",
            url="https://example.gov.cn/notice/1",
            buyer="测试采购人",
            notice_type=notice_type,
        )

    def test_extracts_procurement_intention_without_fabricating_award(self) -> None:
        notice = self._notice("网络安全设备维保服务项目政府采购意向")
        result = extract_structured_fields(
            notice,
            """
            预算金额：14.000000万元(人民币)
            采购品目：C16070200 硬件运维服务
            采购需求概况：网络安全设备维保。
            预计采购时间：2026-10
            """,
        )

        self.assertEqual("采购意向", result.event_type)
        self.assertEqual(14_000_000, result.budget_amount_minor)
        self.assertEqual("2026-10", result.expected_purchase_date)
        self.assertIsNone(result.award_amount_minor)
        self.assertEqual("", result.winning_vendor)

    def test_extracts_award_vendor_amount_and_project_code(self) -> None:
        notice = self._notice("网络安全平台中标结果公告", "中标公告")
        result = extract_structured_fields(
            notice,
            """
            一、项目编号：JLSZC20260006605
            三、中标信息：
            供应商名称：中国移动通信集团吉林有限公司
            中标金额：1,309,000.00 元
            中标日期：2026年04月20日
            """,
        )

        self.assertEqual("中标公告", result.event_type)
        self.assertEqual("JLSZC20260006605", result.project_code)
        self.assertEqual("中国移动通信集团吉林有限公司", result.winning_vendor)
        self.assertEqual(130_900_000, result.award_amount_minor)
        self.assertEqual("2026-04-20", result.award_at)

    def test_keeps_budget_limit_and_award_separate(self) -> None:
        notice = self._notice("网络安全服务采购公告", "采购公告")
        result = extract_structured_fields(
            notice,
            "预算金额（元）：2,164,878.00\n最高限价（元）：1,310,850.00",
        )

        self.assertEqual(216_487_800, result.budget_amount_minor)
        self.assertEqual(131_085_000, result.max_price_minor)
        self.assertIsNone(result.award_amount_minor)

    def test_scopes_ccgp_group_row_before_inferring_budget(self) -> None:
        notice = self._notice(
            "中国人民银行吉林省分行2026年8至12月政府采购意向-网络安全设备维保服务项目",
            "采购意向",
        )
        group_text = """
        序号
        采购单位
        采购项目名称
        预算金额(万元)
        18
        测试采购人
        虚拟化平台硬件设备维保项目
        C16070200硬件运维服务
        62.120000
        2026年10月
        19
        测试采购人
        网络安全设备维保服务项目
        C16070200硬件运维服务
        详见项目详情
        14.000000
        2026年10月
        详见项目详情
        20
        测试采购人
        网络租赁项目
        101.060000
        2026年10月
        """
        scoped = scope_text_for_notice(notice, group_text)
        result = extract_structured_fields(notice, scoped)

        self.assertIn("网络安全设备维保服务项目", scoped)
        self.assertNotIn("101.060000", scoped)
        self.assertEqual(14_000_000, result.budget_amount_minor)
        self.assertEqual("2026-10", result.expected_purchase_date)

    def test_invalid_calendar_date_is_not_emitted(self) -> None:
        notice = self._notice("网络安全中标公告", "中标公告")
        result = extract_structured_fields(notice, "中标日期：2026年02月30日")
        self.assertEqual("", result.award_at)

    def test_extracts_real_jilin_procurement_tender_template(self) -> None:
        notice = self._notice(
            "吉林省教育考试院2026年网络安全服务项目", "采购公告"
        )
        raw = (FIXTURES / "jilin_procurement_tender_minimal.html").read_bytes()
        text = parse_html_document(raw, notice.url).text

        result = extract_structured_fields(notice, text)

        self.assertEqual("采购计划-[2026]-13087号-JLRX-20260622", result.project_code)
        self.assertEqual(82_840_000, result.budget_amount_minor)
        self.assertEqual(82_840_000, result.max_price_minor)
        self.assertIn("预算金额", result.evidence["budget_amount_minor"])

    def test_extracts_real_jilin_procurement_award_table(self) -> None:
        notice = self._notice(
            "吉林省教育考试院2026年网络安全服务项目中标结果公告", "中标公告"
        )
        raw = (FIXTURES / "jilin_procurement_award_minimal.html").read_bytes()
        text = parse_html_document(raw, notice.url).text

        result = extract_structured_fields(notice, text)

        self.assertEqual("采购计划-[2026]-13087号-JLRX-20260622", result.project_code)
        self.assertEqual("长春雅信科技有限责任公司", result.winning_vendor)
        self.assertEqual(82_460_000, result.award_amount_minor)
        self.assertIn("长春雅信科技有限责任公司", result.evidence["winning_vendor"])
        self.assertIn("投标总价：824600（元）", result.evidence["award_amount_minor"])


if __name__ == "__main__":
    unittest.main()
