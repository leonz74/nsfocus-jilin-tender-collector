from __future__ import annotations

"""Small, credential-safe AI provider connectivity probes."""

import json
import re
import time
from typing import Any
from urllib.parse import quote

from .classify import LLMClassifier
from .http_client import HttpClient


_AUTH_VALUE_RE = re.compile(
    r"(?i)(authorization|x-api-key|x-goog-api-key|api[-_]?key)"
    r"(\s*[:=]\s*)(?:bearer\s+)?[^\s,;]+"
)
_SECRET_QUERY_RE = re.compile(
    r"(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&#\s]+"
)


def _safe_error(exc: Exception, api_key: str) -> str:
    """Return a short diagnostic without reflecting credentials to the UI."""
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    if api_key:
        message = message.replace(api_key, "[已隐藏]")
        message = message.replace(quote(api_key, safe=""), "[已隐藏]")
    message = _AUTH_VALUE_RE.sub(r"\1\2[已隐藏]", message)
    message = _SECRET_QUERY_RE.sub(r"\1[已隐藏]", message)
    message = re.sub(r"\s+", " ", message)[:300]
    return message or "连接失败"


def _assert_json_response(result: object, *, protocol: str) -> None:
    status = int(getattr(result, "status", 0))
    if not 200 <= status < 300:
        raise ValueError(f"AI 接口返回 HTTP {status}")
    body = getattr(result, "body", b"")
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    parsed = json.loads(str(body))
    if not isinstance(parsed, dict):
        raise ValueError("AI 接口响应不是 JSON 对象")
    if parsed.get("error"):
        raise ValueError("AI 接口返回错误响应")
    if protocol == "anthropic":
        valid = isinstance(parsed.get("content"), list) and bool(parsed["content"])
    elif protocol == "gemini":
        valid = isinstance(parsed.get("candidates"), list) and bool(parsed["candidates"])
    else:
        valid = isinstance(parsed.get("choices"), list) and bool(parsed["choices"])
    if not valid:
        raise ValueError("AI 接口响应缺少模型生成结果")


def _probe_request(classifier: LLMClassifier) -> None:
    """Send the smallest broadly compatible generation request for a protocol."""
    if classifier.protocol == "anthropic":
        result = classifier.client.post_json(
            classifier.endpoint,
            {
                "model": classifier.model,
                "max_tokens": 8,
                "messages": [{"role": "user", "content": "仅回复 OK"}],
            },
            headers={
                "x-api-key": classifier.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        _assert_json_response(result, protocol="anthropic")
        return

    if classifier.protocol == "gemini":
        endpoint = classifier.endpoint.replace(
            "{model}", quote(classifier.model, safe="")
        )
        result = classifier.client.post_json(
            endpoint,
            {
                "contents": [
                    {"role": "user", "parts": [{"text": "仅回复 OK"}]}
                ],
                "generationConfig": {
                    "temperature": 0,
                    "maxOutputTokens": 8,
                },
            },
            headers={"x-goog-api-key": classifier.api_key},
        )
        _assert_json_response(result, protocol="gemini")
        return

    result = classifier.client.post_json(
        classifier.endpoint,
        {
            "model": classifier.model,
            "messages": [{"role": "user", "content": "仅回复 OK"}],
            "temperature": 0,
        },
        headers={"Authorization": f"Bearer {classifier.api_key}"},
    )
    _assert_json_response(result, protocol="openai_compatible")


def test_ai_connection(
    client: HttpClient,
    ai_config: dict[str, Any],
    api_key: str,
) -> dict[str, object]:
    """Probe an AI endpoint and return a JSON-native, safely redacted summary.

    A failed probe is a normal UI result (``ok=False``), not an exception.  The
    caller can therefore show a useful status without ever serializing a raw
    exception that might contain the supplied credential.
    """
    provider = str(
        ai_config.get("provider") or ai_config.get("protocol") or "custom"
    ).strip()[:100] or "custom"
    model = str(ai_config.get("model", "")).strip()[:200]
    credential = str(api_key).strip()
    started = time.perf_counter()
    try:
        classifier = LLMClassifier(client, ai_config, api_key=credential)
        _probe_request(classifier)
    except Exception as exc:
        latency_ms = max(0, round((time.perf_counter() - started) * 1000))
        return {
            "provider": provider,
            "model": model,
            "latency_ms": latency_ms,
            "ok": False,
            "error": _safe_error(exc, credential),
        }
    latency_ms = max(0, round((time.perf_counter() - started) * 1000))
    return {
        "provider": provider,
        "model": model,
        "latency_ms": latency_ms,
        "ok": True,
    }
