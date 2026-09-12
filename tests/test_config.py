from __future__ import annotations

import copy
import unittest

from tender_downloader.config import DEFAULT_USER_AGENT, effective_user_agent, validate_config_data


def valid_config() -> dict:
    return {
        "start_date": "2026-01-01",
        "end_date": "2026-08-20",
        "output_dir": "output",
        "database": "output/state.sqlite3",
        "http": {
            "user_agent": "TenderTest/1.0 (+mailto:test@example.com)",
            "timeout_seconds": 35,
            "delay_seconds": 0,
            "max_retries": 2,
            "max_response_mb": 25,
            "max_download_mb": 100,
            "download_timeout_seconds": 900,
            "allow_private_hosts": False,
        },
        "sources": [
            {
                "type": "jilin_ggzy",
                "enabled": True,
                "authority_rank": 100,
                "page_size": 100,
            }
        ],
        "ai": {
            "enabled": True,
            "endpoint": "https://api.example.com/v1/chat/completions",
            "model": "test-model",
            "api_key_env": "TENDER_AI_API_KEY",
            "auto_accept_threshold": 0.85,
            "second_review_threshold": 0.60,
            "reclassify": False,
            "timeout_seconds": 60,
        },
        "recall": {"mode": "p0_complete"},
        "delivery": {
            "include_notice_html_when_no_attachment": True,
            "copy_uncertain_to_review": True,
        },
    }


class ConfigValidationTests(unittest.TestCase):
    def test_ready_config_is_valid(self) -> None:
        validate_config_data(valid_config(), require_ready=True)

    def test_user_agent_is_internal_and_needs_no_contact_information(self) -> None:
        data = valid_config()
        del data["http"]["user_agent"]
        validate_config_data(data, require_ready=True)
        self.assertEqual(DEFAULT_USER_AGENT, effective_user_agent(data["http"]))
        self.assertEqual(
            DEFAULT_USER_AGENT,
            effective_user_agent(
                {"user_agent": "JilinTenderDownloader/0.1 (+mailto:replace-with@example.com)"}
            ),
        )

    def test_string_boolean_is_rejected(self) -> None:
        for key in ("allow_private_hosts", "allow_benchmark_proxy_hosts"):
            with self.subTest(key=key):
                data = valid_config()
                data["http"][key] = "false"
                with self.assertRaisesRegex(ValueError, key):
                    validate_config_data(data)

    def test_nonfinite_number_is_rejected(self) -> None:
        data = valid_config()
        data["http"]["delay_seconds"] = float("nan")
        with self.assertRaisesRegex(ValueError, "delay_seconds"):
            validate_config_data(data)

    def test_review_threshold_cannot_exceed_accept_threshold(self) -> None:
        data = valid_config()
        data["ai"]["second_review_threshold"] = 0.9
        with self.assertRaisesRegex(ValueError, "second_review_threshold"):
            validate_config_data(data)

    def test_reserved_environment_variable_is_rejected(self) -> None:
        data = valid_config()
        data["ai"]["api_key_env"] = "PATH"
        with self.assertRaisesRegex(ValueError, "api_key_env"):
            validate_config_data(data)

    def test_enabled_ai_rejects_public_plain_http_endpoint(self) -> None:
        data = valid_config()
        data["ai"]["endpoint"] = "http://api.example.com/v1/chat/completions"
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_config_data(data, require_ready=True)

    def test_enabled_ai_allows_explicit_loopback_http_model(self) -> None:
        data = valid_config()
        data["http"]["allow_private_hosts"] = True
        data["ai"]["endpoint"] = "http://127.0.0.1:11434/v1/chat/completions"
        validate_config_data(data, require_ready=True)

    def test_custom_web_form_login_config_is_valid_without_credentials(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "hospital_portal",
                "name": "医院采购门户",
                "enabled": True,
                "authority_rank": 70,
                "start_urls": ["https://tender.example.com/notices"],
                "auth": {
                    "mode": "form",
                    "login_url": "https://tender.example.com/login",
                    "username_field": "username",
                    "password_field": "password",
                    "extra_fields": {"action": "login"},
                },
            }
        )
        validate_config_data(data, require_ready=True)

    def test_custom_web_browser_login_config_is_valid(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "browser_portal",
                "name": "浏览器登录采购门户",
                "enabled": True,
                "authority_rank": 70,
                "start_urls": [
                    "https://tender.example.com/notices",
                    "https://tender.example.com/awards",
                ],
                "auth": {
                    "mode": "browser",
                    "login_url": "https://tender.example.com/sign-in",
                },
            }
        )
        validate_config_data(data, require_ready=True)

    def test_browser_profile_id_and_same_origin_check_url_are_valid(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "browser_portal",
                "name": "可恢复登录门户",
                "enabled": True,
                "start_urls": ["https://tender.example.com/notices"],
                "auth": {
                    "mode": "browser",
                    "login_url": "https://tender.example.com/sign-in",
                    "check_url": "https://tender.example.com/account/notices",
                    "profile_id": "stable_profile_01",
                },
            }
        )
        validate_config_data(data, require_ready=True)

    def test_browser_profile_id_rejects_invalid_or_duplicate_values(self) -> None:
        for bad_value in ("../escape", "ab", "含中文"):
            with self.subTest(profile_id=bad_value):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "browser_portal",
                        "name": "错误登录标识",
                        "enabled": True,
                        "start_urls": ["https://tender.example.com/notices"],
                        "auth": {
                            "mode": "browser",
                            "profile_id": bad_value,
                        },
                    }
                )
                with self.assertRaisesRegex(ValueError, "配置标识无效"):
                    validate_config_data(data)

        duplicate = valid_config()
        for suffix, host in (("one", "one.example.com"), ("two", "two.example.com")):
            duplicate["sources"].append(
                {
                    "type": "custom_web",
                    "id": f"portal_{suffix}",
                    "name": f"重复标识 {suffix}",
                    "enabled": True,
                    "start_urls": [f"https://{host}/notices"],
                    "auth": {
                        "mode": "browser",
                        "profile_id": "same_profile_id",
                    },
                }
            )
        with self.assertRaisesRegex(ValueError, "不能重复"):
            validate_config_data(duplicate)

    def test_browser_check_url_rejects_cross_origin_and_secret_query(self) -> None:
        for check_url, message in (
            ("https://other.example.com/account", "同一来源"),
            ("https://tender.example.com/account?token=secret", "密钥参数"),
        ):
            with self.subTest(check_url=check_url):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "browser_portal",
                        "name": "错误检查地址",
                        "enabled": True,
                        "start_urls": ["https://tender.example.com/notices"],
                        "auth": {
                            "mode": "browser",
                            "login_url": "https://tender.example.com/login",
                            "check_url": check_url,
                        },
                    }
                )
                with self.assertRaisesRegex(ValueError, message):
                    validate_config_data(data)

    def test_custom_official_source_accepts_only_registered_or_institution_hosts(self) -> None:
        for url in (
            "https://www.ccgp.gov.cn/cggg/",
            "https://procurement.example.gov.cn/notices",
            "https://bidding.example.edu.cn/notices",
        ):
            with self.subTest(url=url):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "official_portal",
                        "name": "官方采购门户",
                        "enabled": True,
                        "source_role": "official",
                        "start_urls": [url],
                        "auth": {"mode": "none"},
                    }
                )
                validate_config_data(data, require_ready=True)

    def test_custom_official_source_rejects_arbitrary_commercial_and_http_hosts(self) -> None:
        cases = (
            "https://tender.example.com/notices",
            "https://www.okcis.cn/notices",
            "http://procurement.example.gov.cn/notices",
            "https://procurement.example.gov.cn:8443/notices",
        )
        for url in cases:
            with self.subTest(url=url):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "untrusted_official",
                        "name": "错误标记的来源",
                        "enabled": True,
                        "source_role": "official",
                        "start_urls": [url],
                        "auth": {"mode": "none"},
                    }
                )
                with self.assertRaisesRegex(ValueError, "不能标记为官方来源"):
                    validate_config_data(data, require_ready=True)

        correctly_labeled = valid_config()
        correctly_labeled["sources"].append(
            {
                "type": "custom_web",
                "id": "commercial_lead",
                "name": "商业线索站",
                "enabled": True,
                "source_role": "commercial_lead",
                "start_urls": ["https://www.okcis.cn/notices"],
                "auth": {"mode": "none"},
            }
        )
        validate_config_data(correctly_labeled, require_ready=True)

    def test_custom_web_required_terms_accepts_empty_or_string_list(self) -> None:
        for required_terms in ([], ["吉林", "Jilin"]):
            with self.subTest(required_terms=required_terms):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "regional_portal",
                        "name": "地区采购门户",
                        "enabled": True,
                        "start_urls": ["https://tender.example.com/notices"],
                        "required_terms": required_terms,
                        "auth": {"mode": "none"},
                    }
                )
                validate_config_data(data, require_ready=True)

    def test_custom_web_required_terms_rejects_non_string_lists(self) -> None:
        for required_terms in ("吉林", ["吉林", 1], ["吉林", ""]):
            with self.subTest(required_terms=required_terms):
                data = valid_config()
                data["sources"].append(
                    {
                        "type": "custom_web",
                        "id": "regional_portal",
                        "name": "地区采购门户",
                        "enabled": True,
                        "start_urls": ["https://tender.example.com/notices"],
                        "required_terms": required_terms,
                        "auth": {"mode": "none"},
                    }
                )
                with self.assertRaisesRegex(ValueError, "required_terms"):
                    validate_config_data(data)

    def test_custom_web_browser_login_url_must_use_exact_origin(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "browser_wrong_port",
                "name": "端口不一致的浏览器登录",
                "enabled": True,
                "start_urls": ["https://tender.example.com/notices"],
                "auth": {
                    "mode": "browser",
                    "login_url": "https://tender.example.com:8443/sign-in",
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "同一来源"):
            validate_config_data(data)

    def test_custom_web_browser_login_requires_single_https_origin(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "browser_plain_http",
                "name": "不安全的浏览器登录",
                "enabled": True,
                "start_urls": ["http://tender.example.com/notices"],
                "auth": {"mode": "browser"},
            }
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_config_data(data)

    def test_custom_web_rejects_cross_host_form_login(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "unsafe_login",
                "name": "跨域登录",
                "enabled": True,
                "authority_rank": 50,
                "start_urls": ["https://tender.example.com/notices"],
                "auth": {
                    "mode": "form",
                    "login_url": "https://other.example.net/login",
                    "username_field": "username",
                    "password_field": "password",
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "同一来源"):
            validate_config_data(data)

    def test_custom_web_rejects_login_origin_downgrade(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "secure_portal",
                "name": "安全采购门户",
                "enabled": True,
                "start_urls": ["https://procurement.example.com/notices"],
                "auth": {
                    "mode": "form",
                    "login_url": "http://procurement.example.com/login",
                    "username_field": "username",
                    "password_field": "password",
                },
            }
        )
        with self.assertRaisesRegex(ValueError, "同一来源"):
            validate_config_data(data)

    def test_custom_web_rejects_basic_auth_across_multiple_origins(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "multi_origin",
                "name": "多站点登录",
                "enabled": True,
                "start_urls": [
                    "https://one.example.com/notices",
                    "https://two.example.com/notices",
                ],
                "auth": {"mode": "basic"},
            }
        )
        with self.assertRaisesRegex(ValueError, "只能配置一个"):
            validate_config_data(data)

    def test_custom_web_rejects_basic_auth_over_plain_http(self) -> None:
        data = valid_config()
        data["sources"].append(
            {
                "type": "custom_web",
                "id": "plain_http",
                "name": "明文登录站点",
                "enabled": True,
                "start_urls": ["http://procurement.example.com/notices"],
                "auth": {"mode": "basic"},
            }
        )
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            validate_config_data(data)

    def test_invalid_save_data_does_not_require_mutation(self) -> None:
        original = valid_config()
        data = copy.deepcopy(original)
        data["start_date"] = "2026-12-01"
        with self.assertRaisesRegex(ValueError, "start_date"):
            validate_config_data(data)
        self.assertEqual("2026-01-01", original["start_date"])


if __name__ == "__main__":
    unittest.main()
