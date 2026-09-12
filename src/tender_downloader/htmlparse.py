from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import unquote, urljoin, urlparse

from .models import AttachmentRef
from .utils import safe_component


ATTACHMENT_EXTENSIONS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".rar", ".7z", ".ofd", ".txt"
)


def attachment_url_error(url: str) -> str:
    """Reject placeholder hrefs without asserting that a real URL is reachable."""
    try:
        parsed = urlparse(url)
        basename = unquote(parsed.path).rstrip("/").rsplit("/", 1)[-1].strip()
    except ValueError:
        return "附件地址格式无效"
    if basename in {"附件下载", "点击下载", "点击此处下载", "下载附件", "下载", "点击查看", "查看附件"}:
        return "原页面把按钮文字写成了附件地址，已排除无效链接；请打开原公告核对"
    if parsed.scheme and parsed.scheme not in {"https", "http"}:
        return "附件地址不是 HTTP(S) 链接"
    return ""


class _DocumentParser(HTMLParser):
    def __init__(self, *, article_only: bool = False) -> None:
        super().__init__(convert_charrefs=True)
        self.article_only = article_only
        self.article_found = False
        self._article_depth = 0
        self.text: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.embedded: list[tuple[str, str]] = []
        self._skip_depth = 0
        self._current_href: str | None = None
        self._current_label: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        if self.article_only:
            if tag == "div":
                if self._article_depth:
                    self._article_depth += 1
                elif not self.article_found and "ewb-article" in (values.get("class") or "").split():
                    self.article_found = True
                    self._article_depth = 1
            if not self._article_depth:
                return
        if tag in ("script", "style", "noscript"):
            self._skip_depth += 1
        if tag == "a":
            href = (values.get("href") or "").strip()
            if href == "#" or href.lower().startswith("javascript:"):
                href = ""
            self._current_href = (
                href
                or values.get("data-url")
                or values.get("data-href")
                or values.get("data-download-url")
            )
            self._current_label = []
            if not self._current_href:
                onclick = values.get("onclick") or ""
                match = re.search(
                    r"[\"']([^\"']+(?:\.pdf|\.docx?|\.xlsx?|\.zip|download|filedown)[^\"']*)[\"']",
                    onclick,
                    flags=re.I,
                )
                if match:
                    self._current_href = match.group(1)
        elif tag in ("button", "input"):
            raw_url = (
                values.get("data-url")
                or values.get("data-href")
                or values.get("data-download-url")
            )
            if raw_url:
                self.links.append((raw_url, values.get("title") or values.get("value") or tag))
        if tag in ("iframe", "embed", "object"):
            raw_url = values.get("src") or values.get("data") or values.get("data-src")
            if raw_url:
                self.embedded.append((raw_url, values.get("title") or tag))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.article_only:
            if not self._article_depth:
                return
            if tag == "div":
                self._article_depth -= 1
        if tag in ("script", "style", "noscript") and self._skip_depth:
            self._skip_depth -= 1
        if tag == "a" and self._current_href:
            self.links.append((self._current_href, " ".join(self._current_label).strip()))
            self._current_href = None
            self._current_label = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth or (self.article_only and not self._article_depth):
            return
        value = re.sub(r"\s+", " ", data).strip()
        if value:
            self.text.append(value)
            if self._current_href is not None:
                self._current_label.append(value)


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    text: str
    attachments: tuple[AttachmentRef, ...]


def decode_html(data: bytes, content_type: str = "") -> str:
    charset_match = re.search(r"charset=([\w-]+)", content_type, flags=re.I)
    candidates = [charset_match.group(1)] if charset_match else []
    candidates.extend(["utf-8", "gb18030"])
    for charset in candidates:
        try:
            return data.decode(charset)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", errors="replace")


def parse_html_document(data: bytes, base_url: str, content_type: str = "") -> ParsedDocument:
    location = urlparse(base_url)
    scoped = location.hostname in {"www.jl.gov.cn", "jl.gov.cn"} and location.path.startswith("/ggzy/")
    parser = _DocumentParser(article_only=scoped)
    decoded = decode_html(data, content_type)
    parser.feed(decoded)
    if scoped and not parser.article_found:
        parser = _DocumentParser()
        parser.feed(decoded)
    attachments: list[AttachmentRef] = []
    seen: set[str] = set()
    for raw_url, label in (*parser.links, *parser.embedded):
        if not raw_url or raw_url.strip().startswith("#") or attachment_url_error(raw_url):
            continue
        absolute = urljoin(base_url, html.unescape(raw_url))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            continue
        if absolute.split("#", 1)[0] == base_url.split("#", 1)[0] and raw_url.strip() != base_url:
            continue
        lower_path = parsed.path.lower()
        lower_all = absolute.lower()
        label_lower = label.lower()
        looks_like_file = lower_path.endswith(ATTACHMENT_EXTENSIONS)
        looks_like_download = any(token in lower_all for token in ("download", "attachment", "filedown"))
        label_says_file = any(token in label_lower for token in ("附件", "下载", "采购文件", "招标文件"))
        label_has_extension = bool(re.search(
            r"\.(?:pdf|docx?|xlsx?|zip|rar|7z|ofd|txt)(?:\b|\s|[（(])",
            label_lower,
        ))
        if not (looks_like_file or looks_like_download or label_says_file or label_has_extension):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        suggested = safe_component(label) if label and len(label) <= 120 else None
        attachments.append(
            AttachmentRef(absolute, suggested, label, referer=base_url)
        )
    return ParsedDocument("\n".join(parser.text), tuple(attachments))
