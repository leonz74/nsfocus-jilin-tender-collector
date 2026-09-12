from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal


@dataclass(frozen=True, slots=True)
class AttachmentRef:
    url: str
    original_name: str | None = None
    label: str = ""
    access: Literal[
        "public_direct",
        "public_browser",
        "login_required",
        "ca_required",
        "missing",
    ] = "public_direct"
    referer: str | None = None


@dataclass(frozen=True, slots=True)
class RawDocument:
    body: bytes
    url: str
    headers: dict[str, str]
    analysis_body: bytes | None = None


@dataclass(slots=True)
class Notice:
    source: str
    authority_rank: int
    external_id: str
    title: str
    published_at: str
    url: str
    region: str = "吉林省"
    notice_type: str = ""
    buyer: str = ""
    body_text: str = ""
    attachments: list[AttachmentRef] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    discovery_url: str = ""
    publication_url: str = ""
    origin_url: str = ""
    source_role: str = "unknown"
    origin_verified: bool = False

    @property
    def identity(self) -> str:
        return f"{self.source}:{self.external_id}"


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    sha256: str
    size: int
    original_name: str
    vault_path: Path
    source_url: str
    content_type: str = ""
    fetched_at: str = field(default_factory=lambda: datetime.now().astimezone().isoformat())
    original_name_raw: str = ""
    final_url: str = ""
    document_kind: str = "unknown"
    source_role: str = "unknown"
    origin_verified: bool = False
    gate_status: str = "unverified"
    gate_code: str = ""
    delivery_eligible: bool = False
    discovery_url: str = ""
    official_notice_url: str = ""


@dataclass(frozen=True, slots=True)
class Classification:
    relevant: bool | None
    confidence: float
    industry: str
    security_categories: tuple[str, ...]
    evidence: tuple[str, ...]
    reason: str
    method: str
    ai_confirmed: bool
    needs_review: bool = False


@dataclass(slots=True)
class Coverage:
    source: str
    status: str = "running"
    target_start: str = ""
    target_end: str = ""
    first_date: str = ""
    last_date: str = ""
    reached_start: bool = False
    reached_end: bool = False
    truncated: bool = False
    pages: int = 0
    notices: int = 0
    details_ok: int = 0
    attachments_found: int = 0
    restricted_files: int = 0
    downloads_ok: int = 0
    downloads_failed: int = 0
    blocked: int = 0
    message: str = ""
