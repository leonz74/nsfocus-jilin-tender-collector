from __future__ import annotations

import base64
import html
import json
import logging
import os
import re
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

from ..browser_cdp import BrowserCdpTransport, BrowserTransportError
from ..htmlparse import ATTACHMENT_EXTENSIONS, decode_html, parse_html_document
from ..http_client import FetchError, HttpResult, SourceBlocked
from ..models import AttachmentRef, Coverage, Notice, RawDocument
from ..utils import canonical_url_key, stable_id
from .base import SourceAdapter


_CREDENTIALS_ENV = "TENDER_SOURCE_CREDENTIALS_JSON"
_SESSIONS_ENV = "TENDER_SOURCE_SESSIONS_JSON"
_AUTH_MODES = {"none", "basic", "form", "browser"}
_NOTICE_WORDS = (
    "招标", "采购", "中标", "成交", "磋商", "谈判", "询价", "公告", "结果",
    "tender", "procurement", "bid", "award", "notice",
)
_DETAIL_URL_WORDS = ("detail", "article", "content", "notice")
_NAV_LABELS = {
    "首页", "上一页", "下一页", "末页", "更多", "返回", "登录", "注册", "退出",
    "联系我们", "网站地图", "政务公开", "信息公开",
}
_DETAIL_FIELDS = (
    "项目名称", "项目编号", "采购人", "招标人", "中标供应商", "成交供应商", "开标时间",
)
_AGGREGATE_LABELS = {
    "首页", "网站首页", "招标中心", "采购中心", "招采中心", "公告中心",
    "招标频道", "采购频道", "公告频道", "搜索", "搜索结果", "查询结果", "信息公开",
    "文件下载", "下载中心",
}
_LOGIN_FAILURE_WORDS = (
    "用户名或密码错误", "账号或密码错误", "密码不正确", "登录失败", "认证失败",
    "invalid password", "invalid username", "login failed", "authentication failed",
    "需要短信验证", "二次验证", "双因素认证", "two-factor", "2fa", "ca证书",
)
_STRONG_ACCESS_CHALLENGE_WORDS = (
    "请输入验证码", "访问验证", "人机验证", "滑动验证", "captcha",
    "verify you are human", "slide jigsaw to complete verification",
)
_AMBIGUOUS_ACCESS_CHALLENGE_WORDS = ("安全验证", "完成验证")

LOGGER = logging.getLogger(__name__)


def _origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise SourceBlocked("自定义来源包含无效 URL，需人工复核")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise SourceBlocked("自定义来源 URL 端口无效，需人工复核") from exc
    return parsed.scheme.lower(), parsed.hostname.lower(), port


def _without_fragment(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.headings: list[tuple[str, str]] = []
        self.links: list[tuple[str, str]] = []
        self.text_parts: list[str] = []
        self.non_link_text_parts: list[str] = []
        self._skip_depth = 0
        self._capture_title = False
        self._heading_tag: str | None = None
        self._heading_parts: list[str] = []
        self._href: str | None = None
        self._link_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        values = dict(attrs)
        if tag in {"script", "style", "noscript", "template"}:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._capture_title = True
        elif tag in {"h1", "h2"}:
            self._heading_tag = tag
            self._heading_parts = []
        elif tag == "a":
            href = (values.get("href") or values.get("data-url") or "").strip()
            if href and not href.lower().startswith(("javascript:", "mailto:", "tel:")):
                self._href = href
                self._link_parts = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style", "noscript", "template"}:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag == "title":
            self._capture_title = False
        elif self._heading_tag == tag:
            value = " ".join(self._heading_parts).strip()
            if value:
                self.headings.append((tag, value))
            self._heading_tag = None
            self._heading_parts = []
        elif tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._link_parts).strip()))
            self._href = None
            self._link_parts = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        self.text_parts.append(value)
        if self._href is None:
            self.non_link_text_parts.append(value)
        if self._capture_title:
            self.title_parts.append(value)
        if self._heading_tag:
            self._heading_parts.append(value)
        if self._href is not None:
            self._link_parts.append(value)

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts)

    @property
    def non_link_text(self) -> str:
        return "\n".join(self.non_link_text_parts)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()


class CustomWebSource(SourceAdapter):
    """用户配置的合法站点种子源，只做单层、同源公告发现。"""

    source_type = "custom_web"
    display_name = "自定义网站"

    def __init__(self, client, config: dict) -> None:
        # User-configured sites get their own CookieJar/curl cookie file.  This
        # prevents a login session or an overly broad Domain cookie from being
        # reused by an unrelated built-in or custom source.
        super().__init__(client.fork_session(), config)
        self.source_id = str(config.get("id", "")).strip()
        if not self.source_id:
            raise ValueError("custom_web 来源必须配置稳定的 id")
        start_urls = config.get("start_urls")
        if not isinstance(start_urls, list) or not start_urls or not all(
            isinstance(item, str) and item.strip() for item in start_urls
        ):
            raise ValueError(f"自定义来源 {self.source_id} 的 start_urls 必须是非空 URL 数组")
        self.start_urls = [_without_fragment(item.strip()) for item in start_urls]
        self._source_origins = {_origin(item) for item in self.start_urls}

        auth = config.get("auth", {"mode": "none"})
        if not isinstance(auth, dict):
            raise ValueError(f"自定义来源 {self.source_id} 的 auth 必须是对象")
        self.auth = dict(auth)
        self.auth_mode = str(auth.get("mode", "none")).strip().lower()
        if self.auth_mode not in _AUTH_MODES:
            raise ValueError(f"自定义来源 {self.source_id} 的 auth.mode 无效")
        if self.auth_mode != "none":
            if len(self._source_origins) != 1:
                raise ValueError(
                    f"需要登录的自定义来源 {self.source_id} 只能配置一个协议、主机和端口"
                )
            scheme, hostname, _ = next(iter(self._source_origins))
            local_test_origin = self.client.allow_private_hosts and hostname in {
                "127.0.0.1", "localhost", "::1",
            }
            if scheme != "https" and not local_test_origin:
                raise ValueError(
                    f"需要登录的自定义来源 {self.source_id} 必须使用 HTTPS，防止账号密码明文传输"
                )
        self._basic_header = ""
        self._prepared = False
        self.runtime_browser_session: dict | None = None
        self.runtime_credentials: dict | None = None
        required_terms = config.get("required_terms", [])
        if not isinstance(required_terms, list) or not all(
            isinstance(item, str) and item.strip() for item in required_terms
        ):
            raise ValueError(
                f"自定义来源 {self.source_id} 的 required_terms 必须是字符串数组"
            )
        self.required_terms = tuple(dict.fromkeys(
            item.strip().casefold() for item in required_terms
        ))
        self._rejection_reasons: dict[str, int] = {}
        configured_limit = config.get("max_discovered", 80)
        self.max_discovered = max(1, min(int(configured_limit), 200))

    def _load_credentials(self) -> tuple[str, str]:
        raw = (json.dumps({self.source_id: self.runtime_credentials})
               if self.runtime_credentials is not None else os.environ.get(_CREDENTIALS_ENV, ""))
        if not raw:
            raise SourceBlocked(
                f"来源 {self.name} 需要登录，但未提供运行时凭证；已阻止并等待人工复核"
            )
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceBlocked(
                f"来源 {self.name} 的运行时凭证格式无效；已阻止并等待人工复核"
            ) from exc
        record = payload.get(self.source_id) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            raise SourceBlocked(
                f"来源 {self.name} 缺少对应账号密码；已阻止并等待人工复核"
            )
        username = record.get("username")
        password = record.get("password")
        if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
            raise SourceBlocked(
                f"来源 {self.name} 的账号密码不完整；已阻止并等待人工复核"
            )
        return username, password

    def _load_browser_session(self) -> dict:
        raw = (json.dumps({self.source_id: self.runtime_browser_session})
               if self.runtime_browser_session is not None else os.environ.get(_SESSIONS_ENV, ""))
        if not raw:
            raise SourceBlocked(
                f"来源 {self.name} 尚未完成浏览器登录；请先打开登录窗口并保存会话"
            )
        if len(raw) > 2 * 1024 * 1024:
            raise SourceBlocked(f"来源 {self.name} 的浏览器会话数据过大；已安全阻止")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SourceBlocked(
                f"来源 {self.name} 的浏览器会话格式无效；请重新登录"
            ) from exc
        record = payload.get(self.source_id) if isinstance(payload, dict) else None
        if not isinstance(record, dict):
            raise SourceBlocked(
                f"来源 {self.name} 缺少已保存的浏览器会话；请重新登录"
            )
        captured_at = record.get("captured_at")
        if captured_at is not None:
            valid_capture_time = False
            if isinstance(captured_at, str) and 1 <= len(captured_at) <= 128:
                try:
                    datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
                    valid_capture_time = True
                except ValueError:
                    pass
            elif not isinstance(captured_at, bool) and isinstance(captured_at, (int, float)):
                numeric_capture_time = float(captured_at)
                valid_capture_time = (
                    numeric_capture_time >= 0
                    and numeric_capture_time <= 253_402_300_799_000
                    and numeric_capture_time not in {float("inf"), float("-inf")}
                    and numeric_capture_time == numeric_capture_time
                )
            if not valid_capture_time:
                raise SourceBlocked(
                    f"来源 {self.name} 的浏览器会话时间无效；请重新登录"
                )
        cookies = record.get("cookies")
        browser_transport = record.get("browser_transport")
        if not isinstance(cookies, list) and not isinstance(browser_transport, dict):
            raise SourceBlocked(
                f"来源 {self.name} 的浏览器会话不完整；请重新登录"
            )
        return record

    def prepare(self) -> None:
        if self._prepared:
            return
        if self.auth_mode == "none":
            self._prepared = True
            return

        if self.auth_mode == "browser":
            session = self._load_browser_session()
            seed = urlsplit(self.start_urls[0])
            origin = urlunsplit((seed.scheme, seed.netloc, "", "", ""))
            try:
                descriptor = session.get("browser_transport")
                if isinstance(descriptor, dict):
                    transport = BrowserCdpTransport.from_descriptor(
                        descriptor,
                        expected_origin=origin,
                        timeout_seconds=self.client.timeout_seconds,
                    )
                    self.client.attach_browser_transport(transport)
                else:
                    cookies = session.get("cookies")
                    if not isinstance(cookies, list):
                        raise ValueError("浏览器会话缺少 Cookie")
                    self.client.import_browser_cookies(origin, cookies)
                result = self.client.request(
                    self.start_urls[0],
                    headers=self._request_headers(self.start_urls[0]),
                    sensitive=True,
                )
            except SourceBlocked as exc:
                raise SourceBlocked(
                    f"来源 {self.name} 的浏览器登录会话已失效或无权访问；请重新登录"
                ) from exc
            except (BrowserTransportError, FetchError, ValueError) as exc:
                raise SourceBlocked(
                    f"来源 {self.name} 的浏览器登录会话无效；请重新登录"
                ) from exc
            page = decode_html(
                self._analysis_bytes(result),
                result.headers.get("content-type", ""),
            ).lower()
            visible_parser = _PageParser()
            visible_parser.feed(page)
            visible_text = visible_parser.text.lower()
            final_path = urlsplit(result.url).path.lower()
            configured_login = str(self.auth.get("login_url", "")).strip()
            returned_to_configured_login = bool(
                configured_login
                and _without_fragment(result.url).split("?", 1)[0].rstrip("/")
                == _without_fragment(configured_login).split("?", 1)[0].rstrip("/")
            )
            looks_like_login_route = bool(
                re.search(r"(?:^|[/_-])(?:login|signin|sign-in|auth)(?:$|[/_.-])", final_path)
            )
            has_password_form = bool(
                re.search(r"type\s*=\s*[\"']password[\"']", page, flags=re.I)
            )
            has_visible_login_form = has_password_form and any(
                marker in visible_text
                for marker in ("登录", "用户名", "账号", "sign in", "signin", "login")
            )
            if (
                # Login bundles often contain dormant error strings in script
                # source. Only visible page text is evidence of an actual login
                # failure; the raw HTML is retained solely for form detection.
                any(marker in visible_text for marker in _LOGIN_FAILURE_WORDS)
                or returned_to_configured_login
                or (looks_like_login_route and has_password_form)
                or has_visible_login_form
            ):
                raise SourceBlocked(
                    f"来源 {self.name} 的浏览器登录会话未通过入口验证；请重新登录"
                )
            self._prepared = True
            return

        username, password = self._load_credentials()
        if self.auth_mode == "basic":
            token = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
            self._basic_header = f"Basic {token}"
            self._prepared = True
            return

        login_url = str(self.auth.get("login_url", "")).strip()
        if not login_url:
            raise SourceBlocked(
                f"来源 {self.name} 未配置表单登录地址；已阻止并等待人工复核"
            )
        if _origin(login_url) not in self._source_origins:
            raise SourceBlocked(
                f"来源 {self.name} 的登录地址与采集入口不是同一来源；已安全阻止"
            )
        username_field = str(self.auth.get("username_field", "username")).strip()
        password_field = str(self.auth.get("password_field", "password")).strip()
        if not username_field or not password_field:
            raise SourceBlocked(
                f"来源 {self.name} 的登录字段名无效；已阻止并等待人工复核"
            )
        extra = self.auth.get("extra_fields", {})
        if not isinstance(extra, dict) or not all(
            isinstance(key, str) and key and isinstance(value, str)
            for key, value in extra.items()
        ):
            raise SourceBlocked(
                f"来源 {self.name} 的表单附加字段无效；已阻止并等待人工复核"
            )
        fields = dict(extra)
        fields[username_field] = username
        fields[password_field] = password
        try:
            result = self.client.post_form(login_url, fields, sensitive=True)
        except SourceBlocked as exc:
            raise SourceBlocked(
                f"来源 {self.name} 登录被拒绝或需要验证码/二次认证；已阻止并等待人工复核"
            ) from exc
        except FetchError as exc:
            raise SourceBlocked(
                f"来源 {self.name} 登录失败；已阻止并等待人工复核"
            ) from exc
        page = decode_html(
            self._analysis_bytes(result),
            result.headers.get("content-type", ""),
        ).lower()
        if any(marker in page for marker in _LOGIN_FAILURE_WORDS):
            raise SourceBlocked(
                f"来源 {self.name} 登录未通过或需要额外认证；已阻止并等待人工复核"
            )
        if (
            _origin(result.url) == _origin(login_url)
            and re.search(r"type\s*=\s*[\"']password[\"']", page, flags=re.I)
        ):
            raise SourceBlocked(
                f"来源 {self.name} 登录后仍停留在登录页；已阻止并等待人工复核"
            )
        self._prepared = True

    def close(self) -> None:
        self.client.close()

    def _request_headers(self, url: str) -> dict[str, str]:
        try:
            allowed = _origin(url) in self._source_origins
        except SourceBlocked:
            allowed = False
        if self.auth_mode == "basic" and allowed and self._basic_header:
            return {"Authorization": self._basic_header}
        if not allowed:
            # 防止共享 CookieJar 中的会话被带到来源范围之外。
            return {"Cookie": ""}
        return {}

    def fetch_detail(self, notice: Notice) -> RawDocument:
        if not self._prepared:
            self.prepare()
        result = self.client.request(notice.url, headers=self._request_headers(notice.url))
        return RawDocument(
            body=result.body,
            url=result.url,
            headers=result.headers,
            analysis_body=result.analysis_body,
        )

    def artifact_headers(self, artifact: AttachmentRef) -> dict[str, str]:
        headers = self._request_headers(artifact.url)
        if artifact.referer and _origin(artifact.referer) == _origin(artifact.url):
            headers["Referer"] = artifact.referer
        return headers

    @staticmethod
    def _analysis_bytes(document: HttpResult | RawDocument) -> bytes:
        return (
            document.analysis_body
            if document.analysis_body is not None
            else document.body
        )

    @staticmethod
    def _parse_page(result: HttpResult) -> _PageParser:
        parser = _PageParser()
        parser.feed(
            decode_html(
                CustomWebSource._analysis_bytes(result),
                result.headers.get("content-type", ""),
            )
        )
        return parser

    @staticmethod
    def _is_access_challenge(parser: _PageParser) -> bool:
        visible = parser.text.casefold()
        if any(marker in visible for marker in _STRONG_ACCESS_CHALLENGE_WORDS):
            return True
        if not any(marker in visible for marker in _AMBIGUOUS_ACCESS_CHALLENGE_WORDS):
            return False

        # “安全验证”也可能是网络安全标书中的正常项目用语。只有页面不具备
        # 公告正文形状时，才把这两个较宽泛的短语解释为访问挑战。
        primary = "\n".join((parser.title, *(value for _, value in parser.headings))).casefold()
        has_notice_word = any(word in f"{primary}\n{visible[:5000]}" for word in _NOTICE_WORDS)
        has_detail_field = any(field.casefold() in visible for field in _DETAIL_FIELDS)
        has_published_date = bool(re.search(
            r"(?:发布(?:时间|日期)|公告日期|中标日期|成交日期)\s*[:：]?\s*20\d{2}",
            visible,
        ))
        return not (has_notice_word and (has_detail_field or has_published_date))

    @staticmethod
    def _extract_date(text: str, headers: dict[str, str]) -> tuple[date | None, str]:
        preferred = re.search(
            r"(?:发布(?:时间|日期)|公告日期|中标日期|成交日期)\s*[:：]?\s*"
            r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?",
            text,
            flags=re.I,
        )
        generic = preferred or re.search(
            r"(?<!\d)(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})日?(?!\d)",
            text,
        )
        if generic:
            try:
                return date(*(int(generic.group(i)) for i in range(1, 4))), "page"
            except ValueError:
                pass
        last_modified = headers.get("last-modified", "")
        if last_modified:
            try:
                return parsedate_to_datetime(last_modified).date(), "last-modified"
            except (TypeError, ValueError, OverflowError):
                pass
        return None, "missing"

    @staticmethod
    def _title(parser: _PageParser, fallback: str) -> str:
        h1 = next((value for tag, value in parser.headings if tag == "h1"), "")
        h2 = next((value for tag, value in parser.headings if tag == "h2"), "")
        value = h1 or fallback.strip() or h2 or parser.title
        if not value:
            value = "自定义公告"
        return re.sub(r"\s+", " ", html.unescape(value)).strip()[:300]

    @staticmethod
    def _notice_type(title: str, text: str) -> str:
        sample = f"{title}\n{text[:1000]}"
        if "中标" in sample:
            return "中标公告"
        if "成交" in sample:
            return "成交公告"
        if "结果" in sample:
            return "结果公告"
        if "招标" in sample:
            return "招标公告"
        if "采购" in sample:
            return "采购公告"
        return "自定义公告"

    @staticmethod
    def _buyer(text: str) -> str:
        match = re.search(
            r"(?:采购人|采购单位|招标人)\s*[:：]\s*([^\n]{2,100})",
            text,
        )
        if not match:
            return ""
        # Some portals render the notice table from an escaped HTML fragment,
        # so a field value can contain literal ``</td><td ...>`` markup.  Strip
        # complete tags, stop before any truncated tag left by the bounded
        # capture, and then apply the normal next-field boundaries.
        value = html.unescape(match.group(1))
        value = re.sub(r"<[^>]*>", " ", value)
        value = value.split("<", 1)[0]
        value = re.sub(r"\s+", " ", value).strip()
        return re.split(
            r"\s{2,}|地址\s*[:：]|联系人\s*[:：]",
            value,
        )[0].strip()

    @staticmethod
    def _looks_like_listing_page(parser: _PageParser) -> bool:
        """Recognize explicit aggregate-page headings without matching notice titles.

        A detail such as ``某中心采购公告`` must remain a valid seed, while
        headings such as ``招标中心`` and ``采购公告列表`` are navigation pages.
        The first H1 is preferred because a detail page's ``<title>`` often ends in
        the site name (which itself may end in ``中心``).
        """
        h1 = next((value for tag, value in parser.headings if tag == "h1"), "")
        primary = h1 or parser.title
        if not primary:
            return False
        segments = [
            re.sub(r"\s+", "", value).lower()
            for value in re.split(r"\s*(?:\||[-_–—·])\s*", primary)
            if value.strip()
        ]
        if not segments:
            return False
        # A conventional detail title in the first segment takes precedence over
        # a trailing site name such as "某某交易中心".
        first = segments[0]
        first_is_detail = (
            any(word in first for word in _NOTICE_WORDS)
            and not first.endswith(("列表", "中心", "频道", "首页", "搜索结果", "查询结果"))
        )
        if first_is_detail:
            return False
        for value in segments:
            value = re.sub(r"[（(]\d+[）)]$", "", value)
            if value in _AGGREGATE_LABELS:
                return True
            if value.endswith(("列表", "中心", "频道", "首页")):
                return True
        return False

    @staticmethod
    def _is_strong_attachment(attachment: AttachmentRef) -> bool:
        """Require file identity in addition to a generic download label.

        Many procurement portals put navigation links such as ``文件下载`` or
        ``货物类招标文件`` on every page.  Conversely, real attachment APIs often
        have no filename extension.  Preserve those APIs when the route carries
        a file identifier in its query string or immediately after a file-like
        path segment.
        """

        parsed = urlsplit(attachment.url)
        path = unquote(parsed.path).lower()
        label = (attachment.label or "").lower()
        if path.endswith(ATTACHMENT_EXTENSIONS):
            return True
        if re.search(
            r"\.(?:pdf|docx?|xlsx?|zip|rar|7z|ofd|txt)(?:\b|\s|[（(])",
            label,
        ):
            return True

        route = re.search(
            r"(?:^|[/_.-])(?:download(?:file)?|attachment|filedown|getfile|files?|documents?)"
            r"(?:$|[/_.-])",
            path,
            flags=re.I,
        )
        if route is None:
            return False

        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            normalized_key = re.sub(r"[^a-z0-9]", "", key.casefold())
            normalized_value = value.strip()
            named_identity = (
                normalized_key in {
                    "id", "uuid", "guid", "token", "key", "code", "f",
                    "url", "path", "name", "filename",
                }
                or any(
                    marker in normalized_key
                    for marker in ("file", "attachment", "document", "download")
                )
            )
            opaque_identity = (
                len(normalized_value) >= 12
                and bool(re.search(r"[a-z]", normalized_value, flags=re.I))
                and bool(re.search(r"\d", normalized_value))
            )
            if normalized_value and (named_identity or opaque_identity):
                return True
        if parsed.query and re.fullmatch(r"[a-z0-9_-]{4,}", parsed.query, flags=re.I):
            return True

        suffix = path[route.end():].lstrip("/_.-").split("/", 1)[0]
        return bool(
            suffix
            and (
                suffix.isdigit()
                or bool(re.fullmatch(r"[a-f0-9-]{12,}", suffix, flags=re.I))
                or (
                    len(suffix) >= 6
                    and bool(re.search(r"[a-z]", suffix, flags=re.I))
                    and bool(re.search(r"\d", suffix))
                )
            )
        )

    @staticmethod
    def _has_strong_attachment(parser: _PageParser, result: HttpResult) -> bool:
        """Return true only when a link has file/download *URL* evidence.

        Generic portal navigation labels such as ``文件下载`` or
        ``货物类招标文件`` are deliberately insufficient on their own.
        """
        attachments = parse_html_document(
            CustomWebSource._analysis_bytes(result),
            result.url,
            result.headers.get("content-type", ""),
        ).attachments
        return any(CustomWebSource._is_strong_attachment(item) for item in attachments)

    def discover_artifacts(
        self, notice: Notice, detail: RawDocument
    ) -> list[AttachmentRef]:
        content_type = detail.headers.get("content-type", "")
        parsed = parse_html_document(
            self._analysis_bytes(detail),
            detail.url,
            content_type,
        )
        return [
            item for item in parsed.attachments
            if self._is_strong_attachment(item)
        ]

    @staticmethod
    def _looks_like_direct(parser: _PageParser, result: HttpResult) -> bool:
        title = parser.title.lower()
        text = parser.text[:5000].lower()
        has_notice_word = any(word in f"{title}\n{text}" for word in _NOTICE_WORDS)
        return CustomWebSource._has_strong_attachment(parser, result) or (
            has_notice_word and any(field in text for field in _DETAIL_FIELDS)
        )

    @staticmethod
    def _url_detail_signals(url: str) -> tuple[bool, bool]:
        parsed = urlsplit(url)
        sample = unquote(f"{parsed.path} {parsed.query}").lower()
        has_detail_word = any(word in sample for word in _DETAIL_URL_WORDS)
        has_identifier = bool(re.search(
            r"(?:\d{5,}|[a-f0-9]{12,})", sample, flags=re.I
        )) or bool(re.search(
            r"(?:^|&)(?:id|infoid|articleid|noticeid|contentid|uuid|code)="
            r"[^&\s]{3,}",
            parsed.query,
            flags=re.I,
        ))
        return has_detail_word, has_identifier

    @staticmethod
    def _candidate_links(parser: _PageParser, page_url: str) -> list[tuple[str, str]]:
        page_origin = _origin(page_url)
        listing_context = CustomWebSource._looks_like_listing_page(parser) or any(
            word in f"{parser.title}\n{parser.text[:1000]}" for word in _NOTICE_WORDS
        )
        result: list[tuple[str, str]] = []
        seen: set[str] = set()
        for raw_url, raw_label in parser.links:
            absolute = _without_fragment(urljoin(page_url, html.unescape(raw_url.strip())))
            if absolute == _without_fragment(page_url):
                continue
            try:
                if _origin(absolute) != page_origin:
                    continue
            except SourceBlocked:
                continue
            parsed = urlsplit(absolute)
            if parsed.username or parsed.password or parsed.path.lower().endswith(ATTACHMENT_EXTENSIONS):
                continue
            label = re.sub(r"\s+", " ", html.unescape(raw_label)).strip()
            if not label or label in _NAV_LABELS or len(label) > 300:
                continue
            label_sample = label.lower()
            url_has_detail_word, url_has_identifier = (
                CustomWebSource._url_detail_signals(absolute)
            )
            # Notice-like link text is context, not a detail shape.  This keeps
            # first-level columns such as "货物类招标文件" out while
            # retaining numeric IDs and explicit /notice/ or /detail/ routes.
            if not (url_has_detail_word or url_has_identifier):
                continue
            label_has_notice_word = any(word in label_sample for word in _NOTICE_WORDS)
            if not (url_has_detail_word or label_has_notice_word or listing_context):
                continue
            if not url_has_detail_word and len(label) < 4:
                continue
            key = canonical_url_key(absolute)
            if key in seen:
                continue
            seen.add(key)
            result.append((absolute, label))
        return result

    def _notice_from_result(
        self,
        result: HttpResult,
        *,
        label: str,
        start: date,
        end: date,
        explicit_seed: bool,
    ) -> Notice | None:
        parser = self._parse_page(result)
        if self._looks_like_listing_page(parser):
            self._record_rejection("聚合或导航页")
            return None
        published, date_source = self._extract_date(parser.text, result.headers)
        if published is None and label:
            published, date_source = self._extract_date(label, {})
        if published is None:
            if not explicit_seed:
                self._record_rejection("缺少可验证的发布时间")
                return None
            # 明确粘贴的公告页应被处理；同时标记日期为保守回退值，禁止伪装成已验证日期。
            published = end
            date_source = "configured-seed-fallback"
        if not start <= published <= end:
            self._record_rejection("页面日期不在配置范围")
            return None
        title = self._title(parser, label)
        if self.required_terms:
            # Province/site navigation is commonly repeated as anchor text on
            # every nationwide notice.  Do not let those global links satisfy
            # a region filter; the detail title, non-link body, and URL remain
            # searchable.
            searchable = unquote(
                f"{title}\n{parser.non_link_text}\n{result.url}"
            ).casefold()
            if not any(term in searchable for term in self.required_terms):
                visible = parser.text.casefold()
                if self._is_access_challenge(parser):
                    self._record_rejection("页面仍为验证码或访问验证页")
                elif any(marker in visible for marker in (
                    "我的办公室", "无权访问", "权限不足", "请先登录",
                )):
                    self._record_rejection("页面不是公告正文或当前账号无权访问")
                else:
                    self._record_rejection("页面未出现配置的筛选关键词")
                return None
        return Notice(
            source=self.name,
            authority_rank=self.authority_rank,
            external_id=f"{self.source_id}-{stable_id(canonical_url_key(result.url))}",
            title=title,
            published_at=published.isoformat(),
            url=result.url,
            region=str(self.config.get("region", "吉林省")),
            notice_type=self._notice_type(title, parser.text),
            buyer=self._buyer(parser.text),
            metadata={
                "custom_source_id": self.source_id,
                "auth_mode": self.auth_mode,
                "date_source": date_source,
            },
        )

    def _record_rejection(self, reason: str) -> None:
        self._rejection_reasons[reason] = self._rejection_reasons.get(reason, 0) + 1

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        if not self._prepared:
            self.prepare()
        self._rejection_reasons = {}
        seen: set[str] = set()
        dates: list[date] = []
        failed_links = 0
        discovered_count = 0
        challenged_seed_pages = 0

        for seed_url in self.start_urls:
            result = self.client.request(seed_url, headers=self._request_headers(seed_url))
            coverage.pages += 1
            parser = self._parse_page(result)
            if self._is_access_challenge(parser):
                challenged_seed_pages += 1
                self._record_rejection("页面仍为验证码或访问验证页")
                continue
            candidates = self._candidate_links(parser, result.url)
            looks_listing = self._looks_like_listing_page(parser)
            seed_is_direct = (
                any(self._url_detail_signals(result.url))
                or self._looks_like_direct(parser, result)
            )
            if not looks_listing and (
                not candidates
                or seed_is_direct
            ):
                notice = self._notice_from_result(
                    result, label="", start=start, end=end, explicit_seed=True
                )
                if notice and canonical_url_key(notice.url) not in seen:
                    seen.add(canonical_url_key(notice.url))
                    dates.append(date.fromisoformat(notice.published_at))
                    coverage.notices += 1
                    yield notice
            if seed_is_direct and not looks_listing:
                # Links on a detail seed are usually related notices or global
                # navigation, not members of a configured listing page.
                continue

            for url, label in candidates:
                if discovered_count >= self.max_discovered:
                    coverage.truncated = True
                    break
                key = canonical_url_key(url)
                if key in seen:
                    continue
                discovered_count += 1
                try:
                    detail = self.client.request(url, headers=self._request_headers(url))
                    coverage.pages += 1
                except SourceBlocked:
                    raise
                except FetchError:
                    failed_links += 1
                    continue
                notice = self._notice_from_result(
                    detail, label=label, start=start, end=end, explicit_seed=False
                )
                if notice is None:
                    continue
                final_key = canonical_url_key(notice.url)
                if final_key in seen:
                    continue
                seen.add(final_key)
                dates.append(date.fromisoformat(notice.published_at))
                coverage.notices += 1
                yield notice
            if coverage.truncated:
                break

        if challenged_seed_pages == len(self.start_urls):
            coverage.reached_start = False
            coverage.reached_end = False
            coverage.status = "blocked"
            coverage.blocked = 1
            coverage.message = (
                f"全部 {challenged_seed_pages} 个配置种子页仍为验证码或访问验证页；"
                "需要用户在登录浏览器中完成人工验证后重试"
            )
            LOGGER.warning("来源 %s 访问受阻：%s", self.name, coverage.message)
            return

        if dates:
            coverage.first_date = min(dates).isoformat()
            coverage.last_date = max(dates).isoformat()
        coverage.reached_start = False
        coverage.reached_end = False
        coverage.status = "partial"
        messages = ["自定义网站仅执行配置种子页及单层同源发现，不能证明全站完整覆盖"]
        if coverage.truncated:
            messages.append(f"发现链接达到安全上限 {self.max_discovered}")
        if failed_links:
            messages.append(f"{failed_links} 个候选链接读取失败")
        if self._rejection_reasons:
            reasons = "、".join(
                f"{reason} {count} 页"
                for reason, count in sorted(self._rejection_reasons.items())
            )
            messages.append(f"未识别为公告：{reasons}")
        coverage.message = "；".join(messages)
