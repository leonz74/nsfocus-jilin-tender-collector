from __future__ import annotations

import unittest
from dataclasses import replace

from tender_downloader.official_registry import (
    DEFAULT_SOURCE_REGISTRY,
    SourceRegistration,
    SourceRegistry,
    SourceRole,
    UrlPolicyViolation,
    canonicalize_official_url,
    host_matches_domain,
    trusted_configured_official_host,
)
from tender_downloader.provenance import (
    CandidateDiscoveryMethod,
    OfficialCandidateKind,
    extract_matching_official_detail_candidate,
    extract_official_candidates,
    is_aggregator_export_link,
    validate_official_candidate,
)


PROJECT_URL = (
    "https://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details"
    "?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc"
)
GROUP_URL = (
    "https://cgyx.ccgp.gov.cn/cgyx/pub/details"
    "?groupId=c477f86f-22b3-422a-b711-ddc7bce5cbd1"
)
LEAD_URL = "https://www.okcis.cn/20260812-n2-security.html"
JILIN_ATTACHMENT_CDN_URL = (
    "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/"
    "1024FPA/2026/08/official-tender.pdf"
)


class OfficialRegistryTests(unittest.TestCase):
    def test_roles_are_explicit_for_official_and_commercial_sources(self) -> None:
        self.assertEqual(
            SourceRole.OFFICIAL,
            DEFAULT_SOURCE_REGISTRY.role_for_url("https://cgyx.ccgp.gov.cn/"),
        )
        self.assertEqual(
            SourceRole.OFFICIAL,
            DEFAULT_SOURCE_REGISTRY.role_for_url("https://jzcg.pbc.gov.cn/notice/1"),
        )
        self.assertEqual(
            SourceRole.COMMERCIAL_LEAD,
            DEFAULT_SOURCE_REGISTRY.role_for_url(LEAD_URL),
        )
        self.assertEqual(
            SourceRole.UNKNOWN,
            DEFAULT_SOURCE_REGISTRY.role_for_url("https://example.com/notice"),
        )

    def test_domain_matching_requires_a_dns_label_boundary(self) -> None:
        self.assertTrue(host_matches_domain("ccgp.gov.cn", "ccgp.gov.cn"))
        self.assertTrue(host_matches_domain("cgyx.ccgp.gov.cn", "ccgp.gov.cn"))
        self.assertFalse(host_matches_domain("fakeccgp.gov.cn", "ccgp.gov.cn"))
        self.assertFalse(host_matches_domain("ccgp.gov.cn.evil.test", "ccgp.gov.cn"))
        self.assertFalse(host_matches_domain("ccgp.gov.cn.", "ccgp.gov.cn"))

    def test_official_url_rejects_lookalikes_userinfo_and_ambiguous_syntax(self) -> None:
        bad_urls = (
            "https://ccgp.gov.cn.evil.test/notice",
            "https://fakeccgp.gov.cn/notice",
            "https://ccgp.gov.cn@evil.test/notice",
            "https://attacker@ccgp.gov.cn/notice",
            "https://ccgp.gov.cn\\@evil.test/notice",
            "https://ccgp.gov.cn:8443/notice",
            "https://ccgp.gov.cn./notice",
            "https://ccgp.gov.cn/notice\nhttps://evil.test/",
        )
        for url in bad_urls:
            with self.subTest(url=url):
                with self.assertRaises(UrlPolicyViolation):
                    canonicalize_official_url(url)

    def test_official_candidate_policy_is_https_only(self) -> None:
        with self.assertRaisesRegex(UrlPolicyViolation, "HTTPS"):
            canonicalize_official_url(
                "http://cgyx.ccgp.gov.cn/cgyx/pub/details?groupId=abcdefgh"
            )
        with self.assertRaises(UrlPolicyViolation):
            canonicalize_official_url("ftp://ccgp.gov.cn/notice")

    def test_canonicalization_removes_fragment_and_default_https_port(self) -> None:
        canonical, registration = canonicalize_official_url(
            "https://cgyx.ccgp.gov.cn:443/notice?id=1#section"
        )
        self.assertEqual("https://cgyx.ccgp.gov.cn/notice?id=1", canonical)
        self.assertEqual("ccgp", registration.key)

    def test_registry_refuses_duplicate_claimed_domain(self) -> None:
        one = SourceRegistration("one", "一", SourceRole.OFFICIAL, ("example.gov.cn",))
        two = SourceRegistration("two", "二", SourceRole.OFFICIAL, ("example.gov.cn",))
        with self.assertRaisesRegex(ValueError, "同时属于"):
            SourceRegistry((one, two))

    def test_jilin_attachment_cdn_is_exact_https_and_context_restricted(self) -> None:
        registration = DEFAULT_SOURCE_REGISTRY.registration_for_url(
            JILIN_ATTACHMENT_CDN_URL,
            enforce_transport=True,
        )
        self.assertIsNotNone(registration)
        assert registration is not None
        self.assertEqual("jilin_government_attachment_cdn", registration.key)
        self.assertIs(
            SourceRole.OFFICIAL,
            DEFAULT_SOURCE_REGISTRY.role_for_url(
                JILIN_ATTACHMENT_CDN_URL,
                enforce_transport=True,
            ),
        )
        self.assertFalse(registration.official_notice_eligible)
        self.assertIsNone(
            DEFAULT_SOURCE_REGISTRY.official_notice_registration(
                JILIN_ATTACHMENT_CDN_URL
            )
        )
        with self.assertRaisesRegex(UrlPolicyViolation, "仅允许作为.*附件来源"):
            trusted_configured_official_host(JILIN_ATTACHMENT_CDN_URL)

        contextual = DEFAULT_SOURCE_REGISTRY.official_attachment_registration(
            JILIN_ATTACHMENT_CDN_URL,
            "https://www.jl.gov.cn/ggzy/notice/123.html",
        )
        self.assertIs(registration, contextual)
        for wrong_parent in (
            "https://www.ccgp.gov.cn/cggg/notice.html",
            LEAD_URL,
            "http://www.jl.gov.cn/ggzy/notice/123.html",
        ):
            with self.subTest(parent=wrong_parent):
                self.assertIsNone(
                    DEFAULT_SOURCE_REGISTRY.official_attachment_registration(
                        JILIN_ATTACHMENT_CDN_URL,
                        wrong_parent,
                    )
                )

    def test_jilin_attachment_registration_does_not_trust_aliyun_family_or_http(self) -> None:
        rejected_urls = (
            "https://other-bucket.oss-cn-north-2-gov-1.aliyuncs.com/file.pdf",
            "https://zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com.evil.test/file.pdf",
            "https://sub.zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com/file.pdf",
            "https://aliyuncs.com/file.pdf",
        )
        for url in rejected_urls:
            with self.subTest(url=url):
                self.assertIs(
                    SourceRole.UNKNOWN,
                    DEFAULT_SOURCE_REGISTRY.role_for_url(url),
                )
                self.assertIsNone(
                    DEFAULT_SOURCE_REGISTRY.official_attachment_registration(
                        url,
                        "https://www.jl.gov.cn/ggzy/notice/123.html",
                    )
                )

        insecure = JILIN_ATTACHMENT_CDN_URL.replace("https://", "http://", 1)
        with self.assertRaisesRegex(UrlPolicyViolation, "HTTPS"):
            DEFAULT_SOURCE_REGISTRY.role_for_url(
                insecure,
                enforce_transport=True,
            )
        self.assertIsNone(
            DEFAULT_SOURCE_REGISTRY.official_attachment_registration(
                insecure,
                "https://www.jl.gov.cn/ggzy/notice/123.html",
            )
        )
        with self.assertRaisesRegex(UrlPolicyViolation, "HTTPS"):
            canonicalize_official_url(insecure)


class ProvenanceExtractionTests(unittest.TestCase):
    def test_extracts_both_ccgp_intention_links_and_retains_lead(self) -> None:
        document = f"""
        <html><body>
          <a href="{PROJECT_URL}">官方项目详情</a>
          <a href="{GROUP_URL}&amp;from=okcis">官方整批采购意向</a>
        </body></html>
        """
        candidates = extract_official_candidates(document, LEAD_URL)

        self.assertEqual(2, len(candidates))
        self.assertEqual(
            {
                OfficialCandidateKind.CCGP_INTENTION_PROJECT,
                OfficialCandidateKind.CCGP_INTENTION_GROUP,
            },
            {candidate.kind for candidate in candidates},
        )
        self.assertTrue(all(candidate.lead_url == LEAD_URL for candidate in candidates))
        self.assertTrue(all(candidate.source_key == "ccgp" for candidate in candidates))
        self.assertTrue(all(candidate.registry_verified for candidate in candidates))
        self.assertTrue(all(candidate.fetch_verification_required for candidate in candidates))
        self.assertTrue(all(validate_official_candidate(item) for item in candidates))

    def test_extracts_required_official_domain_families(self) -> None:
        document = """
        <a href="https://www.jl.gov.cn/ggzy/notice/123.html">吉林公共资源公告</a>
        <a href="https://www.ccgp-jilin.gov.cn/notice/456.html">吉林政府采购公告</a>
        <a href="https://jzcg.pbc.gov.cn/freecms/article/789.html">人民银行采购公告</a>
        <a href="https://www.ggzy.gov.cn/information/html/a/220000/0101/1.html">全国平台公告</a>
        """
        candidates = extract_official_candidates(document, LEAD_URL)
        self.assertEqual(
            {"jilin_government", "ccgp_jilin", "pbc", "national_ggzy"},
            {candidate.source_key for candidate in candidates},
        )
        self.assertTrue(all(
            candidate.kind is OfficialCandidateKind.OFFICIAL_NOTICE
            for candidate in candidates
        ))

    def test_rejects_http_official_links_and_domain_spoofing(self) -> None:
        document = """
        <a href="http://cgyx.ccgp.gov.cn/cgyx/pub/details?groupId=abcdefgh">明文</a>
        <a href="https://ccgp.gov.cn.evil.test/notice">后缀伪装</a>
        <a href="https://fakeccgp.gov.cn/notice">前缀伪装</a>
        <a href="https://ccgp.gov.cn@evil.test/notice">用户信息伪装</a>
        <a href="https://attacker@ccgp.gov.cn/notice">官方主机上的用户信息</a>
        """
        self.assertEqual((), extract_official_candidates(document, LEAD_URL))

    def test_explicit_transport_upgrade_retains_a_validated_http_fallback(self) -> None:
        original = (
            "http://cgyx.ccgp.gov.cn/cgyx/pub/details?"
            "groupId=c477f86f-22b3-422a-b711-ddc7bce5cbd1"
        )
        candidate, = extract_official_candidates(
            f'<a href="{original}">官方采购意向</a>',
            LEAD_URL,
            upgrade_registered_http=True,
        )
        self.assertTrue(candidate.transport_upgraded)
        self.assertEqual(original, candidate.original_url)
        self.assertTrue(candidate.url.startswith("https://"))
        self.assertTrue(validate_official_candidate(candidate))
        self.assertFalse(validate_official_candidate(replace(
            candidate,
            original_url="http://ccgp.gov.cn.evil.test/notice",
        )))

    def test_filters_aggregator_word_and_pdf_exports(self) -> None:
        document = """
        <a href="/exportWord?id=123">导出 Word</a>
        <a href="/api/export?id=123&amp;format=pdf">生成 PDF</a>
        <a href="https://www.okcis.cn/files/generated.docx">导出Word</a>
        <a href="https://www.okcis.cn/files/generated.pdf">导出PDF</a>
        <a href="https://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details?projId=abcdefgh">
          导出 Word
        </a>
        """
        self.assertTrue(is_aggregator_export_link(
            "/exportWord?id=123", "导出 Word", LEAD_URL
        ))
        self.assertTrue(is_aggregator_export_link(
            "/api/export?id=123&format=pdf", "生成 PDF", LEAD_URL
        ))
        self.assertEqual((), extract_official_candidates(document, LEAD_URL))

    def test_keeps_a_real_official_attachment(self) -> None:
        document = (
            '<a href="https://www.ccgp.gov.cn/files/tender-original.pdf">'
            "官方招标文件 PDF 下载</a>"
        )
        candidate, = extract_official_candidates(document, LEAD_URL)
        self.assertEqual(OfficialCandidateKind.OFFICIAL_ATTACHMENT, candidate.kind)
        self.assertEqual("官方招标文件 PDF 下载", candidate.link_text)

    def test_unwraps_only_registered_redirect_target(self) -> None:
        wrapped = (
            "https://www.okcis.cn/jump?url="
            "https%3A%2F%2Fcgyx.ccgp.gov.cn%2Fcgyx%2Fpub%2FproJ%2Fdetails%3F"
            "projId%3Dfe4ba317-1bfd-46d6-8d56-fa8e2f0232bc"
        )
        document = (
            f'<a href="{wrapped}">查看官方来源</a>'
            '<a href="https://www.okcis.cn/jump?url=https%3A%2F%2Fevil.test%2Fx">坏跳转</a>'
        )
        candidate, = extract_official_candidates(document, LEAD_URL)
        self.assertEqual(PROJECT_URL, candidate.url)
        self.assertEqual(
            CandidateDiscoveryMethod.REDIRECT_PARAMETER,
            candidate.discovery_method,
        )

    def test_plain_text_urls_are_found_and_duplicates_are_removed(self) -> None:
        document = (
            f'<a href="{PROJECT_URL}#top">项目</a>'
            f"<p>官方地址：{PROJECT_URL}</p>"
        )
        candidates = extract_official_candidates(document, LEAD_URL)
        self.assertEqual(1, len(candidates))
        self.assertEqual(PROJECT_URL, candidates[0].url)

    def test_ccgp_intention_shell_or_missing_identifier_is_not_a_candidate(self) -> None:
        document = """
        <a href="https://cgyx.ccgp.gov.cn/cgyx/pub/pubSearch">搜索页</a>
        <a href="https://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details">缺项目ID</a>
        <a href="https://cgyx.ccgp.gov.cn/cgyx/pub/details?groupId=bad">坏批次ID</a>
        """
        self.assertEqual((), extract_official_candidates(document, LEAD_URL))

    def test_candidate_validation_detects_tampering(self) -> None:
        candidate, = extract_official_candidates(
            f'<a href="{PROJECT_URL}">官方项目</a>',
            LEAD_URL,
        )
        self.assertFalse(validate_official_candidate(replace(
            candidate,
            source_key="okcis",
        )))
        self.assertFalse(validate_official_candidate(replace(
            candidate,
            url="https://ccgp.gov.cn.evil.test/notice",
        )))
        self.assertFalse(validate_official_candidate(replace(
            candidate,
            fetch_verification_required=False,
        )))

    def test_same_host_links_can_be_excluded_or_explicitly_enabled(self) -> None:
        document = '<a href="https://www.ccgp.gov.cn/cggg/dfgg/zbgg/1.htm">公告</a>'
        lead = "https://www.ccgp.gov.cn/cggg/dfgg/"
        self.assertEqual((), extract_official_candidates(document, lead))
        candidate, = extract_official_candidates(
            document,
            lead,
            cross_domain_only=False,
        )
        self.assertEqual("ccgp", candidate.source_key)

    def test_http_lead_requires_an_explicit_opt_out(self) -> None:
        document = f'<a href="{PROJECT_URL}">官方项目</a>'
        with self.assertRaisesRegex(UrlPolicyViolation, "线索 URL 必须使用 HTTPS"):
            extract_official_candidates(document, "http://legacy.example.com/lead")
        candidate, = extract_official_candidates(
            document,
            "http://legacy.example.com/lead",
            require_https_lead=False,
        )
        self.assertTrue(validate_official_candidate(candidate))


class OfficialGroupDetailRefinementTests(unittest.TestCase):
    def test_selects_project_link_from_the_matching_ccgp_group_row(self) -> None:
        document = """
        <table><tbody>
          <tr>
            <td>中国人民银行吉林省分行</td>
            <td><a href="/cgyx/pub/proJ/details?projId=8989014a-f450-4c64-8e88-affa9247b111">网络维保服务项目</a></td>
            <td><a href="/cgyx/pub/proJ/details?projId=8989014a-f450-4c64-8e88-affa9247b111">详见项目详情</a></td>
          </tr>
          <tr>
            <td>中国人民银行吉林省分行</td>
            <td><a href="/cgyx/pub/proJ/details?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc">网络安全设备维保服务项目</a></td>
            <td>14.000000</td>
            <td><a href="/cgyx/pub/proJ/details?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc">详见项目详情</a></td>
          </tr>
        </tbody></table>
        """
        candidate = extract_matching_official_detail_candidate(
            document,
            GROUP_URL,
            "中国人民银行吉林省分行2026年8至12月政府采购意向-网络安全设备维保服务项目",
            "中国人民银行吉林省分行",
        )

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual(PROJECT_URL, candidate.url)
        self.assertEqual(
            OfficialCandidateKind.CCGP_INTENTION_PROJECT,
            candidate.kind,
        )
        self.assertTrue(validate_official_candidate(candidate))

    def test_project_code_may_match_the_detail_href(self) -> None:
        document = """
        <ul><li>
          吉林省某单位 网络安全服务项目
          <a href="https://www.ccgp.gov.cn/cggg/dfgg/gkzb/notice.htm?projectCode=JL-SEC-001">
            网络安全服务项目
          </a>
        </li></ul>
        """
        candidate = extract_matching_official_detail_candidate(
            document,
            "https://www.ccgp.gov.cn/cggg/dfgg/",
            "网络安全服务项目",
            "吉林省某单位",
            "JL-SEC-001",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual("ccgp", candidate.source_key)
        self.assertEqual(OfficialCandidateKind.OFFICIAL_NOTICE, candidate.kind)

        self.assertIsNone(extract_matching_official_detail_candidate(
            document,
            "https://www.ccgp.gov.cn/cggg/dfgg/",
            "网络安全服务项目",
            "吉林省某单位",
            "JL-SEC-002",
        ))

    def test_accepts_a_registered_official_cross_host_detail(self) -> None:
        document = """
        <li>中国人民银行吉林省分行 网络安全服务项目
          <a href="https://jzcg.pbc.gov.cn/freecms/article/secure-1.html">网络安全服务项目</a>
        </li>
        """
        candidate = extract_matching_official_detail_candidate(
            document,
            "https://www.ccgp.gov.cn/cggg/",
            "网络安全服务项目",
            "中国人民银行吉林省分行",
        )
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual("pbc", candidate.source_key)
        self.assertEqual("https", candidate.url.split(":", 1)[0])

    def test_fails_closed_when_matching_rows_resolve_to_different_projects(self) -> None:
        document = """
        <table>
          <tr><td>某采购人</td><td>网络安全服务项目</td>
            <td><a href="/cgyx/pub/proJ/details?projId=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa">详情</a></td>
          </tr>
          <tr><td>某采购人</td><td>网络安全服务项目</td>
            <td><a href="/cgyx/pub/proJ/details?projId=bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb">详情</a></td>
          </tr>
        </table>
        """
        self.assertIsNone(extract_matching_official_detail_candidate(
            document,
            GROUP_URL,
            "网络安全服务项目",
            "某采购人",
        ))

    def test_rejects_http_commercial_mismatched_and_incomplete_rows(self) -> None:
        unsafe_document = """
        <tr><td>中国人民银行吉林省分行 网络安全设备维保服务项目</td>
          <td><a href="http://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc">项目详情</a></td>
          <td><a href="https://www.okcis.cn/project/1">公告原文</a></td>
        </tr>
        """
        self.assertIsNone(extract_matching_official_detail_candidate(
            unsafe_document,
            GROUP_URL,
            "网络安全设备维保服务项目",
            "中国人民银行吉林省分行",
        ))
        self.assertIsNone(extract_matching_official_detail_candidate(
            unsafe_document,
            GROUP_URL.replace("https://", "http://"),
            "网络安全设备维保服务项目",
            "中国人民银行吉林省分行",
        ))
        self.assertIsNone(extract_matching_official_detail_candidate(
            f'<tr><td>其他项目</td><td><a href="{PROJECT_URL}">详情</a></td></tr>',
            GROUP_URL,
            "网络安全设备维保服务项目",
            "中国人民银行吉林省分行",
        ))
        self.assertIsNone(extract_matching_official_detail_candidate(
            f'<tr><td>中国人民银行吉林省分行 网络安全设备维保服务项目</td><td><a href="{PROJECT_URL}">详情',
            GROUP_URL,
            "网络安全设备维保服务项目",
            "中国人民银行吉林省分行",
        ))


if __name__ == "__main__":
    unittest.main()
