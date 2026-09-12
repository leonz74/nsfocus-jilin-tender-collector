from __future__ import annotations

import io
import struct
import unittest
import zipfile
import zlib

from tender_downloader.content_gate import (
    ContentGateError,
    ContentGateInput,
    ContentGatePolicy,
    evaluate_content,
)
from tender_downloader.official_registry import SourceRole


OFFICIAL_URL = "https://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details?id=test"
TITLE = "中国人民银行吉林省分行网络安全设备维保服务项目采购意向"
BUYER = "中国人民银行吉林省分行"
PROJECT_CODE = "JL-CY-2026-019"


def official_html(extra: str = "") -> bytes:
    return (
        "<!doctype html><html><head><title>采购意向</title></head><body>"
        f"<h1>{TITLE}</h1><p>采购人：{BUYER}</p>"
        f"<p>项目编号：{PROJECT_CODE}</p><p>采购意向，预算金额14万元，"
        "采购需求为网络安全设备维保服务，预计采购时间2026年10月。</p>"
        f"{extra}</body></html>"
    ).encode("utf-8")


def make_docx(text: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "word/document.xml",
            (
                '<?xml version="1.0"?><w:document '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    return output.getvalue()


def make_xlsx() -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        )
        archive.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"/>',
        )
    return output.getvalue()


def make_cfb(stream_name: str) -> bytes:
    """Build a minimal CFB directory fixture without real tender content."""

    free_sector = 0xFFFFFFFF
    end_of_chain = 0xFFFFFFFE
    fat_sector = 0xFFFFFFFD
    header = bytearray(512)
    header[:8] = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    struct.pack_into("<H", header, 0x18, 0x003E)
    struct.pack_into("<H", header, 0x1A, 3)
    header[0x1C:0x1E] = b"\xfe\xff"
    struct.pack_into("<H", header, 0x1E, 9)
    struct.pack_into("<H", header, 0x20, 6)
    struct.pack_into("<I", header, 0x2C, 1)
    struct.pack_into("<I", header, 0x30, 1)
    struct.pack_into("<I", header, 0x38, 4096)
    struct.pack_into("<I", header, 0x3C, end_of_chain)
    struct.pack_into("<I", header, 0x44, end_of_chain)
    for index in range(109):
        struct.pack_into("<I", header, 0x4C + index * 4, free_sector)
    struct.pack_into("<I", header, 0x4C, 0)

    fat = bytearray(b"\xff" * 512)
    struct.pack_into("<I", fat, 0, fat_sector)
    struct.pack_into("<I", fat, 4, end_of_chain)
    directory = bytearray(512)

    def directory_entry(offset: int, name: str, object_type: int) -> None:
        encoded = (name + "\0").encode("utf-16le")
        directory[offset : offset + len(encoded)] = encoded
        struct.pack_into("<H", directory, offset + 64, len(encoded))
        directory[offset + 66] = object_type

    directory_entry(0, "Root Entry", 5)
    directory_entry(128, stream_name, 2)
    return bytes(header + fat + directory)


def make_7z_header() -> bytes:
    start_header = b"\x00" * 20
    crc = zlib.crc32(start_header) & 0xFFFFFFFF
    return b"7z\xbc\xaf\x27\x1c\x00\x04" + struct.pack("<I", crc) + start_header


def make_rar4_header() -> bytes:
    main_header_without_crc = b"\x73\x00\x00\x0d\x00" + b"\x00" * 6
    crc = zlib.crc32(main_header_without_crc) & 0xFFFF
    return b"Rar!\x1a\x07\x00" + struct.pack("<H", crc) + main_header_without_crc


def padded_html(prefix: str, size: int) -> bytes:
    encoded = prefix.encode("utf-8")
    suffix = b"</body></html>"
    if not encoded.endswith(suffix):
        raise AssertionError("fixture must end with HTML suffix")
    padding_size = size - len(encoded)
    if padding_size < 7:
        raise AssertionError("requested synthetic fixture is too small")
    return encoded[: -len(suffix)] + b"<!--" + (b"x" * (padding_size - 7)) + b"-->" + suffix


def request(**overrides) -> ContentGateInput:
    values = {
        "source_role": "official",
        "origin": OFFICIAL_URL,
        "final_url": OFFICIAL_URL,
        "content_type": "text/html; charset=utf-8",
        "body": official_html(),
        "analysis_body": official_html("<p>打印页面</p>"),
        "expected_title": TITLE,
        "expected_buyer": BUYER,
        "expected_project_code": PROJECT_CODE,
        "document_kind": "采购意向",
    }
    values.update(overrides)
    return ContentGateInput(**values)


class ContentGateTests(unittest.TestCase):
    def assert_rejected_with(self, result, code: ContentGateError) -> None:
        self.assertEqual(result.verdict, "reject")
        self.assertFalse(result.deliverable)
        self.assertIn(str(code), result.error_codes)

    def test_allows_official_raw_html_with_matching_semantics(self) -> None:
        result = evaluate_content(request())
        self.assertTrue(result.allowed, result)
        self.assertEqual(result.detected_kind, "html")
        self.assertEqual(result.error_codes, ())
        self.assertEqual(len(result.raw_sha256), 64)

    def test_rejects_synthetic_1844_byte_captcha_even_if_rendered_page_is_valid(self) -> None:
        # Models the observed failure without opening or copying the sensitive file.
        raw = padded_html(
            "<!doctype html><html><head><title>访问验证</title></head><body>"
            '<div id="captcha">请输入验证码，请完成安全验证</div>'
            '<script src="https://captcha.example/challenge.js"></script></body></html>',
            1844,
        )
        self.assertEqual(len(raw), 1844)
        result = evaluate_content(request(body=raw, analysis_body=official_html()))
        self.assert_rejected_with(result, ContentGateError.CAPTCHA_PAGE)
        self.assertIn(str(ContentGateError.RAW_RENDER_MISMATCH), result.error_codes)

    def test_rejects_synthetic_118787_byte_okcis_aggregator_with_full_copied_text(self) -> None:
        copied = (
            "<!doctype html><html><head><title>招标采购导航网</title></head><body>"
            "<nav>OKCIS 会员中心 保存 导出Word 导出PDF</nav>"
            f"<h1>{TITLE}</h1><p>采购人：{BUYER}</p><p>项目编号：{PROJECT_CODE}</p>"
            "<p>采购意向，预算金额14万元，网络安全设备维保服务。</p>"
            "</body></html>"
        )
        raw = padded_html(copied, 118787)
        self.assertEqual(len(raw), 118787)
        result = evaluate_content(
            request(
                source_role="official",  # Host defence still wins if the caller mislabels it.
                origin="https://www.okcis.cn/notice/123",
                final_url="https://www.okcis.cn/notice/123",
                body=raw,
                analysis_body=raw,
            )
        )
        self.assert_rejected_with(result, ContentGateError.COMMERCIAL_AGGREGATOR)
        self.assertIn(str(ContentGateError.UNREGISTERED_OFFICIAL_HOST), result.error_codes)

    def test_commercial_source_role_never_delivers_even_on_official_url(self) -> None:
        result = evaluate_content(request(source_role="commercial_aggregator"))
        self.assert_rejected_with(result, ContentGateError.COMMERCIAL_AGGREGATOR)

        registry_role_result = evaluate_content(request(source_role=SourceRole.COMMERCIAL_LEAD))
        self.assert_rejected_with(registry_role_result, ContentGateError.COMMERCIAL_AGGREGATOR)

    def test_official_registry_enum_role_is_accepted(self) -> None:
        result = evaluate_content(request(source_role=SourceRole.OFFICIAL))
        self.assertTrue(result.allowed, result)

    def test_known_aggregator_subdomain_is_matched_on_domain_boundary(self) -> None:
        policy = ContentGatePolicy(official_hosts=frozenset({"member.okcis.cn"}))
        result = evaluate_content(
            request(
                origin="https://member.okcis.cn/notice/123",
                final_url="https://member.okcis.cn/notice/123",
            ),
            policy,
        )
        self.assert_rejected_with(result, ContentGateError.COMMERCIAL_AGGREGATOR)

    def test_rejects_unregistered_host_and_lookalike_subdomain(self) -> None:
        result = evaluate_content(
            request(
                origin="https://evil.ccgp.gov.cn/notice",
                final_url="https://evil.ccgp.gov.cn/notice",
            )
        )
        self.assert_rejected_with(result, ContentGateError.UNREGISTERED_OFFICIAL_HOST)

    def test_rejects_cross_domain_redirect_to_lookalike(self) -> None:
        result = evaluate_content(request(final_url="https://ccgp.gov.cn.evil.example/file.html"))
        self.assert_rejected_with(result, ContentGateError.CROSS_ORIGIN_REDIRECT)
        self.assertIn(str(ContentGateError.UNREGISTERED_OFFICIAL_HOST), result.error_codes)

    def test_explicit_official_file_host_pair_can_be_registered(self) -> None:
        policy = ContentGatePolicy(
            official_hosts=frozenset({"cgyx.ccgp.gov.cn", "files.ccgp.example"}),
            allowed_redirect_pairs=frozenset({("cgyx.ccgp.gov.cn", "files.ccgp.example")}),
        )
        pdf = b"%PDF-1.7\n1 0 obj <<>> endobj\n%%EOF"
        result = evaluate_content(
            request(
                final_url="https://files.ccgp.example/original.pdf",
                content_type="application/pdf",
                body=pdf,
                analysis_body=None,
                document_kind="pdf",
            ),
            policy,
        )
        self.assertTrue(result.allowed, result)

    def test_rejects_login_and_waf_responses(self) -> None:
        cases = (
            (
                "<!doctype html><html><body><h1>用户登录</h1>请先登录"
                '<input type="password"></body></html>',
                ContentGateError.LOGIN_PAGE,
            ),
            (
                "<!doctype html><html><body><h1>Access Denied</h1>WAF challenge request blocked</body></html>",
                ContentGateError.WAF_BLOCK_PAGE,
            ),
        )
        for raw, code in cases:
            with self.subTest(code=code):
                result = evaluate_content(request(body=raw.encode(), analysis_body=None))
                self.assert_rejected_with(result, code)

    def test_rejects_empty_spa_shell(self) -> None:
        raw = b'<!doctype html><html><body><div id="root"></div><script src="app.js"></script></body></html>'
        result = evaluate_content(request(body=raw, analysis_body=None, expected_title="", expected_buyer="", expected_project_code="", document_kind="html"))
        self.assert_rejected_with(result, ContentGateError.EMPTY_SPA_SHELL)

    def test_rejects_substantive_raw_render_semantic_mismatch(self) -> None:
        unrelated = (
            "<!doctype html><html><body><h1>长春市办公家具采购公告</h1>"
            "<p>采购人长春市某单位，项目编号CC-FURNITURE-88，预算内容为桌椅柜。"
            "本公告提供项目概况、投标截止时间、开标地点和联系方式。</p></body></html>"
        ).encode()
        result = evaluate_content(request(body=unrelated, analysis_body=official_html()))
        self.assert_rejected_with(result, ContentGateError.RAW_RENDER_MISMATCH)

    def test_rejects_pdf_mime_with_html_body(self) -> None:
        result = evaluate_content(
            request(
                final_url=OFFICIAL_URL + ".pdf",
                content_type="application/pdf",
                body=official_html(),
                analysis_body=None,
                document_kind="pdf",
            )
        )
        self.assert_rejected_with(result, ContentGateError.MIME_MAGIC_MISMATCH)
        self.assertIn(str(ContentGateError.DOCUMENT_KIND_MISMATCH), result.error_codes)

    def test_rejects_truncated_pdf(self) -> None:
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/a.pdf",
                content_type="application/pdf",
                body=b"%PDF-1.7\n1 0 obj <<>> endobj",
                analysis_body=None,
                document_kind="pdf",
            )
        )
        self.assert_rejected_with(result, ContentGateError.MIME_MAGIC_MISMATCH)

    def test_allows_official_direct_pdf_original(self) -> None:
        pdf = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF"
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/tender.pdf",
                content_type="application/pdf",
                body=pdf,
                analysis_body=None,
                document_kind="official_attachment",
            )
        )
        self.assertTrue(result.allowed, result)

    def test_allows_structurally_valid_official_docx_original(self) -> None:
        document = make_docx(f"{TITLE} {BUYER} {PROJECT_CODE}")
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/tender.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                body=document,
                analysis_body=None,
                document_kind="docx",
            )
        )
        self.assertTrue(result.allowed, result)

    def test_allows_structurally_valid_official_legacy_doc_and_xls(self) -> None:
        cases = (
            ("WordDocument", ".doc", "application/msword", "doc"),
            ("Workbook", ".xls", "application/vnd.ms-excel", "xls"),
        )
        for stream, suffix, mime, kind in cases:
            with self.subTest(kind=kind):
                result = evaluate_content(
                    request(
                        final_url=f"https://cgyx.ccgp.gov.cn/files/tender{suffix}",
                        content_type=mime,
                        body=make_cfb(stream),
                        analysis_body=None,
                        document_kind=kind,
                    )
                )
                self.assertTrue(result.allowed, result)
                self.assertEqual(result.detected_kind, kind)

    def test_rejects_legacy_office_mime_or_suffix_confusion(self) -> None:
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/tender.xls",
                content_type="application/vnd.ms-excel",
                body=make_cfb("WordDocument"),
                analysis_body=None,
                document_kind="official_attachment",
            )
        )
        self.assert_rejected_with(result, ContentGateError.MIME_MAGIC_MISMATCH)

    def test_allows_structurally_valid_official_xlsx(self) -> None:
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/pricing.xlsx",
                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                body=make_xlsx(),
                analysis_body=None,
                document_kind="xlsx",
            )
        )
        self.assertTrue(result.allowed, result)
        self.assertEqual(result.detected_kind, "xlsx")

    def test_allows_ooxml_served_with_generic_zip_mime_after_structure_check(self) -> None:
        cases = (("docx", make_docx(TITLE)), ("xlsx", make_xlsx()))
        for suffix, body in cases:
            with self.subTest(kind=suffix):
                result = evaluate_content(
                    request(
                        final_url=f"https://cgyx.ccgp.gov.cn/files/tender.{suffix}",
                        content_type="application/zip",
                        body=body,
                        analysis_body=None,
                        expected_title="",
                        expected_buyer="",
                        expected_project_code="",
                        document_kind=suffix,
                    )
                )
                self.assertTrue(result.allowed, result)

    def test_allows_official_zip_rar_and_7z_by_container_signature(self) -> None:
        zip_output = io.BytesIO()
        with zipfile.ZipFile(zip_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("采购文件.txt", "公开采购文件测试内容")
        cases = (
            ("zip", "application/zip", zip_output.getvalue()),
            ("rar", "application/vnd.rar", make_rar4_header()),
            ("7z", "application/x-7z-compressed", make_7z_header()),
        )
        for suffix, mime, body in cases:
            with self.subTest(kind=suffix):
                result = evaluate_content(
                    request(
                        final_url=f"https://cgyx.ccgp.gov.cn/files/package.{suffix}",
                        content_type=mime,
                        body=body,
                        analysis_body=None,
                        document_kind="official_attachment",
                    )
                )
                self.assertTrue(result.allowed, result)
                self.assertEqual(result.detected_kind, suffix)

    def test_rejects_html_login_response_disguised_as_each_attachment_format(self) -> None:
        fake = (
            "<!doctype html><html><head><title>用户登录</title></head><body>"
            '请先登录<input type="password"></body></html>'
        ).encode()
        cases = (
            ("pdf", "application/pdf"),
            ("doc", "application/msword"),
            ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            ("xls", "application/vnd.ms-excel"),
            ("xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            ("zip", "application/zip"),
            ("rar", "application/vnd.rar"),
            ("7z", "application/x-7z-compressed"),
        )
        for suffix, mime in cases:
            with self.subTest(kind=suffix):
                result = evaluate_content(
                    request(
                        final_url=f"https://cgyx.ccgp.gov.cn/files/fake.{suffix}",
                        content_type=mime,
                        body=fake,
                        analysis_body=None,
                        expected_title="",
                        expected_buyer="",
                        expected_project_code="",
                        document_kind="official_attachment",
                    )
                )
                self.assert_rejected_with(result, ContentGateError.LOGIN_PAGE)
                self.assertIn(str(ContentGateError.MIME_MAGIC_MISMATCH), result.error_codes)

    def test_rejects_invalid_or_cross_labeled_archives(self) -> None:
        invalid_7z = b"7z\xbc\xaf\x27\x1c" + b"\x00" * 26
        cases = (
            ("archive.rar", "application/vnd.rar", b"Rar!\x1a\x07\x00"),
            ("archive.7z", "application/x-7z-compressed", invalid_7z),
            ("archive.zip", "application/zip", b"PK\x03\x04broken central directory"),
            ("archive.zip", "application/zip", make_7z_header()),
        )
        for name, mime, body in cases:
            with self.subTest(name=name, mime=mime, body=body[:8]):
                result = evaluate_content(
                    request(
                        final_url=f"https://cgyx.ccgp.gov.cn/files/{name}",
                        content_type=mime,
                        body=body,
                        analysis_body=None,
                        expected_title="",
                        expected_buyer="",
                        expected_project_code="",
                        document_kind="official_attachment",
                    )
                )
                self.assertFalse(result.allowed, result)
                self.assertTrue(
                    str(ContentGateError.MIME_MAGIC_MISMATCH) in result.error_codes
                    or str(ContentGateError.UNSUPPORTED_MEDIA_TYPE) in result.error_codes,
                    result,
                )

    def test_rejects_plain_zip_renamed_docx(self) -> None:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            archive.writestr("payload.txt", "not a Word document")
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/files/fake.docx",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                body=output.getvalue(),
                analysis_body=None,
                document_kind="docx",
            )
        )
        self.assert_rejected_with(result, ContentGateError.MIME_MAGIC_MISMATCH)

    def test_allows_official_json_with_expected_semantics(self) -> None:
        body = (
            '{"title":"' + TITLE + '","buyer":"' + BUYER + '","projectCode":"' + PROJECT_CODE + '",'
            '"type":"采购意向","content":"网络安全设备维保，预算14万元"}'
        ).encode()
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/api/notice.json",
                content_type="application/json; charset=utf-8",
                body=body,
                analysis_body=body,
                document_kind="采购意向",
            )
        )
        self.assertTrue(result.allowed, result)

    def test_rejects_malformed_json(self) -> None:
        result = evaluate_content(
            request(
                final_url="https://cgyx.ccgp.gov.cn/api/notice.json",
                content_type="application/json",
                body=b'{"title":',
                analysis_body=None,
                expected_title="",
                expected_buyer="",
                expected_project_code="",
                document_kind="json",
            )
        )
        self.assert_rejected_with(result, ContentGateError.MALFORMED_JSON)

    def test_rejects_each_semantic_mismatch_from_raw_bytes(self) -> None:
        cases = (
            ({"expected_title": "不存在的网络安全项目标题"}, ContentGateError.TITLE_MISMATCH),
            ({"expected_buyer": "不存在的采购单位"}, ContentGateError.BUYER_MISMATCH),
            ({"expected_project_code": "WRONG-2026-001"}, ContentGateError.PROJECT_CODE_MISMATCH),
            ({"document_kind": "中标公告"}, ContentGateError.DOCUMENT_KIND_MISMATCH),
        )
        for overrides, code in cases:
            with self.subTest(code=code):
                result = evaluate_content(request(**overrides))
                self.assert_rejected_with(result, code)

    def test_result_has_no_override_channel_for_ai(self) -> None:
        captcha = "<!doctype html><html><body>请输入验证码，请完成安全验证</body></html>".encode()
        result = evaluate_content(request(body=captcha, analysis_body=official_html()))
        self.assertFalse(result.allowed)
        self.assertFalse(hasattr(result, "ai_override"))


if __name__ == "__main__":
    unittest.main()
