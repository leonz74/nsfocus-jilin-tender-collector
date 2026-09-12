from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit


MAX_COOKIE_COUNT = 500
MAX_COOKIE_VALUE_LENGTH = 8_192
MAX_SESSION_ENV_BYTES = 16_000
PROFILE_PREFIX = "tender-browser-login-"
PENDING_CLEANUP_MARKER = ".tender-cleanup-pending"
PERSISTENT_PROFILE_MARKER = ".tender-profile-v1"
PERSISTENT_PROFILE_VERSION = "v1"
PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
CLEANUP_ERROR = (
    "临时浏览器数据清理失败；请关闭登录窗口后重试，"
    "并删除系统临时目录中的 tender-browser-login-* 目录"
)
_PROFILE_PROCESS_ENV = "TENDER_BROWSER_PROFILE_PATH"
_WINDOWS_PROFILE_ARGUMENT_PATTERN = (
    r'(?i)(?:^|\s)(?:'
    r'"--user-data-dir=([^"]+)"|'
    r'--user-data-dir="([^"]+)"|'
    r'--user-data-dir=([^\s"]+)|'
    r'(?:"--user-data-dir"|--user-data-dir)\s+(?:"([^"]+)"|([^\s"]+))'
    r')(?=$|\s)'
)
_WINDOWS_PROFILE_TERMINATOR = (r"""
$ErrorActionPreference = 'Stop'
$target = [IO.Path]::GetFullPath($env:TENDER_BROWSER_PROFILE_PATH).TrimEnd('\')
$argumentPattern = '__TENDER_PROFILE_ARGUMENT_PATTERN__'

function Find-ToolBrowserProcesses {
    Get-CimInstance -ClassName Win32_Process -Filter "Name='msedge.exe' OR Name='chrome.exe' OR Name='chromium.exe' OR Name='google-chrome.exe'" |
        Where-Object {
            $line = [string]$_.CommandLine
            if (-not $line) { return $false }
            foreach ($argument in [regex]::Matches($line, $argumentPattern)) {
                $candidate = $null
                for ($groupIndex = 1; $groupIndex -le 5; $groupIndex++) {
                    if ($argument.Groups[$groupIndex].Success) {
                        $candidate = $argument.Groups[$groupIndex].Value
                        break
                    }
                }
                if (-not $candidate) { continue }
                try {
                    $candidate = [IO.Path]::GetFullPath($candidate).TrimEnd('\')
                } catch {
                    continue
                }
                if ([string]::Equals(
                    $candidate,
                    $target,
                    [StringComparison]::OrdinalIgnoreCase
                )) {
                    return $true
                }
            }
            return $false
        }
}

 # Require several consecutive empty scans. Browser.close can tear down CDP
 # before the last profile-owning Edge process has fully exited.
$emptyRounds = 0
for ($round = 0; $round -lt 10; $round++) {
    $profileProcesses = @(Find-ToolBrowserProcesses)
    if ($profileProcesses.Count -eq 0) {
        $emptyRounds++
        if ($emptyRounds -ge 4) { exit 0 }
    } else {
        $emptyRounds = 0
        foreach ($profileProcess in $profileProcesses) {
            try {
                Invoke-CimMethod -InputObject $profileProcess -MethodName Terminate |
                    Out-Null
            } catch {
                # A parent browser process may already have closed this child.
            }
        }
    }
    Start-Sleep -Milliseconds 150
}

exit 3
""").replace(
    "__TENDER_PROFILE_ARGUMENT_PATTERN__",
    _WINDOWS_PROFILE_ARGUMENT_PATTERN,
)


class BrowserLoginError(RuntimeError):
    """A user-facing failure in the temporary browser login flow."""


class BrowserLoginBusyError(BrowserLoginError):
    """Raised when another visual login is already in progress."""


class BrowserProcess(Protocol):
    def poll(self) -> int | None: ...

    def terminate(self) -> None: ...

    def wait(self, timeout: float | None = None) -> int: ...

    def kill(self) -> None: ...


@dataclass
class BrowserHandle:
    process: BrowserProcess
    profile_dir: Path
    websocket_url: str
    # Production Chromium handles support authenticated resource loading over
    # CDP.  The default remains false for legacy/test launchers that only yield
    # cookies and do not expose a reusable endpoint.
    live_transport: bool = False


@dataclass
class _Session:
    source_id: str
    login_url: str
    profile_id: str = ""
    check_url: str = ""
    state: str = "starting"
    started_at: str = field(default_factory=lambda: _now())
    completed_at: str | None = None
    error: str = ""
    cookies: list[dict[str, Any]] = field(default_factory=list)
    handle: BrowserHandle | None = None
    profile_dir: Path | None = None
    probe_worker_running: bool = field(default=False, repr=False)
    # Used by the UI/checkpoint flow to resume a pending run without requiring
    # a second "I have logged in" click.  It never contains credentials.
    ready_event: threading.Event = field(default_factory=threading.Event, repr=False)


Launcher = Callable[[str, Path], BrowserHandle]
CookieReader = Callable[[BrowserHandle], list[dict[str, Any]]]
PageProbe = Callable[[BrowserHandle, str], str]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _validate_login_url(url: str) -> str:
    if not isinstance(url, str):
        raise BrowserLoginError("登录地址必须是字符串")
    value = url.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise BrowserLoginError("登录地址格式无效") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise BrowserLoginError("登录地址必须是有效的 HTTP(S) 地址")
    if parsed.username or parsed.password:
        raise BrowserLoginError("登录地址不能包含账号或密码")
    if port is not None and not 1 <= port <= 65_535:
        raise BrowserLoginError("登录地址端口无效")
    return value


def _login_origin(url: str) -> tuple[str, str, int]:
    value = _validate_login_url(url)
    parsed = urlsplit(value)
    return (
        parsed.scheme.lower(),
        (parsed.hostname or "").lower().rstrip("."),
        parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
    )


def _origin_text(url: str) -> str:
    scheme, host, port = _login_origin(url)
    default_port = 443 if scheme == "https" else 80
    host_text = f"[{host}]" if ":" in host else host
    return f"{scheme}://{host_text}" + (f":{port}" if port != default_port else "")


def _persistent_profile_root() -> Path:
    """Return the application-owned browser-profile root.

    On Windows this is deliberately rooted below LOCALAPPDATA.  No source name,
    hostname, cookie or CDP capability is included in the on-disk path.
    """

    local_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_data:
        base = Path(local_data)
    elif os.name == "nt":
        base = Path.home() / "AppData" / "Local"
    else:
        xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
        base = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    return (base / "TenderDownloader" / "profiles" / PERSISTENT_PROFILE_VERSION).resolve()


def _profile_digest(source_id: str, login_url: str) -> str:
    # Keep the serialized key name for byte-for-byte compatibility with the
    # original source-id based profile layout.  New callers may pass a stable
    # profile id here; legacy callers still produce the exact same digest.
    material = json.dumps(
        {"source_id": source_id, "origin": _origin_text(login_url)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _prepare_persistent_profile(root: Path, source_id: str, login_url: str) -> Path:
    """Create/reuse exactly one opaque profile for a source and exact origin."""

    digest = _profile_digest(source_id, login_url)
    try:
        resolved_root = root.resolve()
        resolved_root.mkdir(parents=True, exist_ok=True)
        profile = (resolved_root / digest).resolve()
    except OSError as exc:
        raise BrowserLoginError("无法创建持久浏览器登录目录") from exc
    if profile.parent != resolved_root or profile.name != digest:
        raise BrowserLoginError("持久浏览器登录目录校验失败")
    marker = profile / PERSISTENT_PROFILE_MARKER
    expected_marker = f"TenderDownloader browser profile {PERSISTENT_PROFILE_VERSION}\n{digest}\n"
    try:
        if profile.exists() and not profile.is_dir():
            raise BrowserLoginError("持久浏览器登录路径不是目录")
        profile.mkdir(exist_ok=True)
        if marker.exists():
            if marker.read_text(encoding="ascii") != expected_marker:
                raise BrowserLoginError("持久浏览器登录目录归属校验失败")
        else:
            # Refuse to adopt an unrelated non-empty directory that happens to
            # have a 64-character name.
            if any(profile.iterdir()):
                raise BrowserLoginError("持久浏览器登录目录缺少归属标记")
            marker.write_text(expected_marker, encoding="ascii")
    except BrowserLoginError:
        raise
    except OSError as exc:
        raise BrowserLoginError("无法初始化持久浏览器登录目录") from exc
    return profile


def _profile_has_browser_data(profile: Path) -> bool:
    """Return whether Chromium has written anything beyond our ownership marker."""

    try:
        return any(item.name != PERSISTENT_PROFILE_MARKER for item in profile.iterdir())
    except OSError as exc:
        raise BrowserLoginError("无法检查持久浏览器登录目录") from exc


def _validate_profile_id(profile_id: str) -> str:
    if not isinstance(profile_id, str):
        raise BrowserLoginError("浏览器登录配置标识无效")
    value = profile_id.strip()
    if not PROFILE_ID_PATTERN.fullmatch(value):
        raise BrowserLoginError("浏览器登录配置标识无效")
    return value


def _validate_check_url(check_url: str, login_url: str) -> str:
    value = _validate_login_url(check_url)
    if _login_origin(value) != _login_origin(login_url):
        raise BrowserLoginError("登录检查地址必须与登录地址使用同一来源")
    return value


def _browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    for command in ("msedge.exe", "chrome.exe", "msedge", "google-chrome", "chromium"):
        found = shutil.which(command)
        if found:
            candidates.append(Path(found))
    if sys.platform == "darwin":
        for root in (Path("/Applications"), Path.home() / "Applications"):
            for app, executable in (
                ("Google Chrome", "Google Chrome"),
                ("Microsoft Edge", "Microsoft Edge"),
                ("Chromium", "Chromium"),
            ):
                candidates.append(root / f"{app}.app" / "Contents" / "MacOS" / executable)
    locations = (
        ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
        ("LOCALAPPDATA", "Microsoft/Edge/Application/msedge.exe"),
        ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
        ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
        ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
    )
    for variable, suffix in locations:
        root = os.environ.get(variable)
        if root:
            candidates.append(Path(root) / Path(suffix))
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key not in seen and candidate.is_file():
            seen.add(key)
            result.append(candidate)
    return result


def _read_debug_endpoint(
    profile_dir: Path,
    process: BrowserProcess,
    *,
    timeout: float = 12.0,
) -> str:
    active_port = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout
    last_error = ""
    while time.monotonic() < deadline:
        try:
            lines = active_port.read_text(encoding="utf-8").splitlines()
            if len(lines) >= 2:
                port = int(lines[0].strip())
                path = lines[1].strip()
                if not 1 <= port <= 65_535 or not path.startswith("/devtools/browser/"):
                    raise ValueError("invalid DevToolsActivePort")
                websocket_url = f"ws://127.0.0.1:{port}{path}"
                if _cdp_endpoint_alive(websocket_url, timeout=0.25):
                    return websocket_url
                last_error = "浏览器调试接口尚未就绪"
        except (OSError, UnicodeError, ValueError) as exc:
            last_error = str(exc)
        return_code = process.poll()
        if return_code not in {None, 0}:
            detail = f"（{last_error}）" if last_error else ""
            raise BrowserLoginError(f"登录窗口启动失败或已提前关闭{detail}")
        time.sleep(0.05)
    detail = f"（{last_error}）" if last_error else ""
    if process.poll() is not None:
        raise BrowserLoginError(f"登录窗口启动失败或已提前关闭{detail}")
    raise BrowserLoginError(f"等待浏览器调试端口超时{detail}")


def launch_temporary_browser(login_url: str, profile_dir: Path) -> BrowserHandle:
    """Launch Edge/Chrome with an isolated profile and loopback-only CDP."""

    candidates = _browser_candidates()
    if not candidates:
        raise BrowserLoginError("未找到 Microsoft Edge 或 Google Chrome")
    executable = candidates[0]
    command = [
        str(executable),
        "--remote-debugging-port=0",
        "--remote-debugging-address=127.0.0.1",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-sync",
        "--new-window",
        login_url,
    ]
    kwargs: dict[str, Any] = {
        "args": command,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "shell": False,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(**kwargs)
    try:
        websocket_url = _read_debug_endpoint(profile_dir, process)
    except Exception:
        _stop_process(process, profile_dir=profile_dir)
        raise
    return BrowserHandle(
        process=process,
        profile_dir=profile_dir,
        websocket_url=websocket_url,
        live_transport=True,
    )


class _WebSocket:
    """Tiny RFC 6455 client sufficient for a local Chrome DevTools session."""

    def __init__(self, url: str, *, timeout: float = 8.0) -> None:
        parsed = urlsplit(url)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise BrowserLoginError("浏览器调试地址不是本机 WebSocket")
        try:
            port = parsed.port
        except ValueError as exc:
            raise BrowserLoginError("浏览器调试端口无效") from exc
        if port is None or not 1 <= port <= 65_535:
            raise BrowserLoginError("浏览器调试端口无效")
        self._socket = socket.create_connection((parsed.hostname, port), timeout=timeout)
        self._socket.settimeout(timeout)
        self._buffer = bytearray()
        key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
        path = parsed.path or "/"
        if parsed.query:
            path += f"?{parsed.query}"
        host = f"{parsed.hostname}:{port}"
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        ).encode("ascii")
        self._socket.sendall(request)
        response = self._read_headers()
        lines = response.decode("iso-8859-1", errors="replace").split("\r\n")
        if not lines or " 101 " not in f" {lines[0]} ":
            self.close()
            raise BrowserLoginError("无法连接浏览器调试接口")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()
        expected = base64.b64encode(
            hashlib.sha1(
                (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
            ).digest()
        ).decode("ascii")
        if not secrets.compare_digest(headers.get("sec-websocket-accept", ""), expected):
            self.close()
            raise BrowserLoginError("浏览器调试接口握手校验失败")

    def _read_headers(self) -> bytes:
        marker = b"\r\n\r\n"
        while marker not in self._buffer:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise BrowserLoginError("浏览器调试连接已关闭")
            self._buffer.extend(chunk)
            if len(self._buffer) > 64 * 1024:
                raise BrowserLoginError("浏览器调试握手响应过大")
        index = self._buffer.index(marker) + len(marker)
        result = bytes(self._buffer[:index])
        del self._buffer[:index]
        return result

    def _recv_exact(self, length: int) -> bytes:
        while len(self._buffer) < length:
            chunk = self._socket.recv(max(4096, length - len(self._buffer)))
            if not chunk:
                raise BrowserLoginError("浏览器调试连接已关闭")
            self._buffer.extend(chunk)
        result = bytes(self._buffer[:length])
        del self._buffer[:length]
        return result

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        first = 0x80 | opcode
        length = len(payload)
        mask = secrets.token_bytes(4)
        if length < 126:
            header = bytes((first, 0x80 | length))
        elif length <= 0xFFFF:
            header = bytes((first, 0x80 | 126)) + length.to_bytes(2, "big")
        else:
            header = bytes((first, 0x80 | 127)) + length.to_bytes(8, "big")
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self._socket.sendall(header + mask + masked)

    def send_json(self, payload: dict[str, Any]) -> None:
        self._send_frame(
            0x1,
            json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        )

    def receive_json(self) -> dict[str, Any]:
        fragments = bytearray()
        message_opcode: int | None = None
        while True:
            first, second = self._recv_exact(2)
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(self._recv_exact(2), "big")
            elif length == 127:
                length = int.from_bytes(self._recv_exact(8), "big")
            if length > 4 * 1024 * 1024:
                raise BrowserLoginError("浏览器调试响应过大")
            mask = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 0x8:
                raise BrowserLoginError("浏览器调试连接已关闭")
            if opcode == 0x9:
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            if opcode in {0x1, 0x2}:
                message_opcode = opcode
                fragments = bytearray(payload)
            elif opcode == 0x0 and message_opcode is not None:
                fragments.extend(payload)
            else:
                continue
            if not final:
                continue
            if message_opcode != 0x1:
                fragments.clear()
                message_opcode = None
                continue
            try:
                decoded = json.loads(bytes(fragments).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise BrowserLoginError("浏览器调试响应格式无效") from exc
            if not isinstance(decoded, dict):
                raise BrowserLoginError("浏览器调试响应不是 JSON 对象")
            return decoded

    def close(self) -> None:
        try:
            self._socket.close()
        except OSError:
            pass


def _cdp_command(
    websocket: _WebSocket,
    command_id: int,
    method: str,
    *,
    params: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    request: dict[str, Any] = {
        "id": command_id,
        "method": method,
        "params": params or {},
    }
    if session_id:
        request["sessionId"] = session_id
    websocket.send_json(request)
    while True:
        response = websocket.receive_json()
        if response.get("id") != command_id:
            continue
        if "error" in response:
            error = response.get("error")
            message = error.get("message", "") if isinstance(error, dict) else str(error)
            raise BrowserLoginError(f"浏览器读取登录状态失败：{message or 'CDP 错误'}")
        result = response.get("result", {})
        if not isinstance(result, dict):
            raise BrowserLoginError("浏览器登录状态响应无效")
        return result


_PAGE_LOGIN_STATE_EXPRESSION = r"""(() => {
  const visible = (node) => {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    const box = node.getBoundingClientRect();
    return style.visibility !== 'hidden' && style.display !== 'none'
      && box.width > 0 && box.height > 0;
  };
  const titleText = String(document.title || '').slice(0, 1000);
  const frameText = [...document.querySelectorAll('iframe')].filter(visible).map(frame => {
    try { return frame.contentDocument?.body?.innerText || ''; } catch { return ''; }
  }).join('\n');
  const bodyText = (String(document.body?.innerText || '') + '\n' + frameText).slice(0, 200000);
  // Some portals render a graphical challenge with no visible body text and
  // put the only unambiguous signal in <title> (for example “请输入验证码”).
  // Reading the title into this local boolean test avoids declaring that page
  // ready while still returning no page text to the application.
  const challengeText = /验证码|访问验证|安全验证|人机验证|请完成(?:以下)?验证|拖动[^\n]{0,20}滑块|点击[^\n]{0,20}验证|captcha|verify you are human|checking your browser|security check|just a moment|slide jigsaw to complete verification/i.test(`${titleText}\n${bodyText}`);
  const challengeElement = Array.from(document.querySelectorAll(
    'iframe[src*="captcha" i], iframe[src*="verify" i], [id*="captcha" i], [class*="captcha" i]'
  )).some(visible);
  const passwordInput = Array.from(document.querySelectorAll('input[type="password"]')).some(visible);
  const loginForm = Array.from(document.querySelectorAll('form')).some((form) => {
    if (!visible(form)) return false;
    const text = String(form.innerText || '').slice(0, 5000);
    return /登录|登陆|sign\s*in|log\s*in/i.test(text)
      && Boolean(form.querySelector('input, button'));
  });
  const loginControl = Array.from(document.querySelectorAll('a, button')).some((node) => {
    if (!visible(node)) return false;
    const text = String(node.innerText || node.getAttribute('aria-label') || '').trim();
    return /^(?:登录|登陆|会员登录|sign\s*in|log\s*in)$/i.test(text);
  });
  return {
    challenge: Boolean(challengeText || challengeElement),
    login: Boolean(passwordInput || loginForm || loginControl),
    visible: document.visibilityState === 'visible',
    focused: document.hasFocus(),
    ready: document.readyState === 'complete'
  };
})()"""


def _same_origin_page_target(target: Any, login_url: str) -> bool:
    if not isinstance(target, dict) or target.get("type") != "page":
        return False
    target_url = target.get("url", "")
    if not isinstance(target_url, str):
        return False
    try:
        return _login_origin(target_url) == _login_origin(login_url)
    except BrowserLoginError:
        return False


def _exact_page_target(target: Any, check_url: str) -> bool:
    """Match one configured check page, ignoring only its fragment.

    Authentication readiness is about the page the crawler will actually read,
    not an unrelated authenticated dashboard tab on the same origin.  Origin
    validation remains centralized in ``_same_origin_page_target``; path and
    query are then compared exactly so a login/home tab cannot mask a CAPTCHA
    on the configured seed page.
    """

    if not _same_origin_page_target(target, check_url):
        return False
    target_url = target.get("url", "")
    try:
        target_parts = urlsplit(target_url)
        check_parts = urlsplit(check_url)
    except (TypeError, ValueError):
        return False
    return (
        (target_parts.path or "/") == (check_parts.path or "/")
        and target_parts.query == check_parts.query
    )


def probe_browser_page(handle: BrowserHandle, check_url: str) -> str:
    """Return challenge/login/content/unknown without reading page contents out.

    The expression returns only five booleans.  It never solves, fills or submits
    a CAPTCHA and never exposes rendered text to the local web UI.
    """

    if not handle.live_transport:
        return "unknown"
    websocket: _WebSocket | None = None
    try:
        websocket = _WebSocket(handle.websocket_url, timeout=1.0)
        target_result = _cdp_command(websocket, 1, "Target.getTargets")
        targets = target_result.get("targetInfos", [])
        same_origin_candidates = [
            item for item in targets
            if _same_origin_page_target(item, check_url)
        ] if isinstance(targets, list) else []
        # The exact configured seed/check page is authoritative.  Preserve the
        # legacy same-origin fallback only while that page has not been opened
        # yet (for example during an interactive login redirect).
        exact_candidates = [
            item for item in same_origin_candidates
            if _exact_page_target(item, check_url)
        ]
        candidates = exact_candidates or same_origin_candidates
        if not candidates:
            return "unknown"
        best_score = -1
        best_state = "unknown"
        command_id = 2
        for candidate in candidates:
            target_id = candidate.get("targetId", "")
            if not isinstance(target_id, str) or not target_id:
                continue
            attached_session = ""
            try:
                attached = _cdp_command(
                    websocket,
                    command_id,
                    "Target.attachToTarget",
                    params={"targetId": target_id, "flatten": True},
                )
                command_id += 1
                attached_session = attached.get("sessionId", "")
                if not isinstance(attached_session, str) or not attached_session:
                    continue
                evaluated = _cdp_command(
                    websocket,
                    command_id,
                    "Runtime.evaluate",
                    params={
                        "expression": _PAGE_LOGIN_STATE_EXPRESSION,
                        "returnByValue": True,
                        "awaitPromise": False,
                    },
                    session_id=attached_session,
                )
                command_id += 1
                remote = evaluated.get("result", {})
                value = remote.get("value", {}) if isinstance(remote, dict) else {}
                if not isinstance(value, dict):
                    continue
                challenge = value.get("challenge")
                login = value.get("login")
                if (
                    not isinstance(challenge, bool)
                    or not isinstance(login, bool)
                    or value.get("ready") is not True
                ):
                    continue
                state = "challenge" if challenge else "login" if login else "content"
                activity_score = (
                    100 if value.get("focused") is True
                    else 50 if value.get("visible") is True
                    else 0
                )
                state_score = {"login": 1, "challenge": 2, "content": 3}[state]
                score = activity_score + state_score
                if score > best_score:
                    best_score = score
                    best_state = state
            except (BrowserLoginError, OSError):
                continue
            finally:
                if attached_session:
                    try:
                        _cdp_command(
                            websocket,
                            command_id,
                            "Target.detachFromTarget",
                            params={"sessionId": attached_session},
                        )
                        command_id += 1
                    except (BrowserLoginError, OSError):
                        pass
        return best_state
    except (BrowserLoginError, OSError):
        return "unknown"
    finally:
        if websocket is not None:
            websocket.close()


def focus_browser_page(handle: BrowserHandle, login_url: str) -> bool:
    """Bring the isolated source's current login/SSO page forward safely."""

    if not _browser_handle_alive(handle):
        return False
    websocket: _WebSocket | None = None
    try:
        websocket = _WebSocket(handle.websocket_url, timeout=1.0)
        target_result = _cdp_command(websocket, 1, "Target.getTargets")
        targets = target_result.get("targetInfos", [])
        page_targets = [
            item for item in targets
            if isinstance(item, dict) and item.get("type") == "page"
        ] if isinstance(targets, list) else []
        if page_targets:
            # During SSO or a CAPTCHA the foreground tab may temporarily be on
            # another origin. Activate that existing page instead of navigating
            # or refreshing the configured portal. The isolated profile belongs
            # to one source, and Chromium returns the newest page last.
            target_id = page_targets[-1].get("targetId", "")
        else:
            created = _cdp_command(
                websocket,
                2,
                "Target.createTarget",
                params={"url": login_url},
            )
            target_id = created.get("targetId", "")
        if not isinstance(target_id, str) or not target_id:
            return False
        _cdp_command(
            websocket,
            3,
            "Target.activateTarget",
            params={"targetId": target_id},
        )
        return True
    except (BrowserLoginError, OSError):
        return False
    finally:
        if websocket is not None:
            websocket.close()


def _evaluate_in_source_page(
    handle: BrowserHandle, page_url: str, expression: str, *, all_pages: bool = False
) -> dict[str, Any]:
    """Run one awaited expression in the source's own page; values only out.

    With ``all_pages`` the expression runs in every matching page (a stale
    challenge tab must not outvote the freshly answered one) and the last
    object verdict wins.
    """

    if not handle.live_transport:
        return {}
    websocket: _WebSocket | None = None
    try:
        # The awaited expression performs its own network round trips (several
        # seconds for a challenge answer), so the read budget must exceed a
        # plain command turnaround.
        websocket = _WebSocket(handle.websocket_url, timeout=20.0)
        targets = _cdp_command(websocket, 1, "Target.getTargets").get("targetInfos", [])
        candidates = [
            item for item in targets
            if isinstance(item, dict) and item.get("type") == "page"
            and _same_origin_page_target(item, page_url)
        ]
        exact = [item for item in candidates if _exact_page_target(item, page_url)]
        verdict: dict[str, Any] = {}
        for item in (exact or candidates):
            target_id = item.get("targetId", "")
            if not isinstance(target_id, str) or not target_id:
                continue
            attached = _cdp_command(
                websocket, 2, "Target.attachToTarget",
                params={"targetId": target_id, "flatten": True},
            )
            session_id = attached.get("sessionId", "")
            if not session_id:
                continue
            try:
                evaluated = _cdp_command(
                    websocket, 3, "Runtime.evaluate",
                    params={
                        "expression": expression,
                        "returnByValue": True,
                        "awaitPromise": True,
                    },
                    session_id=session_id,
                )
                remote = evaluated.get("result", {})
                value = remote.get("value", {}) if isinstance(remote, dict) else {}
                if isinstance(value, dict) and value:
                    verdict = value
                    if not all_pages:
                        return verdict
            finally:
                try:
                    _cdp_command(
                        websocket, 4, "Target.detachFromTarget",
                        params={"sessionId": session_id},
                    )
                except (BrowserLoginError, OSError):
                    pass
        return verdict
    except (BrowserLoginError, OSError):
        return {}
    finally:
        if websocket is not None:
            websocket.close()


def _fresh_request_probe(check_url: str) -> str:
    """Expression that judges session health by a fresh same-origin request,
    immune to stale challenge markup still rendered in old tabs."""

    target = json.dumps(check_url)
    return (
        "(async () => {"
        f"  const r = await fetch({target}, {{cache: 'no-store'}});"
        "  const t = await r.text();"
        "  const challenged = t.includes('captchaPage') || t.includes('id=\"infoString\"');"
        "  return {healthy: r.ok && !challenged};"
        "})()"
    )


def _cdp_endpoint_alive(websocket_url: str, *, timeout: float = 0.5) -> bool:
    """Return whether a loopback browser-level CDP WebSocket is accepting clients."""

    websocket: _WebSocket | None = None
    try:
        websocket = _WebSocket(websocket_url, timeout=timeout)
        return True
    except (BrowserLoginError, OSError):
        return False
    finally:
        if websocket is not None:
            websocket.close()


def _browser_handle_alive(handle: BrowserHandle) -> bool:
    # On Windows, Edge/Chrome may use the Popen process only as a launcher and
    # return 0 after handing the profile to another browser process.  Preserve
    # the cheap/legacy process check while using the CDP endpoint to distinguish
    # that successful hand-off from a genuinely closed window.
    return handle.process.poll() is None or _cdp_endpoint_alive(handle.websocket_url)


def _handle_matches_profile(handle: BrowserHandle, profile_dir: Path) -> bool:
    """Validate launcher output before retaining a privileged local handle."""

    try:
        if handle.profile_dir.resolve() != profile_dir.resolve():
            return False
        parsed = urlsplit(handle.websocket_url)
        port = parsed.port
    except (AttributeError, OSError, ValueError):
        return False
    return (
        parsed.scheme == "ws"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and parsed.username is None
        and parsed.password is None
        and port is not None
        and 1 <= port <= 65_535
        and parsed.path.startswith("/devtools/browser/")
    )


def _close_browser_endpoint(websocket_url: str, *, timeout: float = 3.0) -> bool:
    """Ask the real browser process to exit and wait for its CDP endpoint to stop."""

    websocket: _WebSocket | None = None
    try:
        websocket = _WebSocket(websocket_url, timeout=min(timeout, 1.0))
        try:
            _cdp_command(websocket, 1, "Browser.close")
        except (BrowserLoginError, OSError):
            # Chrome is allowed to tear down the WebSocket while handling
            # Browser.close.  Endpoint disappearance below is authoritative.
            pass
    except (BrowserLoginError, OSError):
        return False
    finally:
        if websocket is not None:
            websocket.close()

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _cdp_endpoint_alive(websocket_url, timeout=0.2):
            return True
        time.sleep(0.05)
    return not _cdp_endpoint_alive(websocket_url, timeout=0.2)


def _resolve_tool_profile(
    path: Path,
    *,
    persistent_root: Path | None = None,
) -> Path | None:
    """Resolve only an exact profile owned by this login flow.

    Legacy randomly-suffixed temporary profiles remain accepted solely so an
    explicit cleanup can safely remove leftovers from older versions.  New
    profiles must be a marked 64-hex child of the application profile root.
    """

    try:
        resolved = path.resolve()
        lexical = Path(os.path.abspath(path))
        temporary_root = Path(tempfile.gettempdir()).resolve()
        profile_root = (persistent_root or _persistent_profile_root()).resolve()
    except OSError:
        return None
    # Refuse symlinks/junctions/reparse aliases.  A profile must be the exact
    # lexical directory below the app-owned root, not a link to another profile
    # or to an unrelated directory.
    if os.path.normcase(str(lexical)) != os.path.normcase(str(resolved)):
        return None
    legacy_profile = (
        resolved.parent == temporary_root
        and resolved.name.startswith(PROFILE_PREFIX)
        and len(resolved.name) > len(PROFILE_PREFIX)
    )
    if legacy_profile:
        return resolved
    if (
        resolved.parent != profile_root
        or re.fullmatch(r"[0-9a-f]{64}", resolved.name) is None
    ):
        return None
    marker = resolved / PERSISTENT_PROFILE_MARKER
    expected = (
        f"TenderDownloader browser profile {PERSISTENT_PROFILE_VERSION}\n"
        f"{resolved.name}\n"
    )
    try:
        if resolved.exists() and marker.read_text(encoding="ascii") != expected:
            return None
    except (OSError, UnicodeError):
        return None
    return resolved


def _owned_persistent_profiles(profile_root: Path) -> set[Path]:
    """Enumerate only marked, direct profile children without exposing names."""

    try:
        resolved_root = profile_root.resolve()
        children = list(resolved_root.iterdir())
    except FileNotFoundError:
        return set()
    except OSError:
        return set()
    result: set[Path] = set()
    for child in children:
        owned = _resolve_tool_profile(child, persistent_root=resolved_root)
        if owned is None:
            continue
        try:
            if owned.is_dir():
                result.add(owned)
        except OSError:
            continue
    return result


def _windows_powershell_executable() -> Path | None:
    windows_root = os.environ.get("WINDIR", "").strip()
    if not windows_root:
        return None
    executable = (
        Path(windows_root)
        / "System32"
        / "WindowsPowerShell"
        / "v1.0"
        / "powershell.exe"
    )
    try:
        return executable if executable.is_file() else None
    except OSError:
        return None


def _terminate_windows_profile_processes(
    profile_dir: Path,
    *,
    persistent_root: Path | None = None,
) -> bool:
    """Terminate only Edge/Chrome processes using one exact tool-owned profile.

    Chrome-family launchers can exit after handing the window to a different
    process, so the original ``Popen`` PID is not a reliable cleanup handle on
    Windows. The fallback is intentionally unavailable for arbitrary paths and
    matches a complete ``--user-data-dir`` argument before terminating anything.
    """

    if os.name != "nt":
        return False
    resolved = _resolve_tool_profile(profile_dir, persistent_root=persistent_root)
    if resolved is None:
        return False
    executable = _windows_powershell_executable()
    if executable is None:
        return False
    environment = os.environ.copy()
    environment[_PROFILE_PROCESS_ENV] = str(resolved)
    try:
        result = subprocess.run(
            [
                str(executable),
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _WINDOWS_PROFILE_TERMINATOR,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
            shell=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            env=environment,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def read_browser_cookies(handle: BrowserHandle) -> list[dict[str, Any]]:
    websocket = _WebSocket(handle.websocket_url)
    try:
        result = _cdp_command(websocket, 1, "Storage.getCookies")
    finally:
        websocket.close()
    cookies = result.get("cookies", [])
    if not isinstance(cookies, list):
        raise BrowserLoginError("浏览器返回的 Cookie 格式无效")
    return [item for item in cookies if isinstance(item, dict)]


def _stop_process(
    process: BrowserProcess,
    websocket_url: str | None = None,
    profile_dir: Path | None = None,
    persistent_root: Path | None = None,
) -> bool:
    launcher_exited = process.poll() is not None
    if websocket_url and _close_browser_endpoint(websocket_url):
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
        if launcher_exited or process.poll() is not None:
            return True
    if launcher_exited:
        if (
            os.name == "nt"
            and profile_dir is not None
            and (
                _terminate_windows_profile_processes(profile_dir)
                if persistent_root is None
                else _terminate_windows_profile_processes(
                    profile_dir, persistent_root=persistent_root
                )
            )
        ):
            return not websocket_url or not _cdp_endpoint_alive(websocket_url)
        return not websocket_url or not _cdp_endpoint_alive(websocket_url)
    try:
        pid = getattr(process, "pid", None)
        if os.name == "nt" and isinstance(pid, int) and pid > 0:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            try:
                process.wait(timeout=2)
                if not websocket_url or not _cdp_endpoint_alive(websocket_url):
                    return True
            except (OSError, subprocess.SubprocessError):
                pass
        process.terminate()
        process.wait(timeout=3)
    except (OSError, subprocess.SubprocessError):
        try:
            process.kill()
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass
    if websocket_url and _cdp_endpoint_alive(websocket_url):
        if (
            os.name == "nt"
            and profile_dir is not None
            and (
                _terminate_windows_profile_processes(profile_dir)
                if persistent_root is None
                else _terminate_windows_profile_processes(
                    profile_dir, persistent_root=persistent_root
                )
            )
        ):
            return not _cdp_endpoint_alive(websocket_url)
        return False
    return process.poll() is not None


def _safe_remove_profile(
    path: Path,
    *,
    persistent_root: Path | None = None,
) -> bool:
    """Remove only a profile directory created for this flow."""

    resolved = _resolve_tool_profile(path, persistent_root=persistent_root)
    if resolved is None:
        return False
    for delay in (0, 0.05, 0.1, 0.2, 0.4, 0.8):
        if delay:
            time.sleep(delay)
        try:
            shutil.rmtree(resolved)
            return True
        except FileNotFoundError:
            return True
        except OSError:
            continue
    return False


def _remove_profile_after_browser_exit(
    path: Path,
    *,
    persistent_root: Path | None = None,
) -> bool:
    """Quiesce one exact Windows profile, delete it, and detect recreation.

    Chromium can close the CDP endpoint before its final profile-owning process
    exits. A successful ``rmtree`` in that gap is not authoritative because the
    process may create the directory again. The Windows branch therefore uses
    only the exact ``--user-data-dir`` matcher, then requires the directory to
    remain absent across a second stable process scan.
    """

    resolved = _resolve_tool_profile(path, persistent_root=persistent_root)
    if resolved is None:
        return False
    if os.name != "nt":
        return (
            _safe_remove_profile(resolved)
            if persistent_root is None
            else _safe_remove_profile(resolved, persistent_root=persistent_root)
        )

    for _ in range(3):
        terminated = (
            _terminate_windows_profile_processes(resolved)
            if persistent_root is None
            else _terminate_windows_profile_processes(
                resolved, persistent_root=persistent_root
            )
        )
        if not terminated:
            continue
        removed = (
            _safe_remove_profile(resolved)
            if persistent_root is None
            else _safe_remove_profile(resolved, persistent_root=persistent_root)
        )
        if not removed:
            continue

        # Catch a delayed child that recreates the profile immediately after
        # deletion. This is intentionally bounded and runs only on explicit
        # browser teardown/stale-profile cleanup, never after an ordinary run.
        recreated = False
        for delay in (0.1, 0.2, 0.35):
            time.sleep(delay)
            if resolved.exists():
                recreated = True
                break
        if recreated:
            continue

        # A second exact-profile scan closes the TOCTOU window. If anything
        # appeared, loop and remove the newly created directory as well.
        stable = (
            _terminate_windows_profile_processes(resolved)
            if persistent_root is None
            else _terminate_windows_profile_processes(
                resolved, persistent_root=persistent_root
            )
        )
        if not stable:
            continue
        if not resolved.exists():
            return True
    # Without a confirmed second stable scan, absence may still be the short
    # window before a delayed Edge child recreates the directory.
    return False


def _cleanup_stale_profiles() -> set[Path]:
    """Retry only tool-owned temporary profiles left by an earlier process."""

    try:
        temporary_root = Path(tempfile.gettempdir()).resolve()
        candidates = [
            item
            for item in temporary_root.iterdir()
            if item.name.startswith(PROFILE_PREFIX)
            and (item / PENDING_CLEANUP_MARKER).is_file()
        ]
    except OSError:
        return set()
    return {
        item for item in candidates
        if not _remove_profile_after_browser_exit(item)
    }


def _mark_profile_pending(path: Path) -> None:
    try:
        resolved = path.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        if (
            resolved.parent != temporary_root
            or not resolved.name.startswith(PROFILE_PREFIX)
        ):
            return
        (resolved / PENDING_CLEANUP_MARKER).write_text("", encoding="ascii")
    except OSError:
        pass


def _sanitize_cookies(
    raw_cookies: list[dict[str, Any]],
    *,
    allowed_host: str,
) -> list[dict[str, Any]]:
    allowed_host = allowed_host.strip().lower().rstrip(".")
    if not allowed_host:
        raise BrowserLoginError("登录网站主机名无效")
    result: list[dict[str, Any]] = []
    total_value_length = 0
    for raw in raw_cookies:
        name = raw.get("name", "")
        value = raw.get("value", "")
        domain = raw.get("domain", "")
        path = raw.get("path", "/")
        if not all(isinstance(item, str) for item in (name, value, domain, path)):
            continue
        if not name or not domain or any("\x00" in item for item in (name, value, domain, path)):
            continue
        cookie_domain = domain.lstrip(".").lower().rstrip(".")
        if not cookie_domain or not (
            allowed_host == cookie_domain
            or allowed_host.endswith("." + cookie_domain)
        ):
            # Storage.getCookies returns cookies for every origin touched during
            # an SSO flow.  Only retain cookies the configured portal can use.
            continue
        if len(result) >= MAX_COOKIE_COUNT:
            raise BrowserLoginError(f"登录 Cookie 数量超过安全上限 {MAX_COOKIE_COUNT}")
        if len(name) > 512 or len(value) > MAX_COOKIE_VALUE_LENGTH or len(domain) > 512:
            raise BrowserLoginError("登录 Cookie 长度超过安全上限")
        total_value_length += len(value.encode("utf-8"))
        if total_value_length > MAX_SESSION_ENV_BYTES:
            raise BrowserLoginError("登录 Cookie 总量过大，无法安全传给采集任务")
        cookie: dict[str, Any] = {
            "name": name,
            "value": value,
            "domain": domain,
            "path": path or "/",
            "secure": bool(raw.get("secure", False)),
            "http_only": bool(raw.get("httpOnly", raw.get("http_only", False))),
        }
        expires = raw.get("expires")
        if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
            cookie["expires"] = float(expires)
        same_site = raw.get("sameSite", raw.get("same_site"))
        if isinstance(same_site, str) and same_site in {"Strict", "Lax", "None"}:
            cookie["same_site"] = same_site
        result.append(cookie)
    if not result:
        raise BrowserLoginError("没有捕获到登录 Cookie；请确认已登录成功后再点“完成登录”")
    return result


class BrowserLoginManager:
    """Own persistent, origin-isolated visual-login browser profiles.

    Chromium stores its normal signed-in state in an opaque profile below the
    application data directory.  Cookies/CDP capabilities are still held only
    in memory and a directory's existence never makes a session ``ready``.
    """

    def __init__(
        self,
        *,
        launcher: Launcher = launch_temporary_browser,
        cookie_reader: CookieReader = read_browser_cookies,
        page_probe: PageProbe = probe_browser_page,
        profile_root: Path | None = None,
    ) -> None:
        self._launcher = launcher
        self._cookie_reader = cookie_reader
        self._page_probe = page_probe
        self._profile_root = (profile_root or _persistent_profile_root()).resolve()
        self._lock = threading.RLock()
        self._shutdown_event = threading.Event()
        self._probe_threads: set[threading.Thread] = set()
        self._sessions: dict[str, _Session] = {}
        # source_id -> (opaque lease token, session). A generation-bound token
        # prevents a late process watcher from restoring or closing a newer run.
        self._leased: dict[str, tuple[str, _Session]] = {}
        # Only legacy temporary profiles explicitly marked by older versions are
        # retried here. Persistent profiles are never startup garbage-collected.
        self._pending_profiles = _cleanup_stale_profiles()

    @staticmethod
    def _public(session: _Session) -> dict[str, Any]:
        state = session.state
        try:
            profile_saved = bool(
                session.profile_dir is not None and session.profile_dir.is_dir()
            )
        except OSError:
            profile_saved = False
        return {
            "source_id": session.source_id,
            "state": state,
            "started_at": session.started_at,
            "completed_at": session.completed_at,
            "cookie_count": len(session.cookies) if state == "ready" else 0,
            "error": session.error,
            "persistent": True,
            "profile_saved": profile_saved,
            "requires_user_action": state in {"waiting", "challenge_required"},
        }

    def _remove_profile(
        self,
        profile_dir: Path,
        *,
        ensure_browser_exit: bool = False,
    ) -> bool:
        return (
            _remove_profile_after_browser_exit(
                profile_dir, persistent_root=self._profile_root
            )
            if ensure_browser_exit
            else _safe_remove_profile(
                profile_dir, persistent_root=self._profile_root
            )
        )

    def _close_handle(
        self,
        handle: BrowserHandle | None,
        *,
        clear_profile: bool = False,
    ) -> bool:
        if handle is None:
            return True
        stopped = _stop_process(
            handle.process,
            handle.websocket_url,
            handle.profile_dir,
            persistent_root=self._profile_root,
        )
        if not stopped and os.name == "nt":
            stopped = _terminate_windows_profile_processes(
                handle.profile_dir, persistent_root=self._profile_root
            ) and not _cdp_endpoint_alive(handle.websocket_url)
        if not stopped:
            return False
        if not clear_profile:
            # Ordinary window/app shutdown persists the profile by design.
            return True
        return self._remove_profile(
            handle.profile_dir,
            ensure_browser_exit=handle.live_transport,
        )

    def _retry_pending_profiles(self) -> bool:
        # Backward-compatibility cleanup for marked temp profiles only.
        with self._lock:
            pending = list(self._pending_profiles)
        results = [_remove_profile_after_browser_exit(path) for path in pending]
        with self._lock:
            for path, cleaned in zip(pending, results):
                if cleaned:
                    self._pending_profiles.discard(path)
        return all(results)

    def _start_probe_worker(self, session: _Session) -> None:
        handle = session.handle
        if handle is None or not handle.live_transport:
            return
        with self._lock:
            if (
                session.state not in {"waiting", "challenge_required"}
                or session.probe_worker_running
                or self._shutdown_event.is_set()
            ):
                return
            session.probe_worker_running = True

        def worker() -> None:
            try:
                while not self._shutdown_event.is_set():
                    with self._lock:
                        if self._sessions.get(session.source_id) is not session:
                            return
                        state = session.state
                    if state == "ready" or state in {
                        "closed", "cleared", "clearing", "failed", "in_use"
                    }:
                        return
                    if state in {"waiting", "challenge_required"}:
                        try:
                            self.probe(session.source_id)
                        except BrowserLoginError:
                            pass
                    if self._shutdown_event.wait(0.8):
                        return
            finally:
                with self._lock:
                    session.probe_worker_running = False
                    self._probe_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=worker,
            name="tender-browser-login-probe",
            daemon=True,
        )
        with self._lock:
            self._probe_threads.add(thread)
        thread.start()

    @staticmethod
    def _validate_source_id(source_id: str) -> str:
        if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 64:
            raise BrowserLoginError("来源 ID 无效")
        return source_id.strip()

    def start(
        self,
        source_id: str,
        login_url: str,
        *,
        profile_id: str | None = None,
        check_url: str | None = None,
    ) -> dict[str, Any]:
        self._retry_pending_profiles()
        source_id = self._validate_source_id(source_id)
        login_url = _validate_login_url(login_url)
        profile_id = _validate_profile_id(profile_id or source_id)
        check_url = _validate_check_url(check_url or login_url, login_url)
        with self._lock:
            if source_id in self._leased:
                raise BrowserLoginBusyError("采集任务正在使用该登录窗口，请等待任务结束")
            previous = self._sessions.get(source_id)
            if previous is not None and previous.state == "starting":
                raise BrowserLoginBusyError("该登录窗口正在启动，请稍候")
            if (
                previous is not None
                and previous.handle is not None
                and previous.profile_id == profile_id
                and _login_origin(previous.login_url) == _login_origin(login_url)
                and _browser_handle_alive(previous.handle)
            ):
                previous.login_url = login_url
                previous.check_url = check_url
                if previous.state in {"failed", "closed", "cleared"}:
                    previous.state = "waiting"
                    previous.error = ""
                    previous.cookies = []
                    previous.ready_event.clear()
                focus_browser_page(previous.handle, previous.login_url)
                self._start_probe_worker(previous)
                return self._public(previous)
            if previous is not None:
                self._sessions.pop(source_id, None)
            session = _Session(
                source_id=source_id,
                login_url=login_url,
                profile_id=profile_id,
                check_url=check_url,
            )
            self._sessions[source_id] = session
        if previous is not None and not self._close_handle(previous.handle):
            with self._lock:
                session.state = "failed"
                session.error = "无法关闭旧登录窗口；持久登录数据未删除"
            raise BrowserLoginError(session.error)

        try:
            profile_dir = _prepare_persistent_profile(
                self._profile_root, profile_id, login_url
            )
            resume_saved_profile = _profile_has_browser_data(profile_dir)
            with self._lock:
                if self._sessions.get(source_id) is session:
                    session.profile_dir = profile_dir
            # A same-origin authenticated entry is a better recovery target for
            # an existing Chromium profile: a saved session can become ready
            # without asking the user to sign in again, while an expired session
            # naturally redirects back to the portal's login/challenge page.
            # A brand-new/marker-only profile still opens the explicit login URL.
            launch_url = check_url if resume_saved_profile else login_url
            handle = self._launcher(launch_url, profile_dir)
            if not _handle_matches_profile(handle, profile_dir):
                try:
                    _stop_process(handle.process, handle.websocket_url)
                except (AttributeError, OSError):
                    pass
                raise BrowserLoginError("登录窗口返回了无效的本机浏览器句柄")
        except Exception as exc:
            message = str(exc) if isinstance(exc, BrowserLoginError) else "无法启动登录窗口"
            with self._lock:
                if self._sessions.get(source_id) is session:
                    session.state = "failed"
                    session.error = message
            if isinstance(exc, BrowserLoginError):
                raise
            raise BrowserLoginError(message) from exc
        with self._lock:
            if self._sessions.get(source_id) is not session or session.state != "starting":
                cancelled = True
            else:
                cancelled = False
                session.handle = handle
                session.state = "waiting"
                public = self._public(session)
        if cancelled:
            self._close_handle(handle)
            raise BrowserLoginError("登录已关闭")
        self._start_probe_worker(session)
        return public

    def _capture(
        self,
        source_id: str,
        *,
        automatic: bool,
        force: bool = False,
    ) -> dict[str, Any]:
        source_id = self._validate_source_id(source_id)
        with self._lock:
            session = self._sessions.get(source_id)
            if session is None:
                raise BrowserLoginError("该来源尚未打开登录窗口")
            if session.state == "ready":
                if not (automatic and force and session.handle is not None):
                    return self._public(session)
                # A ready session is rechecked immediately before a new task is
                # leased. This catches a portal that has since expired or moved
                # back to a CAPTCHA without probing on every UI status poll.
                session.state = "waiting"
                session.cookies = []
                session.ready_event.clear()
            if session.state == "verifying":
                if automatic:
                    return self._public(session)
                raise BrowserLoginBusyError("正在自动检查登录状态，请稍候")
            if session.state not in {"waiting", "challenge_required"}:
                raise BrowserLoginError("当前登录窗口不能检查登录状态")
            handle = session.handle
            if handle is None or not _browser_handle_alive(handle):
                session.state = "closed"
                session.cookies = []
                session.ready_event.clear()
                session.error = "登录窗口已关闭；持久登录数据仍保留，可重新打开"
                raise BrowserLoginError(session.error)
            session.state = "verifying"
            session.error = ""

        if automatic:
            page_state = self._page_probe(handle, session.check_url)
            if page_state != "content":
                with self._lock:
                    if session.state == "verifying":
                        session.state = (
                            "challenge_required"
                            if page_state == "challenge"
                            else "waiting"
                        )
                        session.error = ""
                    return self._public(session)
        try:
            cookies = _sanitize_cookies(
                self._cookie_reader(handle),
                allowed_host=urlsplit(session.login_url).hostname or "",
            )
        except Exception as exc:
            message = str(exc) if isinstance(exc, BrowserLoginError) else "读取浏览器登录状态失败"
            with self._lock:
                if session.state == "verifying":
                    # Keep the live window and persistent profile so the user can
                    # finish login/CAPTCHA without starting over.
                    session.state = "waiting" if automatic else "failed"
                    session.error = "" if automatic else message
                    session.ready_event.clear()
                public = self._public(session)
            if automatic:
                return public
            if isinstance(exc, BrowserLoginError):
                raise
            raise BrowserLoginError(message) from exc
        with self._lock:
            if self._sessions.get(source_id) is not session:
                raise BrowserLoginError("登录会话状态已改变，请重试")
            if session.state != "verifying":
                if automatic:
                    return self._public(session)
                raise BrowserLoginError("登录会话状态已改变，请重试")
            session.cookies = cookies
            session.state = "ready"
            session.completed_at = _now()
            session.error = ""
            session.ready_event.set()
            session.handle = handle if handle.live_transport else None
            public = self._public(session)
        if not handle.live_transport and not self._close_handle(handle):
            with self._lock:
                session.cookies = []
                session.state = "failed"
                session.ready_event.clear()
                session.error = "无法关闭登录窗口；持久登录数据未删除"
            raise BrowserLoginError(session.error)
        return public

    def complete(self, source_id: str) -> dict[str, Any]:
        """Compatibility/manual fallback; normal UI flow uses :meth:`probe`."""

        with self._lock:
            session = self._sessions.get(source_id)
            live_transport = bool(
                session is not None
                and session.handle is not None
                and session.handle.live_transport
            )
        result = self._capture(source_id, automatic=live_transport)
        if live_transport and result.get("state") != "ready":
            raise BrowserLoginError(
                "登录或验证码尚未完成；请在浏览器窗口完成，工具会自动继续"
            )
        return result

    def probe(self, source_id: str, *, force: bool = False) -> dict[str, Any]:
        """Read-only automatic login/challenge probe; never fills or submits."""

        result = self._capture(source_id, automatic=True, force=force)
        if result.get("state") in {"waiting", "challenge_required"}:
            with self._lock:
                session = self._sessions.get(source_id)
            if session is not None:
                self._start_probe_worker(session)
        return result

    def answer_slider_challenge(self, source_id: str, expression: str) -> bool:
        """Answer the source site's own slider challenge for this logged-in
        profile, then re-probe. Only used for sources whose adapter supplies
        the challenge answer; the expression runs inside the source's own
        isolated page and returns only a boolean verdict."""

        with self._lock:
            session = self._sessions.get(source_id)
            handle = session.handle if session is not None else None
            check_url = session.check_url if session is not None else ""
        if handle is None or not _browser_handle_alive(handle) or not check_url:
            return False
        outcome = _evaluate_in_source_page(handle, check_url, expression)
        if not outcome.get("solved"):
            return False
        # Stale tabs keep rendering the old challenge markup even after the
        # session is cleared, so a fresh request is the authoritative verdict.
        healthy = _evaluate_in_source_page(handle, check_url, _fresh_request_probe(check_url))
        if not healthy.get("healthy"):
            return False
        try:
            cookies = _sanitize_cookies(
                self._cookie_reader(handle),
                allowed_host=urlsplit(session.login_url).hostname or "",
            )
        except Exception:
            return False
        with self._lock:
            if self._sessions.get(source_id) is not session:
                return False
            session.cookies = cookies
            session.state = "ready"
            session.completed_at = _now()
            session.error = ""
            session.ready_event.set()
            session.handle = handle if handle.live_transport else None
        return True

    def focus(
        self,
        source_id: str,
        login_url: str | None = None,
        *,
        profile_id: str | None = None,
        check_url: str | None = None,
    ) -> dict[str, Any]:
        source_id = self._validate_source_id(source_id)
        profile_id = _validate_profile_id(profile_id or source_id)
        if login_url is not None:
            login_url = _validate_login_url(login_url)
            check_url = _validate_check_url(check_url or login_url, login_url)
        with self._lock:
            session = self._sessions.get(source_id)
            if (
                session is not None
                and session.handle is not None
                and session.profile_id == profile_id
                and (
                    login_url is None
                    or _login_origin(session.login_url) == _login_origin(login_url)
                )
                and _browser_handle_alive(session.handle)
            ):
                handle = session.handle
                session_url = session.login_url
            else:
                handle = None
                session_url = login_url or (session.login_url if session else "")
                session_check_url = check_url or (
                    session.check_url if session is not None else session_url
                )
        if handle is None:
            if not session_url:
                raise BrowserLoginError("该来源尚未打开登录窗口")
            return self.start(
                source_id,
                session_url,
                profile_id=profile_id,
                check_url=session_check_url,
            )
        if not focus_browser_page(handle, session_url):
            raise BrowserLoginError("无法切换到登录窗口，请重新打开")
        with self._lock:
            return self._public(session)

    def cancel(self, source_id: str) -> dict[str, Any]:
        """Close the window while retaining its persistent profile."""

        source_id = self._validate_source_id(source_id)
        with self._lock:
            if source_id in self._leased:
                raise BrowserLoginBusyError("采集任务正在使用该登录窗口，请等待任务结束")
            session = self._sessions.get(source_id)
            if session is None:
                raise BrowserLoginError("该来源没有可关闭的登录会话")
            handle = session.handle
            session.handle = None
            session.state = "closed"
            session.cookies = []
            session.ready_event.clear()
            session.completed_at = _now()
            session.error = ""
        if not self._close_handle(handle):
            with self._lock:
                session.state = "failed"
                session.error = "关闭登录窗口失败；持久登录数据未删除"
            raise BrowserLoginError(session.error)
        return self._public(session)

    def clear(
        self,
        source_id: str,
        login_url: str,
        *,
        profile_id: str | None = None,
    ) -> dict[str, Any]:
        """Explicitly close and delete one exact source+origin profile."""

        source_id = self._validate_source_id(source_id)
        login_url = _validate_login_url(login_url)
        profile_id = _validate_profile_id(profile_id or source_id)
        digest = _profile_digest(profile_id, login_url)
        profile_dir = self._profile_root / digest
        with self._lock:
            if source_id in self._leased:
                raise BrowserLoginBusyError("采集任务正在使用该登录窗口，请等待任务结束")
            session = self._sessions.get(source_id)
            if session is not None and session.state in {"starting", "verifying"}:
                raise BrowserLoginBusyError("登录状态正在变化，请稍候再清除")
            if session is not None and _login_origin(session.login_url) != _login_origin(login_url):
                raise BrowserLoginError("登录地址已改变，拒绝清除不匹配的浏览器数据")
            if session is not None and session.profile_id != profile_id:
                raise BrowserLoginError("浏览器登录配置已改变，拒绝清除不匹配的数据")
            handle = session.handle if session is not None else None
            if session is None:
                session = _Session(
                    source_id=source_id,
                    login_url=login_url,
                    profile_id=profile_id,
                    check_url=login_url,
                )
                self._sessions[source_id] = session
            session.profile_dir = profile_dir
            session.handle = None
            session.cookies = []
            session.ready_event.clear()
            session.state = "clearing"
            session.error = ""
        if handle is not None and not self._close_handle(handle):
            with self._lock:
                session.state = "failed"
                session.error = "无法关闭登录窗口，未删除持久登录数据"
            raise BrowserLoginError(session.error)
        if not self._remove_profile(
            profile_dir,
            # Explicit clear also handles a browser orphaned by an earlier app
            # crash. Windows cleanup remains scoped to this exact profile path.
            ensure_browser_exit=True,
        ):
            with self._lock:
                session.state = "failed"
                session.error = "持久浏览器登录数据清理失败；未删除其他目录"
            raise BrowserLoginError(session.error)
        with self._lock:
            session.state = "cleared"
            session.profile_dir = None
            session.completed_at = _now()
            return self._public(session)

    def status(self, *, auto_probe: bool = True) -> list[dict[str, Any]]:
        if auto_probe:
            with self._lock:
                probe_ids = [
                    source_id for source_id, session in self._sessions.items()
                    if session.state in {"waiting", "challenge_required"}
                    and session.handle is not None
                    and session.handle.live_transport
                ]
            for source_id in probe_ids:
                try:
                    self.probe(source_id)
                except BrowserLoginError:
                    pass

        stale: list[tuple[_Session, BrowserHandle]] = []
        with self._lock:
            for session in self._sessions.values():
                if (
                    session.state in {"waiting", "challenge_required", "verifying", "ready"}
                    and session.handle is not None
                    and not _browser_handle_alive(session.handle)
                ):
                    session.state = "closed"
                    session.cookies = []
                    session.ready_event.clear()
                    session.error = "登录窗口已关闭；持久登录数据仍保留，可重新打开"
                    stale.append((session, session.handle))
                    session.handle = None
        for _session, handle in stale:
            self._close_handle(handle)
        with self._lock:
            return [self._public(item) for item in self._sessions.values()]

    def _expected_profile_paths(
        self,
        expected_login_urls: dict[str, str],
        *,
        profile_ids: dict[str, str] | None = None,
    ) -> set[Path]:
        if not isinstance(expected_login_urls, dict):
            raise BrowserLoginError("浏览器登录来源配置无效")
        if profile_ids is not None and not isinstance(profile_ids, dict):
            raise BrowserLoginError("浏览器登录配置标识无效")
        result: set[Path] = set()
        for source_id, login_url in expected_login_urls.items():
            safe_source_id = self._validate_source_id(source_id)
            safe_login_url = _validate_login_url(login_url)
            safe_profile_id = _validate_profile_id(
                profile_ids.get(source_id, safe_source_id)
                if profile_ids is not None
                else safe_source_id
            )
            result.add(
                (
                    self._profile_root
                    / _profile_digest(safe_profile_id, safe_login_url)
                ).resolve()
            )
        return result

    def orphaned_profile_summary(
        self,
        expected_login_urls: dict[str, str],
        *,
        profile_ids: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Return only an orphan count; never expose profile names or paths."""

        expected = self._expected_profile_paths(
            expected_login_urls, profile_ids=profile_ids
        )
        with self._lock:
            owned = _owned_persistent_profiles(self._profile_root)
            return {"orphaned_profile_count": len(owned - expected)}

    def clear_orphaned_profiles(
        self,
        expected_login_urls: dict[str, str],
        *,
        profile_ids: dict[str, str] | None = None,
    ) -> dict[str, int]:
        """Explicitly remove profiles no longer reachable from current config.

        Every deletion still passes the marker/direct-child guard.  The method
        refuses to touch a profile leased to a running collection task and
        returns counts only, keeping paths, origins, cookies and CDP capabilities
        out of the web API.
        """

        expected = self._expected_profile_paths(
            expected_login_urls, profile_ids=profile_ids
        )
        removed_count = 0
        failed_count = 0
        with self._lock:
            candidates = _owned_persistent_profiles(self._profile_root) - expected
            if not candidates:
                return {
                    "removed_count": 0,
                    "failed_count": 0,
                    "orphaned_profile_count": 0,
                }

            for _token, session in self._leased.values():
                if session.profile_dir is None:
                    continue
                try:
                    leased_path = session.profile_dir.resolve()
                except OSError:
                    continue
                if leased_path in candidates:
                    raise BrowserLoginBusyError(
                        "采集任务正在使用一项已移除来源的登录数据；请等待任务结束后再清理"
                    )

            tracked: dict[Path, tuple[str, _Session]] = {}
            for source_id, session in self._sessions.items():
                if session.profile_dir is None:
                    continue
                try:
                    session_path = session.profile_dir.resolve()
                except OSError:
                    continue
                if session_path not in candidates:
                    continue
                if session.state in {"starting", "verifying", "clearing"}:
                    raise BrowserLoginBusyError(
                        "登录状态正在变化，请稍候再清理已移除来源的登录数据"
                    )
                tracked[session_path] = (source_id, session)

            for profile_dir in candidates:
                tracked_session = tracked.get(profile_dir)
                if tracked_session is not None:
                    _source_id, session = tracked_session
                    session.state = "clearing"
                    session.cookies = []
                    session.ready_event.clear()
                    handle = session.handle
                    session.handle = None
                    if not self._close_handle(handle):
                        session.state = "failed"
                        session.error = "无法关闭已移除来源的登录窗口；未删除登录数据"
                        failed_count += 1
                        continue

                removed = self._remove_profile(
                    profile_dir,
                    # Also handles an exact-profile browser left by a prior app
                    # crash. The process matcher cannot target other profiles.
                    ensure_browser_exit=True,
                )
                if removed:
                    removed_count += 1
                    if tracked_session is not None:
                        source_id, session = tracked_session
                        if self._sessions.get(source_id) is session:
                            self._sessions.pop(source_id, None)
                else:
                    failed_count += 1
                    if tracked_session is not None:
                        _source_id, session = tracked_session
                        session.state = "failed"
                        session.error = "已移除来源的登录数据清理失败；未删除其他目录"

            remaining = len(
                _owned_persistent_profiles(self._profile_root) - expected
            )
            return {
                "removed_count": removed_count,
                "failed_count": failed_count,
                "orphaned_profile_count": remaining,
            }

    def checkpoint(
        self,
        expected_login_urls: dict[str, str],
        *,
        profile_ids: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Return a credential-free readiness checkpoint for a pending run."""

        self.status(auto_probe=True)
        ready: list[str] = []
        pending: list[dict[str, Any]] = []
        with self._lock:
            for source_id, login_url in expected_login_urls.items():
                expected_profile_id = _validate_profile_id(
                    profile_ids.get(source_id, source_id)
                    if profile_ids is not None
                    else source_id
                )
                session = self._sessions.get(source_id)
                state = session.state if session is not None else "idle"
                if (
                    session is not None
                    and session.profile_id == expected_profile_id
                    and _login_origin(session.login_url) == _login_origin(login_url)
                    and state == "ready"
                    and bool(session.cookies)
                ):
                    ready.append(source_id)
                else:
                    profile_dir = self._profile_root / _profile_digest(
                        expected_profile_id, login_url
                    )
                    owned_profile = _resolve_tool_profile(
                        profile_dir, persistent_root=self._profile_root
                    )
                    try:
                        profile_saved = bool(
                            owned_profile is not None and owned_profile.is_dir()
                        )
                    except OSError:
                        profile_saved = False
                    pending.append(
                        {
                            "source_id": source_id,
                            "state": state,
                            "profile_saved": profile_saved,
                        }
                    )
        return {
            "state": "ready" if not pending else "waiting_for_user",
            "ready": ready,
            "pending": pending,
        }

    def wait_for_sources(self, source_ids: list[str], timeout: float = 0) -> bool:
        """Wait on in-memory readiness events; no credential data is returned."""

        if timeout < 0:
            raise ValueError("timeout 不能为负数")
        with self._lock:
            sessions = [self._sessions.get(source_id) for source_id in source_ids]
        if any(session is None for session in sessions):
            return False
        deadline = time.monotonic() + timeout
        for session in sessions:
            remaining = max(0.0, deadline - time.monotonic())
            if not session.ready_event.wait(remaining):
                return False
        return True

    def sessions_for_sources(
        self,
        source_ids: list[str],
        *,
        expected_login_urls: dict[str, str] | None = None,
        expected_profile_ids: dict[str, str] | None = None,
        assume_ready: bool = False,
    ) -> dict[str, dict[str, Any]]:
        with self._lock:
            live_ready = [
                source_id
                for source_id in source_ids
                if source_id in self._sessions
                and self._sessions[source_id].state == "ready"
                and self._sessions[source_id].handle is not None
                and self._sessions[source_id].handle.live_transport
            ]
        # The DOM probe re-reads already-rendered tabs; after an auto-answered
        # challenge was confirmed by a fresh request, stale challenge tabs must
        # not outvote that verdict, so the pre-lease probe is skipped.
        if not assume_ready:
            for source_id in live_ready:
                try:
                    self.probe(source_id, force=True)
                except BrowserLoginError:
                    pass
            self.status(auto_probe=True)
        with self._lock:
            missing = [
                source_id
                for source_id in source_ids
                if source_id not in self._sessions
                or self._sessions[source_id].state != "ready"
                or not self._sessions[source_id].cookies
            ]
            if missing:
                raise BrowserLoginError(
                    "以下来源正在等待登录或验证码，请在已打开窗口完成后重试："
                    + "、".join(missing)
                )
            changed = [
                source_id
                for source_id in source_ids
                if (
                    expected_login_urls is not None
                    and (
                        source_id not in expected_login_urls
                        or _login_origin(self._sessions[source_id].login_url)
                        != _login_origin(expected_login_urls[source_id])
                    )
                )
                or (
                    expected_profile_ids is not None
                    and (
                        source_id not in expected_profile_ids
                        or self._sessions[source_id].profile_id
                        != _validate_profile_id(expected_profile_ids[source_id])
                    )
                )
            ]
            if changed:
                raise BrowserLoginError(
                    "以下来源的登录地址已改变，请重新登录：" + "、".join(changed)
                )
            stale = [
                source_id
                for source_id in source_ids
                if self._sessions[source_id].handle is not None
                and not _browser_handle_alive(self._sessions[source_id].handle)
            ]
            if stale:
                raise BrowserLoginError(
                    "以下来源的登录窗口已关闭，请重新打开：" + "、".join(stale)
                )
            result = {
                source_id: self._session_payload(self._sessions[source_id])
                for source_id in source_ids
            }
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_SESSION_ENV_BYTES:
            raise BrowserLoginError("全部来源的登录 Cookie 总量过大，无法安全传给采集任务")
        return result

    @staticmethod
    def _session_payload(session: _Session) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "cookies": [dict(cookie) for cookie in session.cookies],
            "captured_at": session.completed_at,
        }
        handle = session.handle
        if handle is not None and handle.live_transport:
            payload["browser_transport"] = {
                "kind": "cdp-load-network-resource-v1",
                "websocket_url": handle.websocket_url,
                "origin": _origin_text(session.login_url),
            }
        return payload

    def lease_for_sources(
        self,
        source_ids: list[str],
        *,
        expected_login_urls: dict[str, str] | None = None,
        expected_profile_ids: dict[str, str] | None = None,
        lease_token: str | None = None,
        assume_ready: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Atomically hide ready sessions while a child process owns them."""

        token = lease_token or secrets.token_urlsafe(24)
        if not isinstance(token, str) or not token or len(token) > 256:
            raise BrowserLoginError("登录会话租约标识无效")
        result = self.sessions_for_sources(
            source_ids,
            expected_login_urls=expected_login_urls,
            expected_profile_ids=expected_profile_ids,
            assume_ready=assume_ready,
        )
        with self._lock:
            for source_id in source_ids:
                session = self._sessions.pop(source_id, None)
                if session is None:
                    for restore_id in source_ids:
                        leased = self._leased.get(restore_id)
                        if leased is not None and leased[0] == token:
                            self._leased.pop(restore_id, None)
                            restored = leased[1]
                            restored.state = "ready"
                            restored.ready_event.set()
                            self._sessions[restore_id] = restored
                    raise BrowserLoginError("登录会话状态已改变，请重试")
                session.state = "in_use"
                self._leased[source_id] = (token, session)
        return result

    def release_leases(
        self,
        source_ids: list[str],
        *,
        restore: bool = False,
        lease_token: str | None = None,
    ) -> None:
        closing: list[_Session] = []
        with self._lock:
            for source_id in source_ids:
                leased = self._leased.get(source_id)
                if leased is None:
                    continue
                token, session = leased
                if lease_token is not None and token != lease_token:
                    continue
                self._leased.pop(source_id, None)
                can_restore = session.handle is None or _browser_handle_alive(session.handle)
                if restore and can_restore:
                    session.state = "ready"
                    session.ready_event.set()
                    self._sessions[source_id] = session
                else:
                    session.state = "closed"
                    session.cookies = []
                    session.ready_event.clear()
                    closing.append(session)
        for session in closing:
            self._close_handle(session.handle)

    def consume(self, source_ids: list[str]) -> None:
        closing: list[_Session] = []
        with self._lock:
            for source_id in source_ids:
                session = self._sessions.pop(source_id, None)
                if session is None:
                    leased = self._leased.pop(source_id, None)
                    session = leased[1] if leased is not None else None
                if session is not None:
                    session.cookies = []
                    session.ready_event.clear()
                    closing.append(session)
        for session in closing:
            self._close_handle(session.handle)

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values()) + [
                session for _, session in self._leased.values()
            ]
            self._sessions.clear()
            self._leased.clear()
            for session in sessions:
                session.state = "closed"
                session.cookies = []
                session.ready_event.clear()
            self._shutdown_event.set()
            probe_threads = list(self._probe_threads)
        for thread in probe_threads:
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        for session in sessions:
            # App shutdown closes the browser but deliberately preserves the
            # isolated profile. Only ``clear`` can delete it.
            self._close_handle(session.handle)
        self._retry_pending_profiles()
