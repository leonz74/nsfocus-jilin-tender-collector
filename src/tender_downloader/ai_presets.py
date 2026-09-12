from __future__ import annotations

"""Built-in AI provider choices shown by the local configuration UI.

Endpoints are full HTTP request URLs rather than SDK ``base_url`` values.  The
Gemini endpoint intentionally contains a ``{model}`` placeholder because the
model name is part of that API's path.  Credentials are never embedded in an
endpoint; :class:`tender_downloader.classify.LLMClassifier` sends them in the
provider-specific request header.
"""


# Keep this value made only of JSON-native containers and scalar values.  The
# web UI can therefore return it directly from a JSON API without a custom
# encoder.  Model order is also the suggested order in the model picker.
AI_PRESETS: list[dict[str, object]] = [
    {
        "id": "openai",
        "name": "OpenAI",
        "protocol": "openai_compatible",
        "endpoint": "https://api.openai.com/v1/chat/completions",
        "models": [
            {"id": "gpt-5.6-terra", "label": "GPT-5.6 Terra（均衡）"},
            {"id": "gpt-5.6-luna", "label": "GPT-5.6 Luna（高吞吐/低成本）"},
            {"id": "gpt-5.6-sol", "label": "GPT-5.6 Sol（最高能力）"},
        ],
        "api_key_env": "OPENAI_API_KEY",
        "docs_url": "https://developers.openai.com/api/docs/models",
    },
    {
        "id": "deepseek",
        "name": "DeepSeek",
        "protocol": "openai_compatible",
        "endpoint": "https://api.deepseek.com/chat/completions",
        "models": [
            {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash（快速/低成本）"},
            {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro（高质量）"},
        ],
        "api_key_env": "DEEPSEEK_API_KEY",
        "docs_url": "https://api-docs.deepseek.com/quick_start/pricing-details-cny/",
    },
    {
        "id": "doubao",
        "name": "火山方舟 / 豆包",
        "protocol": "openai_compatible",
        "endpoint": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "models": [
            {"id": "doubao-seed-2-0-pro-260215", "label": "Doubao Seed 2.0 Pro（高质量）"},
            {"id": "doubao-seed-2-0-lite-260215", "label": "Doubao Seed 2.0 Lite（均衡）"},
            {"id": "doubao-seed-2-0-mini-260428", "label": "Doubao Seed 2.0 Mini（快速/低成本）"},
        ],
        "api_key_env": "ARK_API_KEY",
        "docs_url": "https://www.volcengine.com/docs/82379/1494384",
    },
    {
        "id": "qwen",
        "name": "阿里云百炼 / Qwen（中国内地）",
        "protocol": "openai_compatible",
        "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "models": [
            {"id": "qwen3.7-plus", "label": "Qwen3.7 Plus（均衡）"},
            {"id": "qwen3.7-max", "label": "Qwen3.7 Max（高质量）"},
            {"id": "qwen3.6-flash", "label": "Qwen3.6 Flash（快速/低成本）"},
        ],
        "api_key_env": "DASHSCOPE_API_KEY",
        "docs_url": "https://www.alibabacloud.com/help/en/model-studio/qwen-api-via-openai-chat-completions",
    },
    {
        "id": "zhipu",
        "name": "智谱 GLM",
        "protocol": "openai_compatible",
        "endpoint": "https://open.bigmodel.cn/api/paas/v4/chat/completions",
        "models": [
            {"id": "glm-5.2", "label": "GLM-5.2（旗舰）"},
            {"id": "glm-5-turbo", "label": "GLM-5 Turbo（高速）"},
            {"id": "glm-4.7-flash", "label": "GLM-4.7 Flash（低成本）"},
        ],
        "api_key_env": "ZHIPU_API_KEY",
        "docs_url": "https://docs.bigmodel.cn/cn/guide/develop/http/introduction",
    },
    {
        "id": "moonshot",
        "name": "Moonshot / Kimi",
        "protocol": "openai_compatible",
        "endpoint": "https://api.moonshot.cn/v1/chat/completions",
        "models": [
            {"id": "kimi-k2.6", "label": "Kimi K2.6（推荐）"},
            {"id": "kimi-k2.5", "label": "Kimi K2.5"},
            {"id": "kimi-latest", "label": "Kimi Latest（自动更新）"},
        ],
        "api_key_env": "MOONSHOT_API_KEY",
        "docs_url": "https://platform.kimi.com/docs/api/overview",
    },
    {
        "id": "anthropic",
        "name": "Anthropic Claude",
        "protocol": "anthropic",
        "endpoint": "https://api.anthropic.com/v1/messages",
        "models": [
            {"id": "claude-sonnet-5", "label": "Claude Sonnet 5（均衡）"},
            {"id": "claude-opus-5", "label": "Claude Opus 5（高质量）"},
            {"id": "claude-haiku-4-5-20251001", "label": "Claude Haiku 4.5（快速）"},
        ],
        "api_key_env": "ANTHROPIC_API_KEY",
        "docs_url": "https://platform.claude.com/docs/en/api/messages/create",
    },
    {
        "id": "gemini",
        "name": "Google Gemini",
        "protocol": "gemini",
        "endpoint": (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            "{model}:generateContent"
        ),
        "models": [
            {"id": "gemini-3.5-flash-lite", "label": "Gemini 3.5 Flash-Lite（分类推荐）"},
            {"id": "gemini-3.6-flash", "label": "Gemini 3.6 Flash（均衡）"},
            {"id": "gemini-3.1-pro-preview", "label": "Gemini 3.1 Pro Preview（高质量）"},
        ],
        "api_key_env": "GEMINI_API_KEY",
        "docs_url": "https://ai.google.dev/gemini-api/docs/models",
    },
]
