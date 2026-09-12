from __future__ import annotations

import hashlib
import re
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlencode, urlparse


WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_component(value: str, fallback: str = "未命名", max_length: int = 120) -> str:
    value = unquote(value or "").strip()
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]", "_", value)
    value = re.sub(r"\s+", " ", value).rstrip(". ")
    if not value:
        value = fallback
    stem = value.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED:
        value = f"_{value}"
    if len(value) > max_length:
        suffix = Path(value).suffix
        if len(suffix) > 16:
            suffix = ""
        digest = hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:8]
        budget = max(1, max_length - len(suffix) - len(digest) - 1)
        value = f"{value[:budget]}_{digest}{suffix}"[:max_length]
    return value


def filename_from_url(url: str, fallback: str = "source.bin") -> str:
    name = Path(urlparse(url).path).name
    return safe_component(name or fallback, fallback=fallback)


def stable_id(value: str, length: int = 16) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def canonical_url_key(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    ignored = {"utm_source", "utm_medium", "utm_campaign", "from", "spm"}
    query = urlencode(sorted(
        (key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in ignored
    ))
    return f"{host}{port}{path}" + (f"?{query}" if query else "")
