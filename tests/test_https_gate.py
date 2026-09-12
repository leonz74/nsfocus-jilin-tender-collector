from __future__ import annotations

import json
import unittest
from pathlib import Path

from tender_downloader.config import validate_config_data
from tender_downloader.content_gate import (
    ContentGateError,
    ContentGateInput,
    evaluate_content,
)


class HttpsDeliveryGateTests(unittest.TestCase):
    _BODY = (
        "<html><head><title>网络安全设备采购项目</title></head><body>"
        "<h1>网络安全设备采购项目</h1><p>吉林省测试单位采购防火墙、终端安全和日志审计设备。"
        "本公告为采购项目公开招标公告，供应商应按采购文件要求提交投标文件。</p></body></html>"
    ).encode("utf-8")

    def _verdict(self, origin: str, final_url: str):
        return evaluate_content(ContentGateInput(
            source_role="official",
            origin=origin,
            final_url=final_url,
            content_type="text/html; charset=utf-8",
            body=self._BODY,
            expected_title="网络安全设备采购项目",
            expected_buyer="吉林省测试单位",
            document_kind="official_html",
        ))

    def test_rejects_http_official_origin_even_on_registered_host(self) -> None:
        verdict = self._verdict(
            "http://www.ccgp.gov.cn/cggg/example.htm",
            "https://www.ccgp.gov.cn/cggg/example.htm",
        )
        self.assertFalse(verdict.allowed)
        self.assertIn(
            ContentGateError.INSECURE_ORIGIN_TRANSPORT.value,
            verdict.error_codes,
        )

    def test_rejects_http_final_response_even_from_https_origin(self) -> None:
        verdict = self._verdict(
            "https://www.ccgp.gov.cn/cggg/example.htm",
            "http://www.ccgp.gov.cn/cggg/example.htm",
        )
        self.assertFalse(verdict.allowed)
        self.assertIn(
            ContentGateError.INSECURE_FINAL_TRANSPORT.value,
            verdict.error_codes,
        )

    def test_config_cannot_reenable_official_http_fallback(self) -> None:
        root = Path(__file__).resolve().parents[1]
        data = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
        data["provenance"]["allow_registered_http_fallback"] = True
        with self.assertRaisesRegex(ValueError, "只允许 HTTPS"):
            validate_config_data(data, require_ready=False)


if __name__ == "__main__":
    unittest.main()
