from __future__ import annotations

import unittest
import json
import os

from tender_downloader.classify import LLMClassifier, RuleClassifier, has_strong_evidence, is_candidate
from tender_downloader.http_client import HttpResult
from tender_downloader.models import Notice


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.notice = Notice(
            source="fixture",
            authority_rank=100,
            external_id="1",
            title="吉林某大学三级等保测评项目",
            published_at="2026-01-01",
            url="https://example.gov.cn/1",
            buyer="吉林某大学",
        )

    def test_high_recall_terms(self) -> None:
        self.assertTrue(is_candidate("信息化平台建设及安全整改"))
        self.assertTrue(has_strong_evidence("三级等保测评和渗透测试"))

    def test_rules_never_claim_ai_confirmation(self) -> None:
        result = RuleClassifier().classify(self.notice, "采购三级等保测评和漏洞扫描服务")
        self.assertTrue(result.relevant)
        self.assertFalse(result.ai_confirmed)
        self.assertTrue(result.needs_review)
        self.assertEqual("教育", result.industry)

    def test_llm_rejects_string_instead_of_evidence_array(self) -> None:
        class Client:
            def post_json(self, *args, **kwargs):
                payload = {
                    "choices": [{"message": {"content": json.dumps({
                        "relevant": True,
                        "confidence": 0.99,
                        "industry": "教育",
                        "security_categories": ["测评咨询"],
                        "evidence": "网络安全",
                        "reason": "fixture",
                    }, ensure_ascii=False)}}],
                }
                return HttpResult("u", 200, {}, json.dumps(payload, ensure_ascii=False).encode())

        os.environ["FIXTURE_AI_KEY"] = "x"
        try:
            classifier = LLMClassifier(Client(), {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
                "api_key_env": "FIXTURE_AI_KEY",
            })
            result = classifier.classify(self.notice, "采购网络安全等保测评服务")
        finally:
            os.environ.pop("FIXTURE_AI_KEY", None)
        self.assertFalse(result.ai_confirmed)
        self.assertTrue(result.needs_review)

    def test_llm_confirmation_follows_relevance_tristate(self) -> None:
        class Client:
            output: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                body = {
                    "choices": [{
                        "message": {
                            "content": json.dumps(self.output, ensure_ascii=False),
                        },
                    }],
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        client = Client()
        classifier = LLMClassifier(
            client,
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        )
        cases = (
            (True, ["等保测评"], True, False),
            (True, ["采购"], False, True),
            (True, [], False, True),
            (False, [], True, False),
            (None, ["等保测评"], False, True),
        )
        for relevant, evidence, ai_confirmed, needs_review in cases:
            with self.subTest(relevant=relevant, evidence=evidence):
                client.output = {
                    "relevant": relevant,
                    "confidence": 0.95,
                    "industry": "教育",
                    "security_categories": ["测评咨询"],
                    "evidence": evidence,
                    "reason": "fixture",
                }
                result = classifier.classify(
                    self.notice,
                    "采购网络安全等保测评服务",
                )

                self.assertIs(relevant, result.relevant)
                self.assertIs(ai_confirmed, result.ai_confirmed)
                self.assertIs(needs_review, result.needs_review)
                if relevant is None:
                    self.assertEqual(0.5, result.confidence)

    def test_positive_prompt_requires_verbatim_strong_evidence(self) -> None:
        test_case = self

        class Client:
            payload: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                del headers
                self.payload = payload
                content = json.dumps(
                    test_case._valid_model_output(), ensure_ascii=False
                )
                body = {"choices": [{"message": {"content": content}}]}
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        client = Client()
        classifier = LLMClassifier(
            client,
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        )

        classifier.classify(self.notice, "采购网络安全等保测评服务")

        system = client.payload["messages"][0]["content"]
        user = client.payload["messages"][1]["content"]
        self.assertIn("逐字复制", system)
        self.assertIn("不得概括", system)
        self.assertIn("JSON 布尔值", system)
        self.assertIn("禁止输出字符串", system)
        self.assertIn("采购意向", system)
        self.assertIn("维保项目", system)
        self.assertIn("confidence 必须小于等于 0.5", system)
        self.assertIn("网络安全", system)
        self.assertIn("等保测评", system)
        self.assertIn("严禁字符串", user)
        self.assertIn("relevant=null时不得超过0.5", user)
        self.assertIn("正例至少一条须含明确网络安全强词", user)

    def test_deterministic_industry_overrides_conflicting_model_value(self) -> None:
        class Client:
            output: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                del payload, headers
                body = {
                    "choices": [{
                        "message": {
                            "content": json.dumps(self.output, ensure_ascii=False),
                        },
                    }],
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        bank_notice = Notice(
            source="fixture",
            authority_rank=100,
            external_id="bank-1",
            title="中国人民银行吉林省分行网络安全设备维保项目",
            published_at="2026-08-12",
            url="https://example.gov.cn/bank-1",
            buyer="中国人民银行吉林省分行",
        )
        client = Client()
        classifier = LLMClassifier(
            client,
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        )
        for model_industry, expected in (
            ("其他行业", "金融"),
            ("模型自造行业", "金融"),
            ("教育", "金融"),
        ):
            with self.subTest(model_industry=model_industry):
                client.output = {
                    "relevant": True,
                    "confidence": 0.96,
                    "industry": model_industry,
                    "security_categories": ["安全设备"],
                    "evidence": ["网络安全设备维保"],
                    "reason": "原文含网络安全设备维保",
                }
                result = classifier.classify(
                    bank_notice,
                    "采购维保服务；页面导航：学校、学院、教育局采购信息",
                )

                self.assertEqual(expected, result.industry)
                self.assertTrue(result.ai_confirmed)

    def test_valid_model_industry_is_kept_when_rules_have_no_specific_match(self) -> None:
        class Client:
            def post_json(self, endpoint, payload, headers=None):
                del payload, headers
                output = {
                    "relevant": True,
                    "confidence": 0.96,
                    "industry": "制造业",
                    "security_categories": ["安全设备"],
                    "evidence": ["网络安全"],
                    "reason": "原文含网络安全",
                }
                body = {
                    "choices": [{
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                    }],
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        notice = Notice(
            source="fixture",
            authority_rank=100,
            external_id="company-1",
            title="某公司网络安全采购项目",
            published_at="2026-08-12",
            url="https://example.gov.cn/company-1",
            buyer="某公司",
        )
        result = LLMClassifier(
            Client(),
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        ).classify(notice, "采购网络安全服务")

        self.assertEqual("制造业", result.industry)

    def test_null_result_fields_are_consistently_postprocessed(self) -> None:
        class Client:
            def post_json(self, endpoint, payload, headers=None):
                del payload, headers
                output = {
                    "relevant": None,
                    "confidence": 1.0,
                    "industry": "教育",
                    "security_categories": ["其他网络安全"],
                    "evidence": ["网络安全设备维保服务项目"],
                    "reason": "fixture",
                }
                body = {
                    "choices": [{
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                    }],
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        notice = Notice(
            source="fixture",
            authority_rank=100,
            external_id="bank-null-1",
            title="中国人民银行吉林省分行网络安全设备维保服务项目",
            published_at="2026-08-12",
            url="https://example.gov.cn/bank-null-1",
            buyer="中国人民银行吉林省分行",
        )
        result = LLMClassifier(
            Client(),
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        ).classify(notice, "网络安全设备维保服务项目采购意向")

        self.assertIsNone(result.relevant)
        self.assertEqual(0.5, result.confidence)
        self.assertFalse(result.ai_confirmed)
        self.assertTrue(result.needs_review)
        self.assertEqual("金融", result.industry)
        self.assertEqual(("安全运营与运维",), result.security_categories)

    def test_string_relevant_is_not_accepted_as_json_boolean(self) -> None:
        class Client:
            def post_json(self, endpoint, payload, headers=None):
                del payload, headers
                output = {
                    "relevant": "true",
                    "confidence": 0.99,
                    "industry": "教育",
                    "security_categories": ["测评咨询"],
                    "evidence": ["等保测评"],
                    "reason": "fixture",
                }
                body = {
                    "choices": [{
                        "message": {
                            "content": json.dumps(output, ensure_ascii=False),
                        },
                    }],
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        result = LLMClassifier(
            Client(),
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            api_key="fixture-secret",
        ).classify(self.notice, "采购网络安全等保测评服务")

        self.assertIsNone(result.relevant)
        self.assertEqual(0.5, result.confidence)
        self.assertFalse(result.ai_confirmed)

    @staticmethod
    def _valid_model_output() -> dict:
        return {
            "relevant": True,
            "confidence": 0.95,
            "industry": "教育",
            "security_categories": ["测评咨询"],
            "evidence": ["等保测评"],
            "reason": "采购内容直接包含等保测评",
        }

    def test_openai_compatible_remains_the_default_protocol(self) -> None:
        test_case = self

        class Client:
            endpoint = ""
            payload: dict = {}
            headers: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                self.endpoint = endpoint
                self.payload = payload
                self.headers = headers or {}
                content = json.dumps(test_case._valid_model_output(), ensure_ascii=False)
                body = {"choices": [{"message": {"content": content}}]}
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        client = Client()
        os.environ["FIXTURE_AI_KEY"] = "openai-secret"
        try:
            classifier = LLMClassifier(client, {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
                "api_key_env": "FIXTURE_AI_KEY",
            })
            result = classifier.classify(self.notice, "采购网络安全等保测评服务")
        finally:
            os.environ.pop("FIXTURE_AI_KEY", None)

        self.assertTrue(result.ai_confirmed)
        self.assertEqual("Bearer openai-secret", client.headers["Authorization"])
        self.assertEqual({"type": "json_object"}, client.payload["response_format"])
        self.assertEqual("fixture", client.payload["model"])

    def test_anthropic_protocol_builds_messages_request_and_parses_blocks(self) -> None:
        test_case = self

        class Client:
            endpoint = ""
            payload: dict = {}
            headers: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                self.endpoint = endpoint
                self.payload = payload
                self.headers = headers or {}
                content = json.dumps(test_case._valid_model_output(), ensure_ascii=False)
                body = {
                    "content": [
                        {"type": "thinking", "thinking": "not parsed"},
                        {"type": "text", "text": content},
                    ]
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        client = Client()
        os.environ["FIXTURE_CLAUDE_KEY"] = "claude-secret"
        try:
            classifier = LLMClassifier(client, {
                "protocol": "anthropic",
                "endpoint": "https://api.anthropic.com/v1/messages",
                "model": "claude-sonnet-5",
                "api_key_env": "FIXTURE_CLAUDE_KEY",
            })
            result = classifier.classify(self.notice, "采购网络安全等保测评服务")
        finally:
            os.environ.pop("FIXTURE_CLAUDE_KEY", None)

        self.assertTrue(result.ai_confirmed)
        self.assertEqual("claude-secret", client.headers["x-api-key"])
        self.assertEqual("2023-06-01", client.headers["anthropic-version"])
        self.assertEqual("claude-sonnet-5", client.payload["model"])
        self.assertIn("system", client.payload)
        self.assertNotIn("claude-secret", client.endpoint)

    def test_gemini_protocol_keeps_key_in_header_and_parses_candidate(self) -> None:
        test_case = self

        class Client:
            endpoint = ""
            payload: dict = {}
            headers: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                self.endpoint = endpoint
                self.payload = payload
                self.headers = headers or {}
                content = json.dumps(test_case._valid_model_output(), ensure_ascii=False)
                body = {
                    "candidates": [{"content": {"parts": [{"text": content}]}}]
                }
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        client = Client()
        os.environ["FIXTURE_GEMINI_KEY"] = "gemini-secret"
        try:
            classifier = LLMClassifier(client, {
                "provider": "gemini",
                "endpoint": (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "{model}:generateContent"
                ),
                "model": "gemini-3.6-flash",
                "api_key_env": "FIXTURE_GEMINI_KEY",
            })
            result = classifier.classify(self.notice, "采购网络安全等保测评服务")
        finally:
            os.environ.pop("FIXTURE_GEMINI_KEY", None)

        self.assertTrue(result.ai_confirmed)
        self.assertIn("gemini-3.6-flash:generateContent", client.endpoint)
        self.assertNotIn("gemini-secret", client.endpoint)
        self.assertEqual("gemini-secret", client.headers["x-goog-api-key"])
        self.assertEqual(
            "application/json",
            client.payload["generationConfig"]["responseMimeType"],
        )
        self.assertIn("system_instruction", client.payload)

    def test_unknown_protocol_fails_before_request(self) -> None:
        os.environ["FIXTURE_AI_KEY"] = "x"
        try:
            with self.assertRaisesRegex(ValueError, "不支持的 AI API 协议"):
                LLMClassifier(object(), {
                    "protocol": "unknown",
                    "endpoint": "https://ai.example/v1/messages",
                    "model": "fixture",
                    "api_key_env": "FIXTURE_AI_KEY",
                })
        finally:
            os.environ.pop("FIXTURE_AI_KEY", None)

    def test_explicit_key_does_not_mutate_or_require_environment(self) -> None:
        class Client:
            headers: dict = {}

            def post_json(self, endpoint, payload, headers=None):
                self.headers = headers or {}
                content = json.dumps(
                    ClassificationTests._valid_model_output(), ensure_ascii=False
                )
                body = {"choices": [{"message": {"content": content}}]}
                return HttpResult(endpoint, 200, {}, json.dumps(body).encode())

        os.environ.pop("EXPLICIT_ONLY_AI_KEY", None)
        before = dict(os.environ)
        client = Client()
        classifier = LLMClassifier(
            client,
            {
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
                "api_key_env": "EXPLICIT_ONLY_AI_KEY",
            },
            api_key="ephemeral-secret",
        )
        result = classifier.classify(self.notice, "采购网络安全等保测评服务")

        self.assertTrue(result.ai_confirmed)
        self.assertEqual("Bearer ephemeral-secret", client.headers["Authorization"])
        self.assertEqual(before, dict(os.environ))


if __name__ == "__main__":
    unittest.main()
