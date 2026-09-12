"""Discover models using the selected endpoint and credential, including pages."""
from __future__ import annotations

import json
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .ai_connection import _safe_error


def models_endpoint(ai: dict) -> str:
    parsed = urlsplit(str(ai.get("endpoint", "")).strip())
    path = parsed.path.rstrip("/")
    protocol = ai.get("protocol", "openai_compatible")
    if protocol == "gemini" and "/models/" in path:
        path = path.split("/models/", 1)[0] + "/models"
    else:
        for suffix in ("/chat/completions", "/messages", "/completions", "/responses"):
            if path.endswith(suffix):
                path = path[:-len(suffix)]
                break
        if not path.endswith("/models"):
            path += "/models"
    return urlunsplit((parsed.scheme, parsed.netloc, path, parsed.query, ""))


def list_ai_models(client, ai: dict, credential: str, *, max_pages: int = 200) -> dict:
    protocol = ai.get("protocol", "openai_compatible")
    headers = {"Authorization": f"Bearer {credential}"}
    if protocol == "anthropic":
        headers = {"x-api-key": credential, "anthropic-version": "2023-06-01"}
    elif protocol == "gemini":
        headers = {"x-goog-api-key": credential}
    endpoint = urlsplit(models_endpoint(ai))
    query = dict(parse_qsl(endpoint.query, keep_blank_values=True))
    if protocol == "anthropic":
        query["limit"] = "1000"
    elif protocol == "gemini":
        query["pageSize"] = "1000"
    found, seen = {}, set()
    complete, message, pages, skipped = False, "", 0, 0
    try:
        for _ in range(max_pages):
            url = urlunsplit(endpoint._replace(query=urlencode(query)))
            if url in seen:
                raise ValueError("模型列表分页游标重复，未能读取完整列表")
            seen.add(url)
            result = client.request(url, headers=headers)
            if not 200 <= result.status < 300:
                raise ValueError(f"模型列表接口返回 HTTP {result.status}")
            body = json.loads(result.body)
            if not isinstance(body, dict) or body.get("error"):
                raise ValueError("模型列表接口返回错误或无效 JSON")
            rows = body.get("models" if protocol == "gemini" else "data")
            if not isinstance(rows, list):
                raise ValueError("接口未返回标准模型列表，可在高级接口设置中手动填写模型")
            pages += 1
            for row in rows:
                model_id = row.get("name" if protocol == "gemini" else "id") if isinstance(row, dict) else None
                if not isinstance(model_id, str) or not model_id.strip():
                    skipped += 1
                    continue
                model_id = model_id.removeprefix("models/") if protocol == "gemini" else model_id
                if len(model_id) > 500 or any(ord(ch) < 32 for ch in model_id):
                    skipped += 1
                    continue
                # Do not guess model families: keep all returned IDs, including
                # new/fine-tuned models. Generation support is tested separately.
                label = str(row.get("displayName") or row.get("display_name") or model_id)[:500]
                found[model_id] = {"id": model_id, "label": label if label == model_id else f"{label} · {model_id}"}
            token = body.get("nextPageToken") if protocol == "gemini" else None
            if protocol == "gemini" and token:
                if not isinstance(token, str):
                    raise ValueError("模型列表分页游标无效")
                query["pageToken"] = token
            elif body.get("has_more"):
                token = body.get("last_id") or (rows[-1].get("id") if rows and isinstance(rows[-1], dict) else None)
                if not isinstance(token, str) or not token:
                    raise ValueError("模型列表声明还有下一页，但没有分页游标")
                query["after_id" if protocol == "anthropic" else "after"] = token
            else:
                complete = True
                break
        if not complete:
            message = f"模型列表未读取完整（已读取 {pages} 页），可重试或手动填写模型"
        if skipped:
            complete = False
            message = f"有 {skipped} 条模型记录格式异常，列表可能不完整"
        if complete and not found:
            message = "该接口未返回任何模型；请检查 Key 权限，或手动填写模型后测试连接"
    except Exception as exc:
        message = _safe_error(exc, credential)
    return {"ok": bool(found) or complete, "models": sorted(found.values(), key=lambda m: m["id"].casefold()),
            "complete": complete, "pages": pages, "message": message,
            "error": message if not found and not complete else ""}
