from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ArtifactInspection:
    blocked: bool = False
    needs_review: bool = False
    reason: str = ""


def inspect_artifact(path: Path, original_name: str, content_type: str) -> ArtifactInspection:
    with path.open("rb") as handle:
        preview = handle.read(8192)
    stripped = preview.lstrip().lower()
    suffix = Path(original_name).suffix.lower()
    content_type = content_type.lower()

    html_like = stripped.startswith((b"<!doctype html", b"<html", b"<script"))
    block_markers = (
        "请输入验证码", "访问过于频繁", "登录后下载", "waf challenge", "access denied"
    )
    decoded = preview.decode("utf-8", errors="ignore").lower()
    if (html_like and suffix not in (".html", ".htm")) or any(
        marker.lower() in decoded for marker in block_markers
    ):
        return ArtifactInspection(
            blocked=True,
            needs_review=True,
            reason="附件响应实际为 HTML/验证码/登录页面",
        )

    is_pdf = preview.startswith(b"%PDF-")
    is_zip = preview.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"))
    is_ole = preview.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    if (suffix == ".pdf" or content_type == "application/pdf") and not is_pdf:
        return ArtifactInspection(needs_review=True, reason="PDF 扩展名/MIME 与文件魔数不符")
    if suffix in (".docx", ".xlsx", ".zip", ".ofd") and not is_zip:
        return ArtifactInspection(needs_review=True, reason="ZIP/OOXML 扩展名与文件魔数不符")
    if suffix in (".doc", ".xls") and not is_ole:
        return ArtifactInspection(needs_review=True, reason="旧版 Office 扩展名与文件魔数不符")
    return ArtifactInspection()
