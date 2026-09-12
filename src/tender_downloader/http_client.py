from __future__ import annotations

import email.message
import http.cookiejar
from http.client import HTTPException
import ipaddress
import json
import locale
import math
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin, urlparse
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

from .browser_cdp import BrowserCdpTransport, BrowserTransportError
from .utils import filename_from_url, safe_component, sha256_file


class FetchError(RuntimeError):
    pass


class SourceBlocked(FetchError):
    pass


class ArtifactRestricted(FetchError):
    pass


class _SafeRedirectHandler(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], None]) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.validator(newurl)
        old = urlparse(req.full_url)
        new = urlparse(newurl)
        old_origin = (old.scheme.lower(), (old.hostname or "").lower(), old.port)
        new_origin = (new.scheme.lower(), (new.hostname or "").lower(), new.port)
        if getattr(req, "_tender_sensitive", False) and old_origin != new_origin:
            raise SourceBlocked("敏感登录请求发生跨主机重定向，已安全阻止并等待人工复核")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None:
            setattr(
                redirected,
                "_tender_sensitive",
                bool(getattr(req, "_tender_sensitive", False)),
            )
            if old_origin != new_origin:
                # urllib 会复制原请求头；认证头或显式 Cookie 绝不能跨源。
                actual_names = {
                    name
                    for collection in (redirected.headers, redirected.unredirected_hdrs)
                    for name in collection
                    if name.lower() in SENSITIVE_HEADER_NAMES
                }
                for name in actual_names:
                    redirected.remove_header(name)
                # 空 Cookie 头是一个抑制标记，阻止共享 CookieJar 根据宽域
                # Domain 规则在新的主机上重新附加原来源登录会话。
                redirected.add_unredirected_header("Cookie", "")
        return redirected


@dataclass(frozen=True, slots=True)
class HttpResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    analysis_body: bytes | None = None


@dataclass(frozen=True, slots=True)
class DownloadResult:
    path: Path
    final_url: str
    original_name: str
    content_type: str
    sha256: str
    size: int
    original_name_raw: str = ""


BLOCK_MARKERS = (
    "您的访问过于频繁",
    "<title>频繁访问",
    "waf challenge",
    "access denied",
    "\"code\":829",
    "请输入验证码",
    "slide jigsaw to complete verification",
)

SENSITIVE_HEADER_NAMES = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
    "x-api_key",
    "x-goog-api-key",
    "api-key",
    "apikey",
    "x-auth-token",
    "x-access-token",
}

RFC2544_BENCHMARK_NETWORK = ipaddress.ip_network("198.18.0.0/15")


class HttpClient:
    def __init__(
        self,
        *,
        user_agent: str,
        timeout_seconds: float = 35,
        delay_seconds: float = 3,
        max_retries: int = 2,
        max_response_bytes: int = 25 * 1024 * 1024,
        max_download_bytes: int = 1024 * 1024 * 1024,
        download_timeout_seconds: float = 900,
        allow_private_hosts: bool = False,
        allow_benchmark_proxy_hosts: bool = False,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.delay_seconds = delay_seconds
        self.max_retries = max_retries
        self.max_response_bytes = max_response_bytes
        self.max_download_bytes = max_download_bytes
        self.download_timeout_seconds = download_timeout_seconds
        self.allow_private_hosts = allow_private_hosts
        self.allow_benchmark_proxy_hosts = allow_benchmark_proxy_hosts
        self.sleeper = sleeper
        self._last_request: dict[str, float] = {}
        self._cookie_jar = http.cookiejar.CookieJar()
        self._curl_domains: set[str] = set()
        self._opener = build_opener(
            _SafeRedirectHandler(self._validate_url),
            HTTPCookieProcessor(self._cookie_jar),
        )
        self._curl_cookie_dir = Path(tempfile.mkdtemp(prefix="jilin-tender-curl-"))
        self._curl_cookie_path = self._curl_cookie_dir / "cookies.txt"
        self._curl_cookie_path.touch()
        self._browser_transport: BrowserCdpTransport | None = None

    def fork_session(self) -> "HttpClient":
        """Create an isolated cookie/curl session with the same transport policy."""
        return type(self)(
            user_agent=self.user_agent,
            timeout_seconds=self.timeout_seconds,
            delay_seconds=self.delay_seconds,
            max_retries=self.max_retries,
            max_response_bytes=self.max_response_bytes,
            max_download_bytes=self.max_download_bytes,
            download_timeout_seconds=self.download_timeout_seconds,
            allow_private_hosts=self.allow_private_hosts,
            allow_benchmark_proxy_hosts=self.allow_benchmark_proxy_hosts,
            sleeper=self.sleeper,
        )

    def import_browser_cookies(
        self,
        origin: str,
        cookies: list[dict],
    ) -> int:
        """Import browser cookies into this isolated session, scoped to one origin.

        Playwright/CDP expose domain cookies using their original ``Domain``
        attribute.  A parent-domain cookie may legitimately authenticate the
        configured host, but retaining that scope would let it travel to sibling
        hosts.  Every accepted cookie is therefore narrowed to the exact origin
        hostname before it enters the CookieJar.

        Validation is atomic for matching cookies. Cookies for unrelated hosts
        (for example an SSO identity provider) are ignored; malformed cookies
        abort the import before anything is committed.
        """
        parsed = urlparse(origin)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        except ValueError as exc:
            raise ValueError("浏览器会话来源端口无效") from exc
        local_test_origin = self.allow_private_hosts and hostname in {
            "127.0.0.1", "localhost", "::1",
        }
        if (
            not hostname
            or parsed.username
            or parsed.password
            or (parsed.scheme.lower() != "https" and not local_test_origin)
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("浏览器会话必须绑定单一 HTTPS 来源（协议、主机和端口）")
        if not isinstance(cookies, list) or not 1 <= len(cookies) <= 500:
            raise ValueError("浏览器会话 cookies 必须包含 1-500 项")

        prepared: list[http.cookiejar.Cookie] = []
        now = time.time()
        for index, item in enumerate(cookies, start=1):
            if not isinstance(item, dict):
                raise ValueError(f"第 {index} 个浏览器 Cookie 必须是对象")
            name = item.get("name")
            value = item.get("value")
            domain = item.get("domain")
            path = item.get("path")
            secure = item.get("secure")
            # CDP omits ``expires`` after sanitisation for session cookies;
            # Playwright commonly represents the same state as -1.
            expires = item.get("expires", -1)
            if (
                not isinstance(name, str)
                or not 1 <= len(name) <= 256
                or not re.fullmatch(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+", name)
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 name 无效")
            if (
                not isinstance(value, str)
                or len(value) > 16_384
                or re.search(r"[\x00-\x1f\x7f;]", value)
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 value 无效")
            if not isinstance(domain, str) or not domain.strip():
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 domain 无效")
            cookie_domain = domain.strip().lower().rstrip(".")
            leading_dot = cookie_domain.startswith(".")
            if cookie_domain.startswith(".."):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 domain 无效")
            cookie_domain = cookie_domain[1:] if leading_dot else cookie_domain
            if (
                not cookie_domain
                or ":" in cookie_domain
                or "/" in cookie_domain
                or re.search(r"[\x00-\x20\x7f]", cookie_domain)
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 domain 无效")
            if (
                not isinstance(path, str)
                or not path.startswith("/")
                or len(path) > 2048
                or re.search(r"[\x00-\x1f\x7f]", path)
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 path 无效")
            if not isinstance(secure, bool):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 secure 必须是布尔值")
            if (
                isinstance(expires, bool)
                or not isinstance(expires, (int, float))
                or not math.isfinite(float(expires))
                or float(expires) < -1
                or float(expires) > 253_402_300_799
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 的 expires 无效")
            for http_only_key in ("httpOnly", "http_only"):
                if http_only_key in item and not isinstance(item[http_only_key], bool):
                    raise ValueError(
                        f"第 {index} 个浏览器 Cookie 的 {http_only_key} 必须是布尔值"
                    )
            belongs_to_origin = (
                hostname == cookie_domain or hostname.endswith(f".{cookie_domain}")
            )
            if not belongs_to_origin:
                # Storage.getCookies may include an identity-provider or analytics
                # domain visited during interactive login.  It is irrelevant to
                # the configured source and must never enter this CookieJar.
                continue
            if name.startswith("__Secure-") and not secure:
                raise ValueError(f"第 {index} 个浏览器 Cookie 违反 __Secure- 约束")
            if name.startswith("__Host-") and (
                not secure or path != "/" or leading_dot or cookie_domain != hostname
            ):
                raise ValueError(f"第 {index} 个浏览器 Cookie 违反 __Host- 约束")

            expires_value = None if float(expires) <= 0 else int(float(expires))
            if expires_value is not None and expires_value <= now:
                # An expired cookie is valid browser output, but it must not be
                # resurrected in the downloader session.
                continue
            prepared.append(http.cookiejar.Cookie(
                version=0,
                name=name,
                value=value,
                port=str(port),
                port_specified=True,
                domain=hostname,
                domain_specified=False,
                domain_initial_dot=False,
                path=path,
                path_specified=True,
                secure=secure,
                expires=expires_value,
                discard=expires_value is None,
                comment=None,
                comment_url=None,
                rest={"HttpOnly": None}
                if item.get("httpOnly", item.get("http_only", False))
                else {},
                rfc2109=False,
            ))

        if not prepared:
            raise ValueError("浏览器会话没有可用的未过期 Cookie")
        self._cookie_jar.clear()
        for cookie in prepared:
            self._cookie_jar.set_cookie(cookie)
        return len(prepared)

    def attach_browser_transport(self, transport: BrowserCdpTransport) -> None:
        """Route only the transport's exact origin through a live browser tab."""

        if not isinstance(transport, BrowserCdpTransport):
            raise TypeError("transport 必须是 BrowserCdpTransport")
        self._browser_transport = transport

    def close(self) -> None:
        if self._browser_transport is not None:
            self._browser_transport.close()
            self._browser_transport = None
        shutil.rmtree(self._curl_cookie_dir, ignore_errors=True)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass

    @staticmethod
    def _decode_process_error(value: bytes) -> str:
        for encoding in ("utf-8", locale.getpreferredencoding(False), "gb18030"):
            try:
                return value.decode(encoding)
            except (UnicodeDecodeError, LookupError):
                continue
        return value.decode("utf-8", errors="replace")

    def _read_response(self, response, *, limit: int) -> bytes:
        length = int(response.headers.get("Content-Length", "0") or 0)
        if length > limit:
            raise FetchError(f"响应超过大小上限: {length} > {limit}")
        body = response.read(limit + 1)
        if len(body) > limit:
            raise FetchError(f"响应超过大小上限: > {limit}")
        return body

    def _validate_url(self, url: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise FetchError(f"不允许的 URL: {url}")
        if parsed.username or parsed.password:
            raise FetchError("URL 不允许包含用户名或密码")
        if self.allow_private_hosts:
            return
        host = parsed.hostname.lower()
        try:
            literal_address = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            # libc/curl also accept legacy numeric IPv4 spellings such as a
            # single decimal integer or hexadecimal/octal components.  Treat
            # those as literals too, otherwise they could masquerade as a DNS
            # hostname and incorrectly receive the RFC 2544 exception.
            try:
                literal_address = ipaddress.ip_address(socket.inet_aton(host))
            except OSError:
                literal_address = None
        if literal_address is not None:
            if not literal_address.is_global:
                raise FetchError(f"拒绝访问非公网 IP 地址: {literal_address}")
            return
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(host, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except socket.gaierror as exc:
            raise FetchError(f"域名解析失败: {host}: {exc}") from exc
        if not addresses:
            raise FetchError(f"域名没有可用地址: {host}")
        for value in addresses:
            address = ipaddress.ip_address(value.split("%", 1)[0])
            if address.is_global:
                continue
            benchmark_proxy = (
                self.allow_benchmark_proxy_hosts
                and isinstance(address, ipaddress.IPv4Address)
                and address in RFC2544_BENCHMARK_NETWORK
            )
            if not benchmark_proxy:
                raise FetchError(f"拒绝访问非公网地址: {host} -> {address}")

    @staticmethod
    def _domain_key(url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        parts = host.split(".")
        if len(parts) >= 3 and parts[-2:] == ["gov", "cn"]:
            return ".".join(parts[-3:])
        return ".".join(parts[-2:]) if len(parts) >= 2 else host

    def _throttle(self, url: str) -> None:
        host = urlparse(url).netloc.lower()
        last = self._last_request.get(host)
        if last is not None:
            remaining = self.delay_seconds - (time.monotonic() - last)
            if remaining > 0:
                self.sleeper(remaining)
        self._last_request[host] = time.monotonic()

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if extra:
            headers.update(extra)
        return headers

    @staticmethod
    def _has_sensitive_headers(headers: dict[str, str] | None) -> bool:
        return any(
            str(key).lower() in SENSITIVE_HEADER_NAMES and bool(str(value))
            for key, value in (headers or {}).items()
        )

    def _cookie_header(self, url: str) -> str:
        host = (urlparse(url).hostname or "").lower()
        values: list[str] = []
        for cookie in self._cookie_jar:
            domain = cookie.domain.lstrip(".").lower()
            if host == domain or host.endswith(f".{domain}"):
                values.append(f"{cookie.name}={cookie.value}")
        return "; ".join(values)

    @staticmethod
    def _check_blocked(status: int, body: bytes) -> None:
        if status in (401, 403, 429):
            raise SourceBlocked(f"HTTP {status}")
        preview = body[:8192].decode("utf-8", errors="ignore").lower()
        if any(marker.lower() in preview for marker in BLOCK_MARKERS):
            raise SourceBlocked("站点返回验证码、限频或 WAF 页面")

    def request(
        self,
        url: str,
        *,
        method: str = "GET",
        data: bytes | None = None,
        headers: dict[str, str] | None = None,
        sensitive: bool = False,
    ) -> HttpResult:
        self._validate_url(url)
        if self._browser_transport is not None and self._browser_transport.handles(url):
            if method.upper() != "GET" or data is not None:
                raise FetchError("已登录浏览器传输当前只允许 GET 请求")
            # Network.loadNetworkResource does not expose arbitrary request-header
            # injection.  Credentials and the normal browser referrer policy are
            # taken from the attached page; secrets never enter a command line.
            disallowed = {
                str(key).lower()
                for key, value in (headers or {}).items()
                if value and str(key).lower() not in {"referer", "accept"}
            }
            if disallowed:
                raise FetchError("已登录浏览器传输不允许注入自定义请求头")
            self._throttle(url)
            try:
                resource = self._browser_transport.request(
                    url, max_bytes=self.max_response_bytes
                )
            except BrowserTransportError as exc:
                raise FetchError(str(exc)) from exc
            self._check_blocked(resource.status, resource.body)
            if resource.status >= 400:
                raise FetchError(f"浏览器 HTTP {resource.status}: {url}")
            return HttpResult(
                url=resource.url,
                status=resource.status,
                headers=resource.headers,
                body=resource.body,
                analysis_body=resource.analysis_body,
            )
        has_sensitive_header = self._has_sensitive_headers(headers)
        has_session_cookie = bool(self._cookie_header(url))
        has_sensitive_auth = has_sensitive_header or has_session_cookie
        if self._domain_key(url) in self._curl_domains and not has_sensitive_auth:
            self._throttle(url)
            try:
                return self._curl_request(url, method=method, data=data, headers=headers)
            except SourceBlocked:
                raise
            except FetchError:
                # A remembered transport is a hint, not a permanent choice.
                # Retry through Python's verified TLS stack if curl stops working.
                self._curl_domains.discard(self._domain_key(url))
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._throttle(url)
            request = Request(url, data=data, method=method, headers=self._headers(headers))
            setattr(request, "_tender_sensitive", sensitive)
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = self._read_response(response, limit=self.max_response_bytes)
                    status = int(getattr(response, "status", 200))
                    self._check_blocked(status, body)
                    if status >= 500:
                        raise FetchError(f"HTTP {status}")
                    return HttpResult(
                        url=response.geturl(),
                        status=status,
                        headers={key.lower(): value for key, value in response.headers.items()},
                        body=body,
                    )
            except HTTPError as exc:
                body = exc.read(8192)
                self._check_blocked(exc.code, body)
                last_error = FetchError(f"HTTP {exc.code}: {url}")
                if exc.code < 500:
                    break
            except SourceBlocked:
                raise
            except (URLError, TimeoutError, OSError, HTTPException, FetchError) as exc:
                last_error = exc
            if attempt < self.max_retries:
                self.sleeper(min(2 ** attempt, 10))
        if last_error and "ssl" in str(last_error).lower():
            has_sensitive_auth = has_sensitive_header or bool(self._cookie_header(url))
            if has_sensitive_auth:
                # curl receives -H values through its process arguments. Never put
                # an API/login credential or session cookie in the process list.
                raise FetchError(
                    "携带认证信息的 HTTPS 请求失败，已禁止使用可能暴露凭证的 curl 后备"
                ) from last_error
            return self._curl_request(url, method=method, data=data, headers=headers)
        raise FetchError(str(last_error or f"请求失败: {url}"))

    def _curl_request(
        self,
        url: str,
        *,
        method: str,
        data: bytes | None,
        headers: dict[str, str] | None,
    ) -> HttpResult:
        """Windows/OpenSSL 与部分政务站 TLS 不兼容时，使用系统 curl 正常访问。

        仅作为同一公开 URL 的传输后备，不改变频率控制，也不处理验证码/WAF。
        """
        if self._has_sensitive_headers(headers) or self._cookie_header(url):
            raise FetchError("携带敏感请求头时禁止使用 curl 后备")
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise FetchError(f"TLS 请求失败且未找到 curl: {url}")
        with tempfile.TemporaryDirectory() as directory:
            header_path = Path(directory) / "headers.txt"
            command = [
                curl, "-sS", "--max-redirs", "0", "--max-time", str(int(self.timeout_seconds)),
                "--max-filesize", str(self.max_response_bytes),
                "-b", str(self._curl_cookie_path), "-c", str(self._curl_cookie_path),
                "-A", self.user_agent, "-D", str(header_path),
                "-w", "\n__CURL_META__%{http_code}\t%{url_effective}",
            ]
            merged_headers = self._headers(headers)
            merged_headers.pop("User-Agent", None)
            if "Cookie" not in merged_headers:
                cookie_value = self._cookie_header(url)
                if cookie_value:
                    merged_headers["Cookie"] = cookie_value
            for key, value in merged_headers.items():
                command.extend(["-H", f"{key}: {value}"])
            if method != "GET":
                command.extend(["-X", method])
            if data is not None:
                command.extend(["--data-binary", "@-"])
            command.append(url)
            completed = subprocess.run(
                command,
                input=data,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                shell=False,
            )
            if completed.returncode != 0:
                raise FetchError(
                    f"curl 请求失败({completed.returncode}): "
                    f"{self._decode_process_error(completed.stderr)[:500]}"
                )
            marker = b"\n__CURL_META__"
            if marker not in completed.stdout:
                raise FetchError("curl 响应缺少状态元数据")
            body, meta = completed.stdout.rsplit(marker, 1)
            if len(body) > self.max_response_bytes:
                raise FetchError(f"curl 响应超过大小上限: > {self.max_response_bytes}")
            status_text, final_url = meta.decode("utf-8", errors="replace").split("\t", 1)
            status = int(status_text)
            raw_headers = header_path.read_text(encoding="iso-8859-1", errors="replace")
            blocks = [block for block in re.split(r"\r?\n\r?\n", raw_headers) if block.startswith("HTTP/")]
            final_headers: dict[str, str] = {}
            if blocks:
                for line in blocks[-1].splitlines()[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        final_headers[key.strip().lower()] = value.strip()
            self._check_blocked(status, body)
            if 300 <= status < 400:
                raise FetchError("curl TLS 后备不跟随重定向；请由普通 HTTP 客户端安全重试")
            if status >= 400:
                raise FetchError(f"curl HTTP {status}: {url}")
            self._curl_domains.add(self._domain_key(final_url.strip() or url))
            return HttpResult(final_url.strip(), status, final_headers, body)

    @staticmethod
    def _parse_curl_headers(path: Path) -> dict[str, str]:
        raw_headers = path.read_text(encoding="iso-8859-1", errors="replace")
        blocks = [
            block for block in re.split(r"\r?\n\r?\n", raw_headers)
            if block.startswith("HTTP/")
        ]
        result: dict[str, str] = {}
        if blocks:
            for line in blocks[-1].splitlines()[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    result[key.strip().lower()] = value.strip()
        return result

    def _curl_download(
        self,
        url: str,
        part_path: Path,
        *,
        suggested_name: str | None,
        headers: dict[str, str] | None,
    ) -> DownloadResult:
        """TLS 后备下载；逐跳验证重定向，完整重下而不拼接不可信分片。"""
        if self._has_sensitive_headers(headers) or self._cookie_header(url):
            raise FetchError("携带敏感请求头时禁止使用 curl 附件后备")
        curl = shutil.which("curl.exe") or shutil.which("curl")
        if not curl:
            raise FetchError(f"附件 TLS 失败且未找到 curl: {url}")
        current_url = url
        self._validate_url(current_url)
        part_path.unlink(missing_ok=True)
        meta_path = Path(f"{part_path}.json")
        meta_path.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory() as directory:
            header_path = Path(directory) / "headers.txt"
            final_headers: dict[str, str] = {}
            for _ in range(6):
                header_path.unlink(missing_ok=True)
                command = [
                    curl, "-sS", "--max-redirs", "0",
                    "--connect-timeout", str(int(self.timeout_seconds)),
                    "--max-time", str(int(self.download_timeout_seconds)),
                    "--max-filesize", str(self.max_download_bytes),
                    "-A", self.user_agent,
                    "-b", str(self._curl_cookie_path),
                    "-c", str(self._curl_cookie_path),
                    "-D", str(header_path),
                    "-o", str(part_path),
                    "-w", "__CURL_META__%{http_code}\t%{url_effective}",
                ]
                merged_headers = self._headers(headers)
                merged_headers.pop("User-Agent", None)
                cookie_value = self._cookie_header(current_url)
                if cookie_value and "Cookie" not in merged_headers:
                    merged_headers["Cookie"] = cookie_value
                for key, value in merged_headers.items():
                    command.extend(["-H", f"{key}: {value}"])
                command.append(current_url)
                completed = subprocess.run(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                    shell=False,
                )
                if completed.returncode != 0:
                    part_path.unlink(missing_ok=True)
                    raise FetchError(
                        f"curl 附件下载失败({completed.returncode}): "
                        f"{self._decode_process_error(completed.stderr)[:500]}"
                    )
                marker = b"__CURL_META__"
                if marker not in completed.stdout:
                    part_path.unlink(missing_ok=True)
                    raise FetchError("curl 附件响应缺少状态元数据")
                meta = completed.stdout.rsplit(marker, 1)[1].decode(
                    "utf-8", errors="replace"
                )
                status_text, effective_url = meta.split("\t", 1)
                status = int(status_text)
                final_headers = self._parse_curl_headers(header_path)
                if 300 <= status < 400:
                    location = final_headers.get("location", "")
                    if not location:
                        raise FetchError(f"附件重定向缺少 Location: HTTP {status}")
                    redirected_url = urljoin(current_url, location)
                    # curl is invoked once per hop, so its own redirect handler
                    # cannot enforce our URL policy.  Validate before the next
                    # invocation to block non-HTTP schemes, credentials and
                    # private/reserved destinations before any request occurs.
                    self._validate_url(redirected_url)
                    current_url = redirected_url
                    continue
                preview = b""
                if part_path.exists():
                    with part_path.open("rb") as handle:
                        preview = handle.read(8192)
                if status in (401, 403):
                    raise ArtifactRestricted(
                        f"curl 附件下载 HTTP {status}: {current_url}"
                    )
                if status == 429:
                    raise SourceBlocked(f"curl 附件下载 HTTP 429: {current_url}")
                self._check_blocked(status, preview)
                if status >= 400:
                    raise FetchError(f"curl 附件 HTTP {status}: {current_url}")
                final_url = effective_url.strip() or current_url
                break
            else:
                part_path.unlink(missing_ok=True)
                raise FetchError("附件重定向次数超过 5 次")

        if not part_path.exists() or part_path.stat().st_size > self.max_download_bytes:
            part_path.unlink(missing_ok=True)
            raise FetchError("curl 附件不存在或超过大小上限")
        disposition_name = self._content_disposition_filename(
            final_headers.get("content-disposition", "")
        )
        url_name = filename_from_url(final_url)
        suggested_has_extension = bool(suggested_name and Path(suggested_name).suffix)
        original_name_raw = (
            disposition_name
            or (suggested_name if suggested_has_extension else None)
            or (url_name if Path(url_name).suffix else None)
            or suggested_name
            or url_name
        )
        with part_path.open("rb") as handle:
            preview = handle.read(16)
        if not Path(original_name_raw).suffix:
            if preview.startswith(b"%PDF-"):
                original_name_raw += ".pdf"
            elif preview.startswith(b"PK\x03\x04"):
                original_name_raw += ".zip"
            elif preview.startswith(b"\xd0\xcf\x11\xe0"):
                original_name_raw += ".doc"
        self._curl_domains.add(self._domain_key(final_url))
        return DownloadResult(
            path=part_path,
            final_url=final_url,
            original_name=safe_component(original_name_raw, max_length=80),
            content_type=final_headers.get("content-type", "").split(";", 1)[0],
            sha256=sha256_file(part_path),
            size=part_path.stat().st_size,
            original_name_raw=original_name_raw,
        )

    def post_form(
        self,
        url: str,
        fields: dict[str, str],
        headers: dict[str, str] | None = None,
        *,
        sensitive: bool = False,
    ) -> HttpResult:
        body = urlencode(fields).encode("utf-8")
        merged = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            merged.update(headers)
        return self.request(
            url,
            method="POST",
            data=body,
            headers=merged,
            sensitive=sensitive,
        )

    def post_json(self, url: str, payload: dict, headers: dict[str, str] | None = None) -> HttpResult:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        merged = {"Content-Type": "application/json", "Accept": "application/json"}
        if headers:
            merged.update(headers)
        return self.request(url, method="POST", data=body, headers=merged)

    @staticmethod
    def _content_disposition_filename(value: str) -> str | None:
        if not value:
            return None
        message = email.message.Message()
        message["content-disposition"] = value
        filename = message.get_filename()
        return str(filename).strip() if filename else None

    def download(
        self,
        url: str,
        part_path: Path,
        suggested_name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> DownloadResult:
        self._validate_url(url)
        has_sensitive_header = self._has_sensitive_headers(headers)
        has_session_cookie = bool(self._cookie_header(url))
        has_sensitive_auth = has_sensitive_header or has_session_cookie
        part_path.parent.mkdir(parents=True, exist_ok=True)
        if self._browser_transport is not None and self._browser_transport.handles(url):
            self._throttle(url)
            try:
                resource = self._browser_transport.download_to(
                    url, part_path, max_bytes=self.max_download_bytes
                )
            except BrowserTransportError as exc:
                part_path.unlink(missing_ok=True)
                raise FetchError(str(exc)) from exc
            with part_path.open("rb") as handle:
                preview = handle.read(8192)
            if resource.status in (401, 403):
                part_path.unlink(missing_ok=True)
                raise ArtifactRestricted(
                    f"浏览器附件下载 HTTP {resource.status}: {url}"
                )
            if resource.status == 429:
                part_path.unlink(missing_ok=True)
                raise SourceBlocked(f"浏览器附件下载 HTTP 429: {url}")
            try:
                self._check_blocked(resource.status, preview)
            except SourceBlocked:
                part_path.unlink(missing_ok=True)
                raise
            if resource.status >= 400:
                part_path.unlink(missing_ok=True)
                raise FetchError(f"浏览器附件 HTTP {resource.status}: {url}")
            disposition_name = self._content_disposition_filename(
                resource.headers.get("content-disposition", "")
            )
            url_name = filename_from_url(resource.url)
            suggested_has_extension = bool(
                suggested_name and Path(suggested_name).suffix
            )
            original_name_raw = (
                disposition_name
                or (suggested_name if suggested_has_extension else None)
                or (url_name if Path(url_name).suffix else None)
                or suggested_name
                or url_name
            )
            if not Path(original_name_raw).suffix:
                preview_lower = preview.lstrip()[:16].lower()
                if preview.startswith(b"%PDF-"):
                    original_name_raw += ".pdf"
                elif preview.startswith(b"PK\x03\x04"):
                    original_name_raw += ".zip"
                elif preview.startswith(b"\xd0\xcf\x11\xe0"):
                    original_name_raw += ".doc"
                elif preview_lower.startswith((b"<!doctype html", b"<html")):
                    original_name_raw += ".html"
            return DownloadResult(
                path=part_path,
                final_url=resource.url,
                original_name=safe_component(original_name_raw, max_length=80),
                content_type=resource.headers.get("content-type", "").split(";", 1)[0],
                sha256=sha256_file(part_path),
                size=part_path.stat().st_size,
                original_name_raw=original_name_raw,
            )
        if self._domain_key(url) in self._curl_domains and not has_sensitive_auth:
            self._throttle(url)
            try:
                result = self._curl_download(
                    url, part_path, suggested_name=suggested_name, headers=headers,
                )
                self._curl_domains.add(self._domain_key(result.final_url))
                return result
            except (SourceBlocked, ArtifactRestricted):
                raise
            except FetchError:
                self._curl_domains.discard(self._domain_key(url))
        meta_path = Path(f"{part_path}.json")
        part_meta: dict[str, str] = {}
        if meta_path.exists():
            try:
                part_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                part_meta = {}
        if part_meta and part_meta.get("url") != url:
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            part_meta = {}
        if part_path.exists() and (
            not part_meta
            or not (part_meta.get("etag") or part_meta.get("last_modified"))
        ):
            # 无法证明旧分片与当前对象同一版本时，安全重下，禁止拼接。
            part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            part_meta = {}
        existing = part_path.stat().st_size if part_path.exists() else 0
        extra_headers: dict[str, str] = dict(headers or {})
        if existing:
            extra_headers["Range"] = f"bytes={existing}-"
            if part_meta.get("etag"):
                extra_headers["If-Range"] = part_meta["etag"]
            elif part_meta.get("last_modified"):
                extra_headers["If-Range"] = part_meta["last_modified"]

        self._throttle(url)
        request = Request(url, headers=self._headers(extra_headers))
        try:
            response = self._opener.open(request, timeout=self.timeout_seconds)
        except HTTPError as exc:
            if exc.code == 416 and existing:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                return self.download(
                    url,
                    part_path,
                    suggested_name=suggested_name,
                    headers=headers,
                )
            if exc.code in (401, 403):
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise ArtifactRestricted(f"附件下载 HTTP {exc.code}: {url}") from exc
            if exc.code == 429:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise SourceBlocked(f"附件下载 HTTP 429: {url}") from exc
            raise FetchError(f"附件下载 HTTP {exc.code}: {url}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            message = str(exc).lower()
            if "ssl" in message or "tls" in message or "unexpected_eof" in message:
                has_sensitive_auth = has_sensitive_header or bool(self._cookie_header(url))
                if has_sensitive_auth:
                    raise FetchError(
                        "携带认证信息的附件请求失败，已禁止使用可能暴露凭证的 curl 后备"
                    ) from exc
                return self._curl_download(
                    url,
                    part_path,
                    suggested_name=suggested_name,
                    headers=headers,
                )
            raise FetchError(f"附件下载失败: {url}: {exc}") from exc

        with response:
            status = int(getattr(response, "status", 200))
            headers = {key.lower(): value for key, value in response.headers.items()}
            if status in (401, 403):
                raise ArtifactRestricted(f"附件下载 HTTP {status}: {url}")
            if status == 429:
                raise SourceBlocked(f"附件下载 HTTP 429: {url}")
            append = status == 206 and existing > 0
            if append:
                content_range = headers.get("content-range", "")
                if not content_range.startswith(f"bytes {existing}-"):
                    raise FetchError(f"断点响应范围不正确: {content_range}")
            current_meta = {
                "url": url,
                "etag": headers.get("etag", ""),
                "last_modified": headers.get("last-modified", ""),
                "final_url": response.geturl(),
            }
            meta_tmp = Path(f"{meta_path}.tmp")
            meta_tmp.write_text(json.dumps(current_meta, ensure_ascii=False), encoding="utf-8")
            os.replace(meta_tmp, meta_path)
            mode = "ab" if append else "wb"
            expected = int(headers.get("content-length", "0") or 0)
            base_size = existing if append else 0
            if expected and base_size + expected > self.max_download_bytes:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise FetchError(
                    f"附件超过大小上限: {base_size + expected} > {self.max_download_bytes}"
                )
            written = 0
            with part_path.open(mode) as handle:
                while chunk := response.read(1024 * 1024):
                    handle.write(chunk)
                    written += len(chunk)
                    if base_size + written > self.max_download_bytes:
                        handle.close()
                        part_path.unlink(missing_ok=True)
                        meta_path.unlink(missing_ok=True)
                        raise FetchError(
                            f"附件超过大小上限: > {self.max_download_bytes}"
                        )
                handle.flush()
            if expected and written != expected:
                raise FetchError(f"附件长度不完整: expected={expected}, actual={written}")
            if append:
                total_match = re.search(r"/(\d+)$", headers.get("content-range", ""))
                if total_match and part_path.stat().st_size != int(total_match.group(1)):
                    raise FetchError(
                        f"断点完成后的总长度不正确: expected={total_match.group(1)}, "
                        f"actual={part_path.stat().st_size}"
                    )
            preview = b""
            with part_path.open("rb") as handle:
                preview = handle.read(8192)
            try:
                self._check_blocked(status, preview)
            except SourceBlocked:
                part_path.unlink(missing_ok=True)
                meta_path.unlink(missing_ok=True)
                raise

            final_url = response.geturl()
            disposition_name = self._content_disposition_filename(
                headers.get("content-disposition", "")
            )
            url_name = filename_from_url(final_url)
            suggested_has_extension = bool(
                suggested_name and Path(suggested_name).suffix.lower() in {
                    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip",
                    ".rar", ".7z", ".ofd", ".txt", ".html", ".htm",
                }
            )
            original_name_raw = (
                disposition_name
                or (suggested_name if suggested_has_extension else None)
                or (url_name if Path(url_name).suffix else None)
                or suggested_name
                or url_name
            )
            extension = ""
            if not Path(original_name_raw).suffix:
                preview_lower = preview.lstrip()[:16].lower()
                if preview.startswith(b"%PDF-"):
                    extension = ".pdf"
                elif preview.startswith(b"PK\x03\x04"):
                    extension = ".zip"
                elif preview.startswith(b"\xd0\xcf\x11\xe0"):
                    extension = ".doc"
                elif preview_lower.startswith((b"<!doctype html", b"<html")):
                    extension = ".html"
            original_name_raw = f"{original_name_raw}{extension}"
            original_name = safe_component(original_name_raw, max_length=80)
            digest = sha256_file(part_path)
            meta_path.unlink(missing_ok=True)
            return DownloadResult(
                path=part_path,
                final_url=final_url,
                original_name=original_name,
                content_type=headers.get("content-type", "").split(";", 1)[0],
                sha256=digest,
                size=part_path.stat().st_size,
                original_name_raw=original_name_raw,
            )
