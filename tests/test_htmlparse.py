from __future__ import annotations

import unittest

from tender_downloader.htmlparse import parse_html_document


class HTMLParseTests(unittest.TestCase):
    def test_extracts_text_and_public_attachments(self) -> None:
        raw = """
        <html><body>
          <h1>网络安全等保测评采购公告</h1>
          <script>ignore me</script>
          <a href="/files/spec.pdf">附件：招标文件.pdf</a>
          <a href="javascript:void(0)">无效链接</a>
          <a href="/news/1.html">普通正文链接</a>
          <iframe src="/files/preview.pdf" title="预览文件"></iframe>
          <button data-url="/api/file?id=3" title="技术要求.docx">获取</button>
          <a href="#" data-url="/download?id=4">附件：参数文件</a>
          <a href="javascript:void(0)" onclick="window.open('/files/extra.pdf')">PDF</a>
        </body></html>
        """.encode()
        parsed = parse_html_document(raw, "https://example.gov.cn/notice/1.html")
        self.assertIn("网络安全等保测评采购公告", parsed.text)
        self.assertNotIn("ignore me", parsed.text)
        self.assertEqual(5, len(parsed.attachments))
        urls = {item.url for item in parsed.attachments}
        self.assertIn("https://example.gov.cn/download?id=4", urls)
        self.assertIn("https://example.gov.cn/files/extra.pdf", urls)
        self.assertEqual("https://example.gov.cn/files/spec.pdf", parsed.attachments[0].url)
        self.assertEqual(
            "https://example.gov.cn/notice/1.html",
            parsed.attachments[0].referer,
        )


if __name__ == "__main__":
    unittest.main()
