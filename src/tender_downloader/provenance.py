from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum
from html.parser import HTMLParser
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit

from .htmlparse import decode_html
from .official_registry import (
    DEFAULT_SOURCE_REGISTRY,
    SourceRegistry,
    SourceRole,
    UrlPolicyViolation,
    canonicalize_official_url,
)


class OfficialCandidateKind(str, Enum):
    CCGP_INTENTION_PROJECT = "ccgp_intention_project"
    CCGP_INTENTION_GROUP = "ccgp_intention_group"
    OFFICIAL_ATTACHMENT = "official_attachment"
    OFFICIAL_NOTICE = "official_notice"


class CandidateDiscoveryMethod(str, Enum):
    LINK = "link"
    EMBEDDED_URL = "embedded_url"
    REDIRECT_PARAMETER = "redirect_parameter"


@dataclass(frozen=True, slots=True)
class OfficialCandidate:
    """An official URL lead that has passed registry validation only.

    ``registry_verified`` deliberately does not mean that the fetched content
    is an official notice or artifact.  The delivery pipeline must fetch and
    validate content separately before promoting a candidate.
    """

    url: str
    lead_url: str
    source_key: str
    source_name: str
    official_host: str
    kind: OfficialCandidateKind
    discovery_method: CandidateDiscoveryMethod
    link_text: str = ""
    registry_verified: bool = True
    fetch_verification_required: bool = True
    transport_upgraded: bool = False
    original_url: str = ""

    @property
    def official_url(self) -> str:
        return self.url


@dataclass(frozen=True, slots=True)
class _ExtractedLink:
    raw_url: str
    label: str
    method: CandidateDiscoveryMethod


@dataclass(frozen=True, slots=True)
class _ParsedOfficialRow:
    text: str
    links: tuple[_ExtractedLink, ...]


@dataclass(slots=True)
class _OfficialRowBuilder:
    tag: str
    text_parts: list[str]
    text_chars: int
    links: list[_ExtractedLink]


class _OfficialRowLimitExceeded(ValueError):
    """Raised internally when a group/list document exceeds parser limits."""


class _OfficialRowParser(HTMLParser):
    """Collect bounded ``tr``/``li`` contexts and their links.

    Project refinement deliberately works on row boundaries rather than on all
    links in the page.  This prevents a matching title in one table row from
    being paired with a project URL from another row.
    """

    _ROW_TAGS = {"tr", "li"}
    _MAX_ROWS = 10_000
    _MAX_ROW_TEXT_CHARS = 64 * 1024
    _MAX_ROW_LINKS = 128

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[_ParsedOfficialRow] = []
        self._row_stack: list[_OfficialRowBuilder] = []
        self._active_link: tuple[_OfficialRowBuilder, str, list[str]] | None = None

    @property
    def is_complete(self) -> bool:
        return not self._row_stack and self._active_link is None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in self._ROW_TAGS:
            if len(self.rows) + len(self._row_stack) >= self._MAX_ROWS:
                raise _OfficialRowLimitExceeded("官方列表行数超过安全上限")
            if self._active_link is not None:
                raise _OfficialRowLimitExceeded("官方列表包含跨行链接")
            self._row_stack.append(_OfficialRowBuilder(tag, [], 0, []))
            return
        if tag != "a" or not self._row_stack:
            return
        if self._active_link is not None:
            raise _OfficialRowLimitExceeded("官方列表包含嵌套链接")
        values = dict(attrs)
        href = (values.get("href") or "").strip()
        if href:
            self._active_link = (self._row_stack[-1], href, [])

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if not self._row_stack:
            return
        row = self._row_stack[-1]
        if data:
            row.text_chars += len(data)
            if row.text_chars > self._MAX_ROW_TEXT_CHARS:
                raise _OfficialRowLimitExceeded("官方列表单行文本超过安全上限")
            row.text_parts.append(data)
        if self._active_link is not None and self._active_link[0] is row and data:
            self._active_link[2].append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a" and self._active_link is not None:
            row, href, label_parts = self._active_link
            if len(row.links) >= self._MAX_ROW_LINKS:
                raise _OfficialRowLimitExceeded("官方列表单行链接数超过安全上限")
            row.links.append(_ExtractedLink(
                href,
                " ".join(label_parts).strip(),
                CandidateDiscoveryMethod.LINK,
            ))
            self._active_link = None
            return
        if tag not in self._ROW_TAGS:
            return
        if not self._row_stack or self._row_stack[-1].tag != tag:
            raise _OfficialRowLimitExceeded("官方列表行结构不完整")
        row = self._row_stack.pop()
        self.rows.append(_ParsedOfficialRow(
            text=" ".join(row.text_parts),
            links=tuple(row.links),
        ))


class _OfficialLinkParser(HTMLParser):
    _DATA_URL_ATTRIBUTES = (
        "data-url", "data-href", "data-target", "data-source-url",
        "data-original-url",
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[_ExtractedLink] = []
        self.embedded_urls: list[_ExtractedLink] = []
        self._href: str | None = None
        self._label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        tag = tag.lower()
        candidates: list[str] = []
        if tag == "a" and values.get("href"):
            candidates.append(values["href"] or "")
        candidates.extend(
            values.get(name, "") or ""
            for name in self._DATA_URL_ATTRIBUTES
            if values.get(name)
        )
        if tag in {"iframe", "embed", "object"}:
            embedded_url = values.get("src") or values.get("data") or values.get("data-src")
            if embedded_url:
                candidates.append(embedded_url)
        if tag == "a":
            self._href = candidates[0].strip() if candidates else None
            self._label_parts = []
            # Additional data attributes can contain the actual target URL.
            candidates = candidates[1:] if self._href else candidates
        label = (
            values.get("aria-label")
            or values.get("title")
            or values.get("value")
            or ""
        ).strip()
        for value in candidates:
            self.links.append(_ExtractedLink(
                value.strip(),
                label,
                CandidateDiscoveryMethod.LINK,
            ))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() == "a":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append(_ExtractedLink(
                self._href,
                " ".join(self._label_parts).strip(),
                CandidateDiscoveryMethod.LINK,
            ))
            self._href = None
            self._label_parts = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            value = re.sub(r"\s+", " ", data).strip()
            if value:
                self._label_parts.append(value)
        # Scan text and script payloads, but never rescan raw markup attributes.
        # Rescanning attributes would allow an export button that was rejected
        # above to reappear as an unlabeled embedded URL.
        for match in _ABSOLUTE_URL_RE.finditer(html.unescape(data)):
            self.embedded_urls.append(_ExtractedLink(
                _trim_url_token(match.group(0)),
                "",
                CandidateDiscoveryMethod.EMBEDDED_URL,
            ))


_ABSOLUTE_URL_RE = re.compile(
    r"(?:(?:https?:)?//)[^\s<>\"'`\\]+",
    flags=re.I,
)
_REDIRECT_QUERY_KEYS = {
    "url", "target", "targeturl", "target_url", "redirect", "redirecturl",
    "redirect_url", "jump", "jumpurl", "jump_url", "sourceurl", "source_url",
    "originalurl", "original_url",
}
_EXPORT_LABEL_RE = re.compile(
    r"(?:导出|生成|转为|另存为|保存为)\s*(?:word|pdf|docx?|\.docx?|\.pdf)",
    flags=re.I,
)
_EXPORT_URL_RE = re.compile(
    r"(?:^|[/_.?&=\-])(?:export|exportword|exportpdf|makepdf|topdf|toword)"
    r"(?:$|[/_.?&=\-])",
    flags=re.I,
)
_ATTACHMENT_PATH_RE = re.compile(
    r"\.(?:pdf|docx?|xlsx?|zip|rar|7z|ofd|txt)$",
    flags=re.I,
)
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_DETAIL_LABEL_RE = re.compile(r"(?:项目)?(?:详情|明细)|查看|公告原文|原文", flags=re.I)
_TITLE_BOUNDARY_RE = re.compile(r"[-—–－|｜]+")
_MAX_DETAIL_HTML_CHARS = 8 * 1024 * 1024


def _trim_url_token(value: str) -> str:
    return value.rstrip(".,;:!?，。；：！？、)]}）】》>")


def _validated_lead_url(lead_url: str, *, require_https: bool) -> str:
    if not isinstance(lead_url, str) or not lead_url or lead_url != lead_url.strip():
        raise UrlPolicyViolation("线索 URL 无效")
    if any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in lead_url) or "\\" in lead_url:
        raise UrlPolicyViolation("线索 URL 含空白、控制字符或反斜杠")
    try:
        parsed = urlsplit(lead_url)
        port = parsed.port
    except ValueError as exc:
        raise UrlPolicyViolation("线索 URL 主机或端口无效") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UrlPolicyViolation("线索 URL 必须是完整 HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UrlPolicyViolation("线索 URL 不允许包含用户信息")
    if require_https and parsed.scheme.lower() != "https":
        raise UrlPolicyViolation("线索 URL 必须使用 HTTPS")
    if port is not None and port not in {80, 443}:
        raise UrlPolicyViolation("线索 URL 不允许使用非标准端口")
    return lead_url.split("#", 1)[0]


def is_aggregator_export_link(
    raw_url: str,
    label: str,
    lead_url: str,
    registry: SourceRegistry | None = None,
) -> bool:
    """Identify an aggregator-generated Word/PDF action, not an original file."""

    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    try:
        lead_role = active_registry.role_for_url(lead_url)
    except UrlPolicyViolation:
        return False
    if lead_role is not SourceRole.COMMERCIAL_LEAD:
        return False
    normalized_label = re.sub(r"\s+", "", html.unescape(label or ""))
    if _EXPORT_LABEL_RE.search(normalized_label):
        return True
    try:
        absolute = urljoin(lead_url, html.unescape(raw_url or ""))
        target = urlsplit(absolute)
        lead = urlsplit(lead_url)
    except ValueError:
        return False
    if not target.hostname or not lead.hostname:
        return False
    same_host = target.hostname.lower() == lead.hostname.lower()
    return same_host and bool(_EXPORT_URL_RE.search(
        f"{target.path}?{target.query}"
    ))


def _candidate_kind(url: str) -> OfficialCandidateKind | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower()
    path_lower = parsed.path.lower().rstrip("/")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_folded = {key.casefold(): value for key, value in query.items()}

    if host == "cgyx.ccgp.gov.cn":
        if path_lower == "/cgyx/pub/proj/details":
            value = query_folded.get("projid", "")
            return (
                OfficialCandidateKind.CCGP_INTENTION_PROJECT
                if _SAFE_ID_RE.fullmatch(value)
                else None
            )
        if path_lower == "/cgyx/pub/details":
            value = query_folded.get("groupid", "")
            return (
                OfficialCandidateKind.CCGP_INTENTION_GROUP
                if _SAFE_ID_RE.fullmatch(value)
                else None
            )
        # Search, export and shell pages are not official-document candidates.
        return None

    if _ATTACHMENT_PATH_RE.search(path_lower):
        return OfficialCandidateKind.OFFICIAL_ATTACHMENT
    return OfficialCandidateKind.OFFICIAL_NOTICE


def _match_key(value: str) -> str:
    normalized = unicodedata.normalize(
        "NFKC",
        html.unescape(value or ""),
    ).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _expected_title_keys(expected_title: str) -> tuple[str, ...]:
    """Return conservative full/suffix keys for commercial lead titles.

    Lead sites commonly prefix a project title with the buyer, time range and
    ``采购意向-``.  The last meaningful title segment lets that form match the
    project name in an official group row without letting the buyer prefix
    match every row in the group.
    """

    normalized = unicodedata.normalize(
        "NFKC",
        html.unescape(expected_title or ""),
    ).strip()
    full_key = _match_key(normalized)
    if len(full_key) < 4:
        return ()
    result = [full_key]
    parts = [part.strip() for part in _TITLE_BOUNDARY_RE.split(normalized)]
    for part in reversed(parts[1:]):
        suffix_key = _match_key(part)
        if len(suffix_key) >= 4:
            if suffix_key != full_key:
                result.append(suffix_key)
            break
    return tuple(result)


def _row_title_matches(row_key: str, title_keys: tuple[str, ...]) -> bool:
    return any(key in row_key for key in title_keys)


def _detail_candidates_in_row(
    row: _ParsedOfficialRow,
    *,
    base_url: str,
    title_keys: tuple[str, ...],
    registry: SourceRegistry,
) -> tuple[OfficialCandidate, ...] | None:
    """Return one row's bounded project candidates, or ``None`` if ambiguous."""

    strong: dict[str, OfficialCandidate] = {}
    generic_detail: dict[str, OfficialCandidate] = {}
    for link in row.links:
        decoded_href = html.unescape(link.raw_url or "").strip()
        if not decoded_href or decoded_href.lower().startswith(
            ("#", "javascript:", "data:", "mailto:", "tel:")
        ):
            continue
        try:
            canonical, registration = canonicalize_official_url(
                urljoin(base_url, decoded_href),
                registry,
            )
        except (UrlPolicyViolation, ValueError):
            # This rejects plaintext HTTP, commercial and unregistered hosts.
            continue
        if canonical == base_url:
            continue
        kind = _candidate_kind(canonical)
        if kind not in {
            OfficialCandidateKind.CCGP_INTENTION_PROJECT,
            OfficialCandidateKind.OFFICIAL_NOTICE,
        }:
            # A second hop must be a project/notice page, never another group
            # page or a loosely associated attachment.
            continue
        label = re.sub(r"\s+", " ", link.label).strip()[:200]
        label_key = _match_key(label)
        label_matches_title = any(
            title_key in label_key or label_key in title_key
            for title_key in title_keys
            if len(label_key) >= 4
        )
        candidate = OfficialCandidate(
            url=canonical,
            lead_url=base_url,
            source_key=registration.key,
            source_name=registration.display_name,
            official_host=(urlsplit(canonical).hostname or "").lower(),
            kind=kind,
            discovery_method=CandidateDiscoveryMethod.LINK,
            link_text=label,
        )
        if not validate_official_candidate(candidate, registry):
            continue
        if (
            kind is OfficialCandidateKind.CCGP_INTENTION_PROJECT
            or label_matches_title
        ):
            strong.setdefault(canonical, candidate)
        elif _DETAIL_LABEL_RE.search(label):
            generic_detail.setdefault(canonical, candidate)

    selected = strong or generic_detail
    if len(selected) > 1:
        return None
    return tuple(selected.values())


def extract_matching_official_detail_candidate(
    data: bytes | str,
    official_page_url: str,
    expected_title: str,
    expected_buyer: str = "",
    expected_project_code: str = "",
    content_type: str = "",
    *,
    registry: SourceRegistry | None = None,
) -> OfficialCandidate | None:
    """Refine an official group/list page to one project-specific HTTPS URL.

    Matching is scoped to a single ``tr`` or ``li``.  Every supplied non-empty
    identity field must occur in that same row (the project code may occur in
    an ``href``).  Exactly one registered official project/notice URL must
    remain across all matching rows; malformed, oversized, missing or
    ambiguous input returns ``None``.

    The returned URL is registry-validated only.  The caller must still fetch
    it and run the normal content/provenance gate before delivery.
    """

    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    try:
        canonical_base, _ = canonicalize_official_url(
            official_page_url,
            active_registry,
        )
    except (UrlPolicyViolation, ValueError):
        return None
    title_keys = _expected_title_keys(expected_title)
    if not title_keys:
        return None
    buyer_key = _match_key(expected_buyer)
    project_code_key = _match_key(expected_project_code)

    if len(data) > _MAX_DETAIL_HTML_CHARS:
        return None
    source = data if isinstance(data, str) else decode_html(data, content_type)
    if len(source) > _MAX_DETAIL_HTML_CHARS:
        return None
    parser = _OfficialRowParser()
    try:
        parser.feed(source)
        parser.close()
    except _OfficialRowLimitExceeded:
        return None
    if not parser.is_complete:
        return None

    matches: dict[str, OfficialCandidate] = {}
    for row in parser.rows:
        searchable = " ".join((
            row.text,
            *(link.raw_url for link in row.links),
        ))
        row_key = _match_key(searchable)
        if not _row_title_matches(row_key, title_keys):
            continue
        if buyer_key and buyer_key not in row_key:
            continue
        if project_code_key and project_code_key not in row_key:
            continue
        candidates = _detail_candidates_in_row(
            row,
            base_url=canonical_base,
            title_keys=title_keys,
            registry=active_registry,
        )
        if candidates is None:
            return None
        for candidate in candidates:
            matches[candidate.url] = candidate
            if len(matches) > 1:
                return None
    if len(matches) != 1:
        return None
    return next(iter(matches.values()))


def _url_variants(raw_url: str, lead_url: str):
    decoded = html.unescape(raw_url or "").strip()
    if not decoded or decoded.startswith(("#", "javascript:", "data:", "mailto:", "tel:")):
        return
    absolute = urljoin(lead_url, decoded)
    yield absolute, CandidateDiscoveryMethod.LINK

    try:
        parsed = urlsplit(absolute)
    except ValueError:
        return
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.casefold() not in _REDIRECT_QUERY_KEYS:
            continue
        nested = value
        # One additional decoding pass handles common redirect wrappers while
        # keeping extraction bounded and deterministic.
        for _ in range(2):
            nested = html.unescape(unquote(nested)).strip()
            if nested.startswith("//"):
                nested = f"https:{nested}"
            if nested.lower().startswith(("http://", "https://")):
                yield nested, CandidateDiscoveryMethod.REDIRECT_PARAMETER
                break


def validate_official_candidate(
    candidate: OfficialCandidate,
    registry: SourceRegistry | None = None,
) -> bool:
    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    try:
        canonical, registration = canonicalize_official_url(
            candidate.url,
            active_registry,
        )
        lead = _validated_lead_url(candidate.lead_url, require_https=False)
    except UrlPolicyViolation:
        return False
    parsed = urlsplit(canonical)
    transport_valid = not candidate.transport_upgraded and not candidate.original_url
    if candidate.transport_upgraded:
        try:
            original = urlsplit(candidate.original_url)
            upgraded = original._replace(
                scheme="https",
                netloc=original.hostname or "",
                fragment="",
            ).geturl()
            upgraded_canonical, _ = canonicalize_official_url(
                upgraded, active_registry
            )
            original_registration = active_registry.registration_for_host(
                original.hostname or ""
            )
            transport_valid = (
                original.scheme.casefold() == "http"
                and original.port in {None, 80}
                and original.username is None
                and original.password is None
                and original_registration is not None
                and original_registration.role is SourceRole.OFFICIAL
                and upgraded_canonical == candidate.url
            )
        except (UrlPolicyViolation, ValueError):
            transport_valid = False
    return (
        candidate.registry_verified
        and candidate.fetch_verification_required
        and transport_valid
        and canonical == candidate.url
        and lead == candidate.lead_url
        and registration.key == candidate.source_key
        and registration.display_name == candidate.source_name
        and parsed.hostname == candidate.official_host
        and _candidate_kind(canonical) == candidate.kind
    )


def extract_official_candidates(
    data: bytes | str,
    lead_url: str,
    content_type: str = "",
    *,
    registry: SourceRegistry | None = None,
    require_https_lead: bool = True,
    cross_domain_only: bool = True,
    upgrade_registered_http: bool = False,
) -> tuple[OfficialCandidate, ...]:
    """Extract registered official links from an HTML lead page.

    Returned candidates are safe to fetch, but are not yet approved for
    delivery.  The caller must verify the fetched response and provenance.
    """

    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    normalized_lead = _validated_lead_url(
        lead_url,
        require_https=require_https_lead,
    )
    lead_host = (urlsplit(normalized_lead).hostname or "").lower()
    source = data if isinstance(data, str) else decode_html(data, content_type)
    parser = _OfficialLinkParser()
    parser.feed(source)

    # Plain text and script-embedded URLs are useful provenance clues.  Link
    # attributes are not rescanned, so a rejected export action cannot bypass
    # its label/path filter by being rediscovered as an unlabeled raw URL.
    extracted = [*parser.links, *parser.embedded_urls]

    candidates: list[OfficialCandidate] = []
    seen: set[str] = set()
    for link in extracted:
        if is_aggregator_export_link(
            link.raw_url,
            link.label,
            normalized_lead,
            active_registry,
        ):
            continue
        for variant, derived_method in _url_variants(link.raw_url, normalized_lead):
            transport_upgraded = False
            original_url = ""
            try:
                canonical, registration = canonicalize_official_url(
                    variant,
                    active_registry,
                )
            except (UrlPolicyViolation, ValueError):
                if not upgrade_registered_http:
                    continue
                try:
                    parsed_variant = urlsplit(variant)
                    registered = active_registry.registration_for_host(
                        parsed_variant.hostname or ""
                    )
                    if (
                        parsed_variant.scheme.casefold() != "http"
                        or parsed_variant.port not in {None, 80}
                        or parsed_variant.username is not None
                        or parsed_variant.password is not None
                        or registered is None
                        or registered.role is not SourceRole.OFFICIAL
                    ):
                        continue
                    upgraded = parsed_variant._replace(
                        scheme="https",
                        netloc=parsed_variant.hostname or "",
                        fragment="",
                    ).geturl()
                    canonical, registration = canonicalize_official_url(
                        upgraded,
                        active_registry,
                    )
                    transport_upgraded = True
                    original_url = variant.split("#", 1)[0]
                except (UrlPolicyViolation, ValueError):
                    continue
            target_host = (urlsplit(canonical).hostname or "").lower()
            if cross_domain_only and target_host == lead_host:
                continue
            kind = _candidate_kind(canonical)
            if kind is None or canonical in seen:
                continue
            seen.add(canonical)
            discovery_method = (
                derived_method
                if derived_method is CandidateDiscoveryMethod.REDIRECT_PARAMETER
                else link.method
            )
            candidate = OfficialCandidate(
                url=canonical,
                lead_url=normalized_lead,
                source_key=registration.key,
                source_name=registration.display_name,
                official_host=target_host,
                kind=kind,
                discovery_method=discovery_method,
                link_text=re.sub(r"\s+", " ", link.label).strip()[:200],
                transport_upgraded=transport_upgraded,
                original_url=original_url,
            )
            if validate_official_candidate(candidate, active_registry):
                candidates.append(candidate)
    return tuple(candidates)


__all__ = [
    "CandidateDiscoveryMethod",
    "OfficialCandidate",
    "OfficialCandidateKind",
    "extract_matching_official_detail_candidate",
    "extract_official_candidates",
    "is_aggregator_export_link",
    "validate_official_candidate",
]
