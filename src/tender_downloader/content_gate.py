"""Deterministic source/content gate for immutable tender delivery.

This module deliberately contains no AI integration.  A positive classifier result
must never be able to turn an untrusted response into a deliverable original.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from enum import StrEnum
from html import unescape
from html.parser import HTMLParser
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import unquote, urlparse


class ContentGateError(StrEnum):
    """Stable machine-readable rejection codes."""

    INVALID_SOURCE_ROLE = "invalid_source_role"
    COMMERCIAL_AGGREGATOR = "commercial_aggregator_not_deliverable"
    INVALID_ORIGIN_URL = "invalid_origin_url"
    INVALID_FINAL_URL = "invalid_final_url"
    INSECURE_ORIGIN_TRANSPORT = "insecure_origin_transport"
    INSECURE_FINAL_TRANSPORT = "insecure_final_transport"
    UNREGISTERED_OFFICIAL_HOST = "unregistered_official_host"
    CROSS_ORIGIN_REDIRECT = "cross_origin_redirect"
    INSECURE_REDIRECT = "insecure_redirect"
    EMPTY_BODY = "empty_body"
    CAPTCHA_PAGE = "captcha_page"
    WAF_BLOCK_PAGE = "waf_block_page"
    LOGIN_PAGE = "login_page"
    EMPTY_SPA_SHELL = "empty_spa_shell"
    RAW_RENDER_MISMATCH = "raw_render_mismatch"
    MIME_MAGIC_MISMATCH = "mime_magic_mismatch"
    MALFORMED_JSON = "malformed_json"
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    TITLE_MISMATCH = "expected_title_mismatch"
    BUYER_MISMATCH = "expected_buyer_mismatch"
    PROJECT_CODE_MISMATCH = "expected_project_code_mismatch"
    DOCUMENT_KIND_MISMATCH = "document_kind_mismatch"


# Exact hosts only.  New official download/CDN hosts must be explicitly registered;
# suffix and substring matching are intentionally not used.
DEFAULT_OFFICIAL_HOSTS = frozenset(
    {
        "ccgp.gov.cn",
        "www.ccgp.gov.cn",
        "cgyx.ccgp.gov.cn",
        "search.ccgp.gov.cn",
        "ccgp-jilin.gov.cn",
        "www.ccgp-jilin.gov.cn",
        "ggzy.gov.cn",
        "www.ggzy.gov.cn",
        "jl.gov.cn",
        "www.jl.gov.cn",
        "jzcg.pbc.gov.cn",
        "pbc.gov.cn",
        "www.pbc.gov.cn",
    }
)


# These are lead/discovery sites, never an immutable-original authority.  The list
# is only a defence in depth measure: callers must also set source_role correctly.
COMMERCIAL_AGGREGATOR_HOSTS = frozenset(
    {
        "okcis.cn",
        "www.okcis.cn",
        "chinabidding.cn",
        "www.chinabidding.cn",
        "bidcenter.com.cn",
        "www.bidcenter.com.cn",
        "qianlima.com",
        "www.qianlima.com",
        "chinabidding.com",
        "www.chinabidding.com",
        "bidding.cn",
        "www.bidding.cn",
    }
)


@dataclass(frozen=True, slots=True)
class ContentGatePolicy:
    """Trust policy for a deployment.

    ``official_hosts`` and redirect pairs are exact normalized host names.  A
    legitimate official file host can be added explicitly without weakening the
    default same-origin rule.
    """

    official_hosts: frozenset[str] = DEFAULT_OFFICIAL_HOSTS
    allowed_redirect_pairs: frozenset[tuple[str, str]] = frozenset()
    minimum_html_text_chars: int = 80
    # 仅供本地单元测试 HTTP 夹具；生产 Pipeline 永远不会设置此项。
    insecure_test_hosts: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class ContentGateInput:
    source_role: str
    origin: str
    final_url: str
    content_type: str
    body: bytes
    analysis_body: bytes | None = None
    expected_title: str = ""
    expected_buyer: str = ""
    expected_project_code: str = ""
    document_kind: str = ""


@dataclass(frozen=True, slots=True)
class ContentGateVerdict:
    verdict: Literal["allow", "reject"]
    error_codes: tuple[str, ...]
    reasons: tuple[str, ...]
    detected_kind: str
    source_host: str
    final_host: str
    raw_sha256: str
    analysis_sha256: str

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"

    @property
    def deliverable(self) -> bool:
        return self.allowed


class _VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style", "noscript", "template", "svg"} and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data.strip():
            self.parts.append(data)


_SOURCE_ROLE_OFFICIAL = {"official", "official_source", "authority", "government", "官方", "官方来源"}
_SOURCE_ROLE_AGGREGATOR = {
    "aggregator",
    "commercial_aggregator",
    "commercial_lead",
    "lead",
    "discovery",
    "商业聚合",
    "聚合站",
    "线索",
}

_HTML_MIMES = {"text/html", "application/xhtml+xml"}
_JSON_MIMES = {"application/json", "text/json"}
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_DOC_MIMES = {"application/msword", "application/vnd.ms-word", "application/doc"}
_XLS_MIMES = {"application/vnd.ms-excel", "application/msexcel", "application/x-msexcel"}
_ZIP_MIMES = {"application/zip", "application/x-zip-compressed"}
_RAR_MIMES = {"application/vnd.rar", "application/x-rar-compressed", "application/rar"}
_SEVEN_Z_MIMES = {"application/x-7z-compressed"}
_GENERIC_DOWNLOAD_MIMES = {
    "",
    "application/octet-stream",
    "application/download",
    "application/x-download",
    "binary/octet-stream",
}

_CAPTCHA_PATTERNS = (
    r"请输入.{0,8}验证码",
    r"请完成.{0,8}(?:安全)?验证",
    r"(?:访问|安全|人机|滑块)验证",
    r"验证码.{0,12}(?:错误|过期|刷新|输入)",
    r"(?:captcha|geetest|hcaptcha|recaptcha)(?:[\s_./?'\"=-]|$)",
    r"challenge-platform",
    r"aliyun.*captcha",
)
_WAF_PATTERNS = (
    r"waf[\s_-]*challenge",
    r"cloudflare\s+ray\s+id",
    r"cf-chl-",
    r"access\s+denied",
    r"request\s+blocked",
    r"访问过于频繁",
    r"请求过于频繁",
    r"您的访问已被拦截",
    r"异常访问",
)
_LOGIN_PATTERNS = (
    r"<input[^>]+type\s*=\s*['\"]?password",
    r"登录后(?:查看|下载|访问|获取)",
    r"请先登录",
    r"账号密码登录",
    r"<title[^>]*>\s*(?:用户)?登录\s*</title>",
)

_MEDIA_KIND_ALIASES = {
    "html": "html",
    "official_html": "html",
    "official_page": "html",
    "官方网页": "html",
    "网页": "html",
    "json": "json",
    "official_json": "json",
    "官方json": "json",
    "pdf": "pdf",
    "docx": "docx",
    "doc": "doc",
    "xls": "xls",
    "xlsx": "xlsx",
    "zip": "zip",
    "rar": "rar",
    "7z": "7z",
}
_GENERIC_ATTACHMENT_KINDS = {
    "attachment",
    "official_attachment",
    "附件",
    "官方附件",
    "招标文件",
    "采购文件",
}
_NOTICE_KIND_TERMS = {
    "采购意向": ("采购意向",),
    "招标公告": ("招标公告", "公开招标", "邀请招标"),
    "采购公告": ("采购公告", "招标公告", "竞争性磋商", "竞争性谈判", "询价公告"),
    "中标公告": ("中标公告", "中标结果", "成交公告", "成交结果"),
    "成交公告": ("成交公告", "成交结果", "中标公告", "中标结果"),
    "合同公告": ("合同公告", "采购合同"),
    "更正公告": ("更正公告", "变更公告", "澄清公告"),
    "废标公告": ("废标公告", "终止公告", "流标公告"),
}


def evaluate_content(
    item: ContentGateInput,
    policy: ContentGatePolicy | None = None,
) -> ContentGateVerdict:
    """Return a deterministic delivery decision.

    All checks operate on immutable network bytes (``body``).  ``analysis_body``
    may help detect browser/raw channel divergence but can never make a failing raw
    response acceptable.
    """

    policy = policy or ContentGatePolicy()
    errors: list[ContentGateError] = []
    reasons: list[str] = []

    def reject(code: ContentGateError, reason: str) -> None:
        if code not in errors:
            errors.append(code)
            reasons.append(reason)

    role = unicodedata.normalize("NFKC", item.source_role).strip().casefold()
    source_url = _parse_http_url(item.origin)
    final_url = _parse_http_url(item.final_url)
    source_host = source_url[1] if source_url else ""
    final_host = final_url[1] if final_url else ""

    if role in _SOURCE_ROLE_AGGREGATOR:
        reject(ContentGateError.COMMERCIAL_AGGREGATOR, "商业聚合站只能作为线索，不能交付为官方原件")
    elif role not in _SOURCE_ROLE_OFFICIAL:
        reject(ContentGateError.INVALID_SOURCE_ROLE, "来源角色不是已确认的官方来源")

    if not source_url:
        reject(ContentGateError.INVALID_ORIGIN_URL, "来源地址不是有效的 HTTP(S) 官方地址")
    if not final_url:
        reject(ContentGateError.INVALID_FINAL_URL, "最终地址不是有效的 HTTP(S) 地址")

    insecure_test_hosts = {
        _normalize_host(host) for host in policy.insecure_test_hosts
    }
    if source_url and source_url[0] != "https" and source_host not in insecure_test_hosts:
        reject(
            ContentGateError.INSECURE_ORIGIN_TRANSPORT,
            "官方原件来源不是 HTTPS，无法证明传输中的原始字节未被替换",
        )
    if final_url and final_url[0] != "https" and final_host not in insecure_test_hosts:
        reject(
            ContentGateError.INSECURE_FINAL_TRANSPORT,
            "官方原件最终响应不是 HTTPS，禁止进入交付目录",
        )

    if _is_commercial_host(source_host) or _is_commercial_host(final_host):
        reject(ContentGateError.COMMERCIAL_AGGREGATOR, "地址属于已知商业聚合站，只能保留为线索")

    trusted_hosts = {_normalize_host(host) for host in policy.official_hosts}
    for host in (source_host, final_host):
        if host and host not in trusted_hosts:
            reject(ContentGateError.UNREGISTERED_OFFICIAL_HOST, f"主机 {host} 未在官方主机注册表中")

    if source_url and final_url:
        source_scheme, _, source_port = source_url
        final_scheme, _, final_port = final_url
        allowed_pair = (source_host, final_host) in {
            (_normalize_host(left), _normalize_host(right))
            for left, right in policy.allowed_redirect_pairs
        }
        if (source_host != final_host or source_port != final_port) and not allowed_pair:
            reject(ContentGateError.CROSS_ORIGIN_REDIRECT, "最终响应发生了未经显式授权的跨主机或跨端口跳转")
        if source_scheme == "https" and final_scheme != "https":
            reject(ContentGateError.INSECURE_REDIRECT, "HTTPS 来源被降级跳转到非 HTTPS 地址")

    raw_sha = hashlib.sha256(item.body).hexdigest()
    analysis_sha = hashlib.sha256(item.analysis_body).hexdigest() if item.analysis_body is not None else ""
    if not item.body:
        reject(ContentGateError.EMPTY_BODY, "原始网络响应为空")

    mime = _canonical_mime(item.content_type)
    suffix = PurePosixPath(unquote(urlparse(item.final_url).path)).suffix.casefold()
    detected_kind = _detect_kind(item.body)
    raw_decoded = _decode_text(item.body, item.content_type)

    if detected_kind in {"html", "json", "unknown"}:
        _check_block_pages(raw_decoded, reject)
    # The national platform serves its linked /html/b/ announcement fragments
    # as text/plain;charset=UTF-8. Preserve that real header and response while
    # accepting HTML only on this observed official endpoint. Block-page and
    # announcement-semantic checks below still apply.
    national_plain_html = bool(
        source_host == final_host == "www.ggzy.gov.cn"
        and re.fullmatch(r"/information/deal/html/b/\d{6}/\d{4}/\d{8}/[A-Za-z0-9]+\.html",
                         urlparse(item.final_url).path)
    )
    _check_mime_and_magic(item.body, detected_kind, mime, suffix, reject,
                          allow_plain_html=national_plain_html)

    raw_semantic_text = ""
    semantic_available = False
    if detected_kind == "html":
        raw_semantic_text = _visible_html_text(raw_decoded)
        semantic_available = True
        if _is_empty_spa_shell(raw_decoded, raw_semantic_text, policy.minimum_html_text_chars):
            reject(ContentGateError.EMPTY_SPA_SHELL, "原始 HTML 只有空壳或脚本，未包含可交付正文")
    elif detected_kind == "json":
        try:
            parsed_json = json.loads(raw_decoded)
            if not isinstance(parsed_json, (dict, list)):
                raise ValueError("top-level JSON must be an object or array")
            raw_semantic_text = _json_text(parsed_json)
            semantic_available = True
        except (json.JSONDecodeError, ValueError):
            reject(ContentGateError.MALFORMED_JSON, "JSON 响应无法解析为对象或数组")
    elif detected_kind == "docx":
        raw_semantic_text = _docx_text(item.body)
        semantic_available = bool(raw_semantic_text)

    if item.analysis_body is not None and detected_kind in {"html", "json"}:
        analysis_decoded = _decode_text(item.analysis_body, item.content_type)
        _check_block_pages(analysis_decoded, reject)
        if detected_kind == "html":
            analysis_text = _visible_html_text(analysis_decoded)
        else:
            try:
                analysis_text = _json_text(json.loads(analysis_decoded))
            except (json.JSONDecodeError, TypeError, ValueError):
                analysis_text = _visible_html_text(analysis_decoded)
        if not _raw_render_consistent(raw_semantic_text, analysis_text):
            reject(ContentGateError.RAW_RENDER_MISMATCH, "原始响应与浏览器渲染内容语义不一致")

    _check_document_kind(item.document_kind, detected_kind, raw_semantic_text, reject)
    if semantic_available:
        _check_expected_semantics(item, raw_semantic_text, reject)

    return ContentGateVerdict(
        verdict="reject" if errors else "allow",
        error_codes=tuple(str(code) for code in errors),
        reasons=tuple(reasons),
        detected_kind=detected_kind,
        source_host=source_host,
        final_host=final_host,
        raw_sha256=raw_sha,
        analysis_sha256=analysis_sha,
    )


def _parse_http_url(value: str) -> tuple[str, str, int] | None:
    try:
        parsed = urlparse(value.strip())
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        host = _normalize_host(parsed.hostname)
        port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    except (UnicodeError, ValueError):
        return None
    return parsed.scheme.casefold(), host, port


def _normalize_host(host: str) -> str:
    value = unicodedata.normalize("NFKC", host).strip().rstrip(".").casefold()
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _is_commercial_host(host: str) -> bool:
    # Domain-boundary matching covers m.example.tld without accepting
    # example.tld.attacker.invalid.
    roots = {value.removeprefix("www.") for value in COMMERCIAL_AGGREGATOR_HOSTS}
    return any(host == root or host.endswith("." + root) for root in roots)


def _canonical_mime(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().casefold()


def _detect_kind(body: bytes) -> str:
    if body.startswith(b"%PDF-"):
        return "pdf"
    if body.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
        ooxml_kind = _ooxml_kind(body)
        if ooxml_kind:
            return ooxml_kind
        if _valid_zip(body):
            return "zip"
        return "invalid_zip"
    if body.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        streams = _cfb_stream_names(body)
        has_word = "worddocument" in streams
        has_workbook = "workbook" in streams or "book" in streams
        if has_word and not has_workbook:
            return "doc"
        if has_workbook and not has_word:
            return "xls"
        # Do not guess solely from a user-controlled extension.  MIME/suffix are
        # used only to report a more useful mismatch for a structurally valid CFB.
        return "ole"
    if body.startswith((b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")):
        return "rar" if _valid_rar_header(body) else "invalid_rar"
    if body.startswith(b"7z\xbc\xaf\x27\x1c"):
        return "7z" if _valid_7z_header(body) else "invalid_7z"
    stripped_body = body[3:] if body.startswith(b"\xef\xbb\xbf") else body
    stripped = stripped_body.lstrip(b"\x00\t\r\n ")[:1024].lower()
    if stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body", b"<script", b"<meta", b"<div")) or (
        stripped.startswith((b"<!--", b"<?xml")) and b"<html" in stripped
    ):
        return "html"
    if stripped.startswith((b"{", b"[")):
        return "json"
    return "unknown"


def _valid_zip(body: bytes) -> bool:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 100_000:
                return False
            if not any(not info.is_dir() and info.file_size > 0 for info in infos):
                return False
            total_uncompressed = 0
            for info in infos:
                total_uncompressed += info.file_size
                if total_uncompressed > 4 * 1024 * 1024 * 1024:
                    return False
                if info.file_size > 1024 and info.compress_size == 0:
                    return False
                if info.compress_size and info.file_size / info.compress_size > 1000:
                    return False
                if not info.is_dir():
                    offset = info.header_offset
                    if offset < 0 or offset + 30 > len(body) or body[offset : offset + 4] != b"PK\x03\x04":
                        return False
                    name_size, extra_size = struct.unpack_from("<HH", body, offset + 26)
                    data_offset = offset + 30 + name_size + extra_size
                    if data_offset + info.compress_size > len(body):
                        return False
            return True
    except (OSError, zipfile.BadZipFile):
        return False


def _ooxml_kind(body: bytes) -> str:
    if not _valid_zip(body):
        return ""
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names:
                return ""
            if "word/document.xml" in names:
                document = archive.getinfo("word/document.xml")
                return "docx" if document.file_size <= 16 * 1024 * 1024 else ""
            if "xl/workbook.xml" in names:
                workbook = archive.getinfo("xl/workbook.xml")
                return "xlsx" if workbook.file_size <= 16 * 1024 * 1024 else ""
    except (OSError, zipfile.BadZipFile):
        return ""
    return ""


def _cfb_stream_names(body: bytes) -> set[str]:
    """Read Compound File Binary directory names without opening stream data."""

    if len(body) < 1536 or body[:8] != b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return set()
    try:
        if body[0x1C:0x1E] != b"\xfe\xff":
            return set()
        sector_shift = struct.unpack_from("<H", body, 0x1E)[0]
        if sector_shift not in {9, 12}:
            return set()
        sector_size = 1 << sector_shift
        if len(body) < sector_size:
            return set()
        fat_count = struct.unpack_from("<I", body, 0x2C)[0]
        first_directory = struct.unpack_from("<I", body, 0x30)[0]
        first_difat = struct.unpack_from("<I", body, 0x44)[0]
        difat_count = struct.unpack_from("<I", body, 0x48)[0]
        fat_sector_ids = [
            value
            for value in struct.unpack_from("<109I", body, 0x4C)
            if value < 0xFFFFFFFC
        ]
        current_difat = first_difat
        seen_difat: set[int] = set()
        for _ in range(min(difat_count, 4096)):
            if current_difat >= 0xFFFFFFFC or current_difat in seen_difat:
                return set()
            seen_difat.add(current_difat)
            sector = _cfb_sector(body, current_difat, sector_size)
            if sector is None:
                return set()
            entries = struct.unpack(f"<{sector_size // 4}I", sector)
            fat_sector_ids.extend(value for value in entries[:-1] if value < 0xFFFFFFFC)
            current_difat = entries[-1]
        if len(fat_sector_ids) < fat_count:
            return set()
        fat_entries: list[int] = []
        for sector_id in fat_sector_ids[:fat_count]:
            sector = _cfb_sector(body, sector_id, sector_size)
            if sector is None:
                return set()
            fat_entries.extend(struct.unpack(f"<{sector_size // 4}I", sector))

        directory_bytes = bytearray()
        current = first_directory
        seen: set[int] = set()
        for _ in range(100_000):
            if current == 0xFFFFFFFE:
                break
            if current >= len(fat_entries) or current in seen:
                return set()
            seen.add(current)
            sector = _cfb_sector(body, current, sector_size)
            if sector is None:
                return set()
            directory_bytes.extend(sector)
            current = fat_entries[current]
        else:
            return set()

        names: set[str] = set()
        for offset in range(0, len(directory_bytes), 128):
            entry = directory_bytes[offset : offset + 128]
            if len(entry) != 128 or entry[66] not in {1, 2, 5}:
                continue
            name_size = struct.unpack_from("<H", entry, 64)[0]
            if name_size < 2 or name_size > 64 or name_size % 2:
                continue
            name = bytes(entry[: name_size - 2]).decode("utf-16le", errors="strict")
            names.add(name.casefold())
        return names
    except (UnicodeDecodeError, struct.error, ValueError):
        return set()


def _cfb_sector(body: bytes, sector_id: int, sector_size: int) -> bytes | None:
    offset = (sector_id + 1) * sector_size
    end = offset + sector_size
    return body[offset:end] if end <= len(body) else None


def _valid_rar_header(body: bytes) -> bool:
    if body.startswith(b"Rar!\x1a\x07\x00"):
        # A RAR 4 archive starts with a MAIN_HEAD (0x73), whose fixed header is
        # 13 bytes including six reserved bytes.
        if len(body) < 20 or body[9] != 0x73:
            return False
        stored_crc = struct.unpack_from("<H", body, 7)[0]
        header_size = struct.unpack_from("<H", body, 12)[0]
        header_end = 7 + header_size
        return (
            header_size >= 13
            and header_end <= len(body)
            and zlib.crc32(body[9:header_end]) & 0xFFFF == stored_crc
        )
    if body.startswith(b"Rar!\x1a\x07\x01\x00"):
        if len(body) < 14:
            return False
        stored_crc = struct.unpack_from("<I", body, 8)[0]
        parsed_size = _rar_vint(body, 12)
        if parsed_size is None:
            return False
        header_size, after_size = parsed_size
        parsed_type = _rar_vint(body, after_size)
        if parsed_type is None or parsed_type[0] != 1:
            return False
        header_end = after_size + header_size
        if header_size < parsed_type[1] - after_size or header_end > len(body):
            return False
        return zlib.crc32(body[12:header_end]) & 0xFFFFFFFF == stored_crc
    return False


def _rar_vint(body: bytes, offset: int) -> tuple[int, int] | None:
    value = 0
    shift = 0
    for position in range(offset, min(len(body), offset + 10)):
        current = body[position]
        value |= (current & 0x7F) << shift
        if not current & 0x80:
            return value, position + 1
        shift += 7
    return None


def _valid_7z_header(body: bytes) -> bool:
    if len(body) < 32 or not body.startswith(b"7z\xbc\xaf\x27\x1c"):
        return False
    stored_crc = struct.unpack_from("<I", body, 8)[0]
    if zlib.crc32(body[12:32]) & 0xFFFFFFFF != stored_crc:
        return False
    next_offset, next_size, next_crc = struct.unpack_from("<QQI", body, 12)
    next_start = 32 + next_offset
    next_end = next_start + next_size
    if next_start > len(body) or next_end > len(body):
        return False
    return zlib.crc32(body[next_start:next_end]) & 0xFFFFFFFF == next_crc


def _check_mime_and_magic(
    body: bytes,
    kind: str,
    mime: str,
    suffix: str,
    reject,
    *,
    allow_plain_html: bool = False,
) -> None:
    expected_from_mime = ""
    if mime in _HTML_MIMES:
        expected_from_mime = "html"
    elif mime in _JSON_MIMES or mime.endswith("+json"):
        expected_from_mime = "json"
    elif mime == "application/pdf":
        expected_from_mime = "pdf"
    elif mime == _DOCX_MIME:
        expected_from_mime = "docx"
    elif mime == _XLSX_MIME:
        expected_from_mime = "xlsx"
    elif mime in _DOC_MIMES:
        expected_from_mime = "doc"
    elif mime in _XLS_MIMES:
        expected_from_mime = "xls"
    elif mime in _ZIP_MIMES:
        # DOCX/XLSX are ZIP containers and are frequently served with a generic
        # ZIP MIME. Their verified internal OOXML structure remains authoritative.
        expected_from_mime = kind if kind in {"docx", "xlsx"} else "zip"
    elif mime in _RAR_MIMES:
        expected_from_mime = "rar"
    elif mime in _SEVEN_Z_MIMES:
        expected_from_mime = "7z"

    expected_from_suffix = {
        ".html": "html",
        ".htm": "html",
        ".json": "json",
        ".pdf": "pdf",
        ".doc": "doc",
        ".docx": "docx",
        ".xls": "xls",
        ".xlsx": "xlsx",
        ".zip": "zip",
        ".rar": "rar",
        ".7z": "7z",
    }.get(suffix, "")
    if expected_from_mime and expected_from_mime != kind:
        reject(ContentGateError.MIME_MAGIC_MISMATCH, f"MIME 声明为 {expected_from_mime}，文件魔数/结构实际为 {kind}")
    if expected_from_suffix and expected_from_suffix != kind:
        reject(ContentGateError.MIME_MAGIC_MISMATCH, f"地址扩展名声明为 {expected_from_suffix}，文件魔数/结构实际为 {kind}")

    compatible = False
    if kind == "html":
        compatible = (mime in _HTML_MIMES or (not mime and suffix in {"", ".html", ".htm"})
                      or (allow_plain_html and mime == "text/plain"))
    elif kind == "json":
        compatible = mime in _JSON_MIMES or mime.endswith("+json") or (not mime and suffix == ".json")
    elif kind == "pdf":
        compatible = mime == "application/pdf" or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "docx":
        compatible = mime == _DOCX_MIME or mime in _ZIP_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "doc":
        compatible = mime in _DOC_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "xls":
        compatible = mime in _XLS_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "xlsx":
        compatible = mime == _XLSX_MIME or mime in _ZIP_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "zip":
        compatible = mime in _ZIP_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "rar":
        compatible = mime in _RAR_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    elif kind == "7z":
        compatible = mime in _SEVEN_Z_MIMES or mime in _GENERIC_DOWNLOAD_MIMES
    if not compatible:
        if kind in {"html", "json", "pdf", "doc", "docx", "xls", "xlsx", "zip", "rar", "7z"}:
            reject(ContentGateError.MIME_MAGIC_MISMATCH, f"Content-Type {mime or '(缺失)'} 与实际 {kind} 内容不兼容")
        else:
            reject(
                ContentGateError.UNSUPPORTED_MEDIA_TYPE,
                "仅允许官方 HTML、JSON、PDF、Word、Excel、ZIP、RAR 或 7Z 原始响应",
            )

    if kind == "pdf" and b"%%EOF" not in body[-4096:]:
        reject(ContentGateError.MIME_MAGIC_MISMATCH, "PDF 缺少结束标记，响应可能被截断")


def _check_block_pages(decoded: str, reject) -> None:
    folded = unicodedata.normalize("NFKC", decoded).casefold()
    if any(re.search(pattern, folded, flags=re.I | re.S) for pattern in _CAPTCHA_PATTERNS):
        reject(ContentGateError.CAPTCHA_PAGE, "响应是验证码或人机验证页面")
    if any(re.search(pattern, folded, flags=re.I | re.S) for pattern in _WAF_PATTERNS):
        reject(ContentGateError.WAF_BLOCK_PAGE, "响应是 WAF/限流/访问拦截页面")
    if any(re.search(pattern, folded, flags=re.I | re.S) for pattern in _LOGIN_PATTERNS):
        reject(ContentGateError.LOGIN_PAGE, "响应是登录页或登录后可见提示")


def _decode_text(body: bytes, content_type: str) -> str:
    charset = re.search(r"charset\s*=\s*['\"]?([\w.-]+)", content_type, flags=re.I)
    candidates = [charset.group(1)] if charset else []
    candidates.extend(["utf-8-sig", "gb18030"])
    for encoding in candidates:
        try:
            return body.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return body.decode("utf-8", errors="replace")


def _visible_html_text(decoded: str) -> str:
    # Browser analysis payloads may be plain visible text rather than serialized DOM.
    if "<" not in decoded and ">" not in decoded:
        return _collapse_text(decoded)
    parser = _VisibleTextParser()
    try:
        parser.feed(decoded)
        parser.close()
    except Exception:
        # Malformed public HTML is common; the deterministic fallback still strips
        # tags without executing any content.
        return _collapse_text(re.sub(r"<[^>]*>", " ", decoded))
    return _collapse_text(" ".join(parser.parts))


def _collapse_text(value: str) -> str:
    return re.sub(r"\s+", " ", unescape(value)).strip()


def _is_empty_spa_shell(decoded: str, visible_text: str, minimum_chars: int) -> bool:
    normalized_text = _semantic_normalize(visible_text)
    if len(normalized_text) >= minimum_chars:
        return False
    lowered = decoded.casefold()
    has_mount = bool(re.search(r"<(?:div|main)[^>]+id\s*=\s*['\"](?:app|root|__next|rootapp)['\"]", lowered))
    script_count = len(re.findall(r"<script\b", lowered))
    return has_mount or script_count > 0 or len(normalized_text) < 20


def _raw_render_consistent(raw_text: str, analysis_text: str) -> bool:
    raw = _semantic_normalize(raw_text)
    rendered = _semantic_normalize(analysis_text)
    if not raw or not rendered:
        return raw == rendered
    if raw in rendered or rendered in raw:
        return True
    raw_shingles = _shingles(raw)
    rendered_shingles = _shingles(rendered)
    if not raw_shingles or not rendered_shingles:
        return raw == rendered
    containment = len(raw_shingles & rendered_shingles) / min(len(raw_shingles), len(rendered_shingles))
    return containment >= 0.55


def _shingles(value: str, width: int = 4) -> set[str]:
    if len(value) < width:
        return {value} if value else set()
    return {value[index : index + width] for index in range(len(value) - width + 1)}


def _json_text(value: object) -> str:
    parts: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            for key, child in node.items():
                parts.append(str(key))
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)
        elif node is not None:
            parts.append(str(node))

    visit(value)
    return _collapse_text(" ".join(parts))


def _docx_text(body: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(body)) as archive:
            xml = archive.read("word/document.xml").decode("utf-8", errors="replace")
    except (KeyError, OSError, zipfile.BadZipFile):
        return ""
    return _collapse_text(re.sub(r"<[^>]+>", " ", xml))


def _check_document_kind(kind: str, detected: str, text: str, reject) -> None:
    expected = unicodedata.normalize("NFKC", kind).strip().casefold()
    if not expected:
        return
    media_kind = _MEDIA_KIND_ALIASES.get(expected)
    if media_kind and media_kind != detected:
        reject(ContentGateError.DOCUMENT_KIND_MISMATCH, f"期望文档类型 {media_kind}，实际为 {detected}")
        return
    if expected in _GENERIC_ATTACHMENT_KINDS:
        if detected not in {"pdf", "doc", "docx", "xls", "xlsx", "zip", "rar", "7z"}:
            reject(ContentGateError.DOCUMENT_KIND_MISMATCH, "期望官方附件，但响应不是受支持的采购文件格式")
        return
    notice_terms = _NOTICE_KIND_TERMS.get(expected)
    if notice_terms and not any(_semantic_match(term, text) for term in notice_terms):
        reject(ContentGateError.DOCUMENT_KIND_MISMATCH, f"原始正文不含期望的公告类型：{kind}")


def _check_expected_semantics(item: ContentGateInput, text: str, reject) -> None:
    if item.expected_title and not _semantic_match(item.expected_title, text, fuzzy=True):
        reject(ContentGateError.TITLE_MISMATCH, "原始正文与预期公告标题不匹配")
    if item.expected_buyer and not _semantic_match(item.expected_buyer, text):
        reject(ContentGateError.BUYER_MISMATCH, "原始正文不含预期采购人")
    if item.expected_project_code and not _project_code_match(item.expected_project_code, text):
        reject(ContentGateError.PROJECT_CODE_MISMATCH, "原始正文不含预期项目编号")


def _semantic_normalize(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _semantic_match(expected: str, actual: str, fuzzy: bool = False) -> bool:
    needle = _semantic_normalize(expected)
    haystack = _semantic_normalize(actual)
    if not needle:
        return True
    if needle in haystack:
        return True
    if not fuzzy or len(needle) < 8:
        return False
    expected_shingles = _shingles(needle, width=2)
    actual_shingles = _shingles(haystack, width=2)
    if not expected_shingles:
        return False
    return len(expected_shingles & actual_shingles) / len(expected_shingles) >= 0.8


def _project_code_match(expected: str, actual: str) -> bool:
    # Project identifiers are frequently rendered with spaces/dashes changed.
    return _semantic_normalize(expected) in _semantic_normalize(actual)


__all__ = [
    "COMMERCIAL_AGGREGATOR_HOSTS",
    "DEFAULT_OFFICIAL_HOSTS",
    "ContentGateError",
    "ContentGateInput",
    "ContentGatePolicy",
    "ContentGateVerdict",
    "evaluate_content",
]
