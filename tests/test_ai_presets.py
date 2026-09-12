from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from tender_downloader.ai_presets import AI_PRESETS


class AiPresetTests(unittest.TestCase):
    def test_presets_are_json_serializable_and_complete(self) -> None:
        encoded = json.dumps(AI_PRESETS, ensure_ascii=False)
        self.assertIn("OpenAI", encoded)

        required = {
            "id", "name", "protocol", "endpoint", "models", "api_key_env",
        }
        expected_ids = {
            "openai", "deepseek", "doubao", "qwen", "zhipu", "moonshot",
            "anthropic", "gemini",
        }
        self.assertEqual(expected_ids, {str(item["id"]) for item in AI_PRESETS})
        for preset in AI_PRESETS:
            self.assertTrue(required.issubset(preset))
            self.assertIn(
                preset["protocol"],
                {"openai_compatible", "anthropic", "gemini"},
            )
            self.assertTrue(preset["models"])
            self.assertTrue(str(preset["endpoint"]).startswith("https://"))
            for model in preset["models"]:
                self.assertEqual({"id", "label"}, set(model))
                self.assertTrue(model["id"])
                self.assertTrue(model["label"])

    def test_no_preset_puts_credentials_in_url(self) -> None:
        for preset in AI_PRESETS:
            endpoint = str(preset["endpoint"])
            query_keys = {key.lower() for key in parse_qs(urlsplit(endpoint).query)}
            self.assertFalse(
                query_keys & {"key", "api_key", "apikey", "token", "access_token"},
                preset["id"],
            )

    def test_current_primary_model_choices_are_present(self) -> None:
        by_id = {str(item["id"]): item for item in AI_PRESETS}
        model_ids = {
            preset_id: [str(model["id"]) for model in preset["models"]]
            for preset_id, preset in by_id.items()
        }
        self.assertEqual(
            ["gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.6-sol"],
            model_ids["openai"],
        )
        self.assertIn("deepseek-v4-flash", model_ids["deepseek"])
        self.assertEqual(
            [
                "doubao-seed-2-0-pro-260215",
                "doubao-seed-2-0-lite-260215",
                "doubao-seed-2-0-mini-260428",
            ],
            model_ids["doubao"],
        )
        self.assertEqual(
            "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
            by_id["doubao"]["endpoint"],
        )
        self.assertEqual("ARK_API_KEY", by_id["doubao"]["api_key_env"])
        self.assertIn("qwen3.7-plus", model_ids["qwen"])
        self.assertIn("glm-5.2", model_ids["zhipu"])
        self.assertIn("kimi-k2.6", model_ids["moonshot"])
        self.assertIn("claude-sonnet-5", model_ids["anthropic"])
        self.assertIn("gemini-3.6-flash", model_ids["gemini"])


if __name__ == "__main__":
    unittest.main()
