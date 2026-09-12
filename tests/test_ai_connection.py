from __future__ import annotations

import unittest

from tender_downloader.ai_connection import test_ai_connection as probe_ai_connection
from tender_downloader.http_client import FetchError, HttpResult


class RecordingClient:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.endpoint = ""
        self.payload: dict = {}
        self.headers: dict[str, str] = {}

    def post_json(self, endpoint, payload, headers=None):
        self.endpoint = endpoint
        self.payload = payload
        self.headers = headers or {}
        if self.error is not None:
            raise self.error
        if "x-api-key" in self.headers:
            body = b'{"content":[{"type":"text","text":"OK"}]}'
        elif "x-goog-api-key" in self.headers:
            body = b'{"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}'
        else:
            body = b'{"choices":[{"message":{"content":"OK"}}]}'
        return HttpResult(endpoint, 200, {}, body)


class AiConnectionTests(unittest.TestCase):
    def test_openai_compatible_probe(self) -> None:
        client = RecordingClient()
        result = probe_ai_connection(
            client,
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-v4-flash",
            },
            "openai-compatible-secret",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("deepseek", result["provider"])
        self.assertEqual("deepseek-v4-flash", result["model"])
        self.assertIsInstance(result["latency_ms"], int)
        self.assertEqual(
            "Bearer openai-compatible-secret", client.headers["Authorization"]
        )
        self.assertEqual("deepseek-v4-flash", client.payload["model"])
        self.assertLessEqual(len(client.payload["messages"]), 1)

    def test_anthropic_probe(self) -> None:
        client = RecordingClient()
        result = probe_ai_connection(
            client,
            {
                "provider": "anthropic",
                "protocol": "anthropic",
                "endpoint": "https://api.anthropic.com/v1/messages",
                "model": "claude-sonnet-5",
            },
            "anthropic-secret",
        )

        self.assertTrue(result["ok"])
        self.assertEqual("anthropic-secret", client.headers["x-api-key"])
        self.assertEqual("2023-06-01", client.headers["anthropic-version"])
        self.assertEqual(8, client.payload["max_tokens"])

    def test_gemini_probe_keeps_key_out_of_url(self) -> None:
        client = RecordingClient()
        result = probe_ai_connection(
            client,
            {
                "provider": "gemini",
                "protocol": "gemini",
                "endpoint": (
                    "https://generativelanguage.googleapis.com/v1beta/models/"
                    "{model}:generateContent"
                ),
                "model": "gemini-3.6-flash",
            },
            "gemini-secret",
        )

        self.assertTrue(result["ok"])
        self.assertIn("gemini-3.6-flash:generateContent", client.endpoint)
        self.assertNotIn("gemini-secret", client.endpoint)
        self.assertEqual("gemini-secret", client.headers["x-goog-api-key"])
        self.assertEqual(8, client.payload["generationConfig"]["maxOutputTokens"])

    def test_failure_summary_redacts_key_and_authorization_values(self) -> None:
        secret = "sk-test-super-secret-value"
        client = RecordingClient(
            FetchError(
                "failed Authorization: Bearer " + secret
                + "?api_key=" + secret
            )
        )
        result = probe_ai_connection(
            client,
            {
                "provider": "custom",
                "protocol": "openai_compatible",
                "endpoint": "https://ai.example/v1/chat/completions",
                "model": "fixture",
            },
            secret,
        )

        self.assertFalse(result["ok"])
        rendered = repr(result)
        self.assertNotIn(secret, rendered)
        self.assertIn("已隐藏", result["error"])

    def test_empty_explicit_key_is_a_safe_failure(self) -> None:
        result = probe_ai_connection(
            RecordingClient(),
            {
                "provider": "deepseek",
                "protocol": "openai_compatible",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-v4-flash",
            },
            "  ",
        )

        self.assertFalse(result["ok"])
        self.assertEqual("API Key 不能为空", result["error"])


if __name__ == "__main__":
    unittest.main()
