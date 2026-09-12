from __future__ import annotations

import copy
import csv
import errno
import hmac
import io
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from ..ai_connection import test_ai_connection
from ..ai_keys import MacKeyStore, key_account
from ..ai_models import list_ai_models
from ..classify import RuleClassifier
from ..config import effective_user_agent, load_config, sample_config, validate_config_data
from ..database import Database
from ..export import download_links_csv
from ..http_client import HttpClient
from ..pipeline import Pipeline
from ..query import normalise_query, query_config, query_state_path
from ..sources import build_sources, CustomWebSource, source_display_name
from ..sources.okcis import CAPTCHA_SOLVE as OKCIS_CAPTCHA_SOLVE
from ..storage import ImmutableStore
from .browser_login import (
    BrowserLoginBusyError,
    BrowserLoginError,
    BrowserLoginManager,
)


MAX_REQUEST_BYTES = 256 * 1024
MAX_LOG_LINES = 2_000
SOURCE_CREDENTIALS_ENV = "TENDER_SOURCE_CREDENTIALS_JSON"
SOURCE_SESSIONS_ENV = "TENDER_SOURCE_SESSIONS_JSON"
SENSITIVE_KEYS = {
    "api_key",
    "access_token",
    "refresh_token",
    "password",
    "secret",
    "authorization",
    "token",
    "client_secret",
    "private_key",
}
NORMALIZED_SENSITIVE_KEYS = {"".join(char for char in key if char.isalnum()) for key in SENSITIVE_KEYS}
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/styles.css": ("styles.css", "text/css; charset=utf-8"),
}


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class AlreadyRunningError(RuntimeError):
    pass


class WebUIAlreadyRunningError(RuntimeError):
    """Raised when another local dashboard already owns the requested port."""

    pass


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _is_secret_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = "".join(char for char in key.strip().lower() if char.isalnum())
    return normalized in NORMALIZED_SENSITIVE_KEYS


def _contains_secret(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_is_secret_key(key) or _contains_secret(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_secret(item) for item in value)
    return False


def _without_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _without_secrets(item)
            for key, item in value.items()
            if not _is_secret_key(key)
        }
    if isinstance(value, list):
        return [_without_secrets(item) for item in value]
    return copy.deepcopy(value)


def _without_browser_profile_ids(value: Any) -> Any:
    """Return a public config copy without internal browser-profile bindings."""

    result = _without_secrets(value)
    if not isinstance(result, dict):
        return result
    sources = result.get("sources", [])
    if not isinstance(sources, list):
        return result
    for source in sources:
        if not isinstance(source, dict) or source.get("type") != "custom_web":
            continue
        auth = source.get("auth")
        if isinstance(auth, dict):
            auth.pop("profile_id", None)
    return result


def _browser_source_origin(source: Any) -> tuple[str, str, int] | None:
    if not isinstance(source, dict) or source.get("type") != "custom_web":
        return None
    auth = source.get("auth", {})
    if not isinstance(auth, dict):
        return None
    start_urls = source.get("start_urls", [])
    login_url = str(auth.get("login_url", "")).strip()
    if not login_url and isinstance(start_urls, list) and start_urls:
        login_url = str(start_urls[0]).strip()
    try:
        parsed = urlsplit(login_url)
        if not parsed.hostname:
            return None
        return (
            parsed.scheme.lower(),
            parsed.hostname.lower().rstrip("."),
            parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
        )
    except ValueError:
        return None


def _stored_profile_id(source: Any) -> str:
    if not isinstance(source, dict):
        return ""
    auth = source.get("auth", {})
    if not isinstance(auth, dict):
        return ""
    configured = str(auth.get("profile_id", "")).strip()
    return configured or str(source.get("id", "")).strip()


def _assign_browser_profile_ids(
    candidate: dict[str, Any],
    previous: dict[str, Any] | None,
) -> None:
    """Bind browser profiles without trusting or exposing a client-side id.

    Existing legacy sources use their source id, preserving their exact on-disk
    digest.  Once stored, the internal id survives ordinary source-id/name edits.
    Ambiguous matches deliberately get a fresh isolated profile rather than risk
    attaching another source's signed-in browser data.
    """

    sources = candidate.get("sources", [])
    if not isinstance(sources, list):
        return
    previous_sources = (
        previous.get("sources", []) if isinstance(previous, dict) else []
    )
    if not isinstance(previous_sources, list):
        previous_sources = []
    previous_by_id = {
        str(source.get("id", "")).strip(): source
        for source in previous_sources
        if isinstance(source, dict) and source.get("type") == "custom_web"
    }
    used: set[str] = set()

    for index, source in enumerate(sources):
        if not isinstance(source, dict) or source.get("type") != "custom_web":
            continue
        auth = source.get("auth")
        if not isinstance(auth, dict):
            continue
        source_id = str(source.get("id", "")).strip()
        origin = _browser_source_origin(source)
        matched = previous_by_id.get(source_id)
        if matched is None and index < len(previous_sources):
            indexed = previous_sources[index]
            if (
                isinstance(indexed, dict)
                and indexed.get("type") == "custom_web"
                and origin is not None
                and _browser_source_origin(indexed) == origin
            ):
                matched = indexed
        if matched is None and origin is not None:
            same_name_origin = [
                item
                for item in previous_sources
                if isinstance(item, dict)
                and item.get("type") == "custom_web"
                and str(item.get("name", "")).strip()
                == str(source.get("name", "")).strip()
                and _browser_source_origin(item) == origin
            ]
            if len(same_name_origin) == 1:
                matched = same_name_origin[0]

        mode = str(auth.get("mode", "none"))
        if mode != "browser":
            previous_id = _stored_profile_id(matched)
            if previous_id:
                auth["profile_id"] = previous_id
            else:
                auth.pop("profile_id", None)
            continue

        profile_id = _stored_profile_id(matched)
        if not profile_id or profile_id in used:
            while True:
                profile_id = f"profile_{secrets.token_hex(16)}"
                if profile_id not in used:
                    break
        auth["profile_id"] = profile_id
        used.add(profile_id)


def _redaction_values(values: list[str]) -> tuple[str, ...]:
    variants: set[str] = set()
    for value in values:
        if not value:
            continue
        variants.add(value)
        # Child programs often print the JSON environment value, in which
        # quotes, backslashes and control characters are escaped.
        escaped = json.dumps(value, ensure_ascii=False)[1:-1]
        if escaped:
            variants.add(escaped)
    return tuple(sorted(variants, key=len, reverse=True))


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


class ProcessManager:
    """Runs one downloader child process and keeps a bounded, redacted log."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path.resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._generation = 0
        self._operation: str | None = None
        self._state = "idle"
        self._exit_code: int | None = None
        self._started_at: str | None = None
        self._finished_at: str | None = None
        self._stop_requested = False
        self._logs: deque[dict[str, Any]] = deque(maxlen=MAX_LOG_LINES)
        self._next_log_id = 1
        self._redactions: tuple[str, ...] = ()

    def _append_log_locked(self, message: str) -> None:
        cleaned = message.replace("\x00", "").rstrip("\r\n")
        for secret in self._redactions:
            if secret:
                cleaned = cleaned.replace(secret, "[已隐藏密钥]")
        # Prevent a single malformed line from monopolising status responses.
        if len(cleaned) > 8_000:
            cleaned = cleaned[:8_000] + "…（已截断）"
        self._logs.append(
            {
                "id": self._next_log_id,
                "timestamp": _now(),
                "message": cleaned,
            }
        )
        self._next_log_id += 1

    def _append_log(self, message: str) -> None:
        with self._lock:
            self._append_log_locked(message)

    def start(
        self,
        operation: str,
        *,
        api_key: str = "",
        api_key_env: str = "",
        source_credentials: dict[str, dict[str, str]] | None = None,
        source_sessions: dict[str, dict[str, Any]] | None = None,
        finished_callback: Callable[[], None] | None = None,
        query_spec: dict | None = None,
    ) -> None:
        if operation not in {"run", "verify", "sample", "query"}:
            raise ValueError(f"不支持的任务: {operation}")
        with self._lock:
            if self._process is not None and self._process.poll() is None:
                raise AlreadyRunningError("已有任务正在运行")

            self._generation += 1
            generation = self._generation
            self._operation = operation
            self._state = "starting"
            self._exit_code = None
            self._started_at = _now()
            self._finished_at = None
            self._stop_requested = False
            self._logs.clear()
            credential_values = [
                value
                for credentials in (source_credentials or {}).values()
                for value in credentials.values()
                if value
            ]
            session_values: list[str] = []
            for session in (source_sessions or {}).values():
                if not isinstance(session, dict):
                    continue
                cookies = session.get("cookies", [])
                if isinstance(cookies, list):
                    session_values.extend(
                        str(cookie.get("value", ""))
                        for cookie in cookies
                        if isinstance(cookie, dict) and cookie.get("value")
                    )
                browser_transport = session.get("browser_transport")
                if isinstance(browser_transport, dict):
                    capability = browser_transport.get("websocket_url", "")
                    if isinstance(capability, str) and capability:
                        session_values.append(capability)
            inherited_api_key = (
                os.environ.get(api_key_env, "")
                if api_key_env and not api_key
                else ""
            )
            self._redactions = _redaction_values(
                [api_key, inherited_api_key, *credential_values, *session_values]
            )

            environment = os.environ.copy()
            environment["PYTHONUTF8"] = "1"
            environment["PYTHONIOENCODING"] = "utf-8"
            environment["PYTHONUNBUFFERED"] = "1"
            if api_key and api_key_env:
                environment[api_key_env] = api_key
            if source_credentials:
                environment[SOURCE_CREDENTIALS_ENV] = json.dumps(
                    source_credentials,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                environment.pop(SOURCE_CREDENTIALS_ENV, None)
            if source_sessions:
                environment[SOURCE_SESSIONS_ENV] = json.dumps(
                    source_sessions,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            else:
                environment.pop(SOURCE_SESSIONS_ENV, None)

            command = [
                sys.executable,
                "-u",
                "-m",
                "tender_downloader",
                "run" if operation in {"sample", "query"} else operation,
                "--config",
                str(self.config_path),
            ]
            if operation == "sample":
                command.append("--sample")
            if operation == "query":
                command.extend(["--query-json", json.dumps(query_spec, ensure_ascii=False)])
            kwargs: dict[str, Any] = {
                "args": command,
                "cwd": str(self.config_path.parent),
                "env": environment,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.PIPE,
                "stderr": subprocess.STDOUT,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "bufsize": 1,
                "shell": False,
            }
            if os.name == "nt":
                kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True

            try:
                process = subprocess.Popen(**kwargs)
            except Exception:
                self._state = "failed"
                self._finished_at = _now()
                self._redactions = ()
                raise
            self._process = process
            self._state = "running"
            self._append_log_locked(
                "开始按本次条件查询各平台。" if operation == "query" else
                "开始试采最多10条公开公告。" if operation == "sample" else
                "开始运行采集任务。" if operation == "run" else "开始校验原件哈希。"
            )

        threading.Thread(
            target=self._watch,
            args=(process, generation, finished_callback),
            name=f"tender-{operation}-{generation}",
            daemon=True,
        ).start()

    def _watch(
        self,
        process: subprocess.Popen[str],
        generation: int,
        finished_callback: Callable[[], None] | None = None,
    ) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                self._append_log(line)
        except (OSError, ValueError) as exc:
            self._append_log(f"读取任务日志失败：{exc}")
        finally:
            try:
                process.stdout.close()
            except OSError:
                pass
            return_code = process.wait()
            if finished_callback is not None:
                try:
                    finished_callback()
                except Exception:
                    # Never include the callback exception: it may contain a
                    # temporary CDP capability or profile path.
                    self._append_log("登录浏览器会话清理失败，请关闭登录窗口后重启工具。")
            with self._lock:
                if generation != self._generation or process is not self._process:
                    return
                self._exit_code = return_code
                self._finished_at = _now()
                if self._stop_requested or return_code == 130:
                    self._state = "stopped"
                    message = "任务已停止；已下载的原件和断点仍会保留。"
                elif return_code == 0:
                    self._state = "completed"
                    message = "任务已成功完成。"
                elif return_code == 2:
                    self._state = "partial"
                    message = "任务部分完成；请在日志和 coverage.csv 中查看未覆盖来源。"
                else:
                    self._state = "failed"
                    message = f"任务失败，退出码 {return_code}。"
                self._append_log_locked(message)
                # The key is no longer needed after the child exits.
                self._redactions = ()

    def stop(self) -> bool:
        with self._lock:
            process = self._process
            if process is None or process.poll() is not None:
                return False
            self._stop_requested = True
            self._state = "stopping"
            self._append_log_locked("正在停止任务，请稍候……")

        # Give the CLI a chance to run its cleanup path. On Windows CTRL_BREAK is
        # only valid for a process created in its own process group.
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                process.send_signal(signal.SIGINT)
            process.wait(timeout=6)
        except (OSError, subprocess.TimeoutExpired):
            try:
                process.terminate()
                process.wait(timeout=4)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        return True

    def close(self) -> None:
        self.stop()

    def status(self, *, after: int = 0) -> dict[str, Any]:
        with self._lock:
            process = self._process
            running = process is not None and process.poll() is None
            logs = [copy.deepcopy(row) for row in self._logs if int(row["id"]) > after]
            last_log_id = self._next_log_id - 1
            return {
                "running": running,
                "operation": self._operation,
                "state": self._state,
                "exit_code": self._exit_code,
                "started_at": self._started_at,
                "finished_at": self._finished_at,
                "pid": process.pid if running and process is not None else None,
                "last_log_id": last_log_id,
                "logs": logs,
            }


class ConfigWebApp:
    def __init__(
        self,
        config_path: str | Path,
        *,
        browser_login_manager: BrowserLoginManager | None = None,
    ) -> None:
        self.config_path = Path(config_path).resolve()
        self.static_dir = Path(__file__).with_name("static")
        self.csrf_token = secrets.token_urlsafe(32)
        self.ai_keys = MacKeyStore()
        self.runner = ProcessManager(self.config_path)
        self.browser_logins = browser_login_manager or BrowserLoginManager()
        self._download_lock = threading.RLock()
        self._download_states_lock = threading.RLock()
        self._download_states: dict[str, str] = {}

    def status(self, *, after: int = 0) -> dict[str, Any]:
        status = self.runner.status(after=after)
        with self._download_states_lock:
            status["active_downloads"] = dict(self._download_states)
        return status

    def read_config(self) -> dict[str, Any]:
        config = load_config(self.config_path)
        return _without_browser_profile_ids(config.data)

    def save_config(self, data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        if _contains_secret(data):
            raise ValueError("配置中不能保存明文密钥；请在 API Key 输入框填写并使用“保存 Key”")
        validate_config_data(data)
        safe_data = _without_secrets(data)
        try:
            previous = load_config(self.config_path).data
        except FileNotFoundError:
            previous = None
        _assign_browser_profile_ids(safe_data, previous)
        validate_config_data(safe_data)
        _atomic_write_json(self.config_path, safe_data)
        return _without_browser_profile_ids(safe_data)

    def validate_config(self, data: Any | None = None) -> dict[str, Any]:
        candidate = self.read_config() if data is None else data
        if not isinstance(candidate, dict):
            raise ValueError("配置必须是 JSON 对象")
        if _contains_secret(candidate):
            raise ValueError("配置对象不能包含明文密钥")
        validate_config_data(candidate, require_ready=True)
        warnings: list[str] = []
        ai = candidate.get("ai", {})
        endpoint = str(ai.get("endpoint", "")) if isinstance(ai, dict) else ""
        http = candidate.get("http", {})
        if ai.get("enabled", False) and endpoint.startswith(
            ("http://127.0.0.1", "http://localhost", "http://[::1]")
        ):
            if not isinstance(http, dict) or not http.get("allow_private_hosts", False):
                warnings.append(
                    "AI 地址在本机，但当前 HTTP 安全策略会拒绝私网地址；请使用公网 HTTPS 模型地址。"
                )
            else:
                warnings.append(
                    "已允许私网地址：此开关也会作用于采集链接，正式运行建议改用公网 HTTPS 模型地址。"
                )
        return {"valid": True, "warnings": warnings}

    def _resolve_ai_key(self, ai: dict, supplied: str = "") -> str:
        if not isinstance(supplied, str) or len(supplied) > 16_384 or any(c in supplied for c in "\x00\r\n"):
            raise ValueError("API Key 长度或格式无效")
        return (supplied.strip() or self.ai_keys.get(key_account(self.config_path, ai))
                or os.environ.get(str(ai.get("api_key_env", "TENDER_AI_API_KEY")), "").strip())

    def _ai_draft(self, ai_config: Any = None, http_config: Any = None) -> tuple[dict, dict]:
        candidate = copy.deepcopy(load_config(self.config_path).data)
        for name, supplied in (("ai", ai_config), ("http", http_config)):
            if supplied is not None:
                if not isinstance(supplied, dict) or _contains_secret(supplied):
                    raise ValueError(f"{name} 必须是配置对象，且不能包含明文密钥")
                candidate[name] = copy.deepcopy(supplied)
        ai = copy.deepcopy(candidate.get("ai", {}))
        # Fetching models/saving a credential must work before a model is chosen.
        candidate["ai"]["enabled"] = True
        candidate["ai"]["model"] = "model-discovery"
        validate_config_data(candidate, require_ready=True)
        return ai, candidate.get("http", {})

    def ai_key_status(self, ai_config: Any = None, http_config: Any = None) -> dict:
        ai, _ = self._ai_draft(ai_config, http_config)
        return {"ok": True, "saved": self.ai_keys.exists(key_account(self.config_path, ai)),
                "supported": self.ai_keys.supported,
                "environment": bool(os.environ.get(str(ai.get("api_key_env", "TENDER_AI_API_KEY")), "").strip())}

    def save_ai_key(self, api_key: str, ai_config: Any = None, http_config: Any = None) -> dict:
        ai, _ = self._ai_draft(ai_config, http_config)
        if not isinstance(api_key, str) or not api_key.strip() or len(api_key) > 16_384 or any(c in api_key for c in "\x00\r\n"):
            raise ValueError("请填写要保存的 API Key")
        # Remember the connection too, so a reload can find the same Keychain
        # entry. Other unsaved page sections remain untouched.
        candidate = copy.deepcopy(load_config(self.config_path).data)
        for name in ("provider", "protocol", "endpoint", "model", "api_key_env", "enabled"):
            if name in ai:
                candidate.setdefault("ai", {})[name] = ai[name]
        validate_config_data(candidate)
        self.ai_keys.save(key_account(self.config_path, ai), api_key.strip())
        try:
            _atomic_write_json(self.config_path, candidate)
        except OSError as exc:
            raise ValueError("Key 已保存到钥匙串，但接口配置保存失败；请重试保存配置") from exc
        return {"ok": True, "saved": True, "supported": True, "environment": False,
                "message": "Key 已保存到 Mac 钥匙串，当前接口和模型设置也已保存"}

    def delete_ai_key(self, ai_config: Any = None, http_config: Any = None) -> dict:
        ai, _ = self._ai_draft(ai_config, http_config)
        self.ai_keys.delete(key_account(self.config_path, ai))
        return self.ai_key_status(ai_config, http_config)

    def fetch_ai_models(self, api_key: str = "", ai_config: Any = None, http_config: Any = None) -> dict:
        ai, http = self._ai_draft(ai_config, http_config)
        credential = self._resolve_ai_key(ai, api_key)
        if not credential:
            raise ValueError("请先填写或保存 API Key，再获取模型列表")
        client = HttpClient(user_agent=effective_user_agent(http), delay_seconds=0, max_retries=0,
            timeout_seconds=min(float(ai.get("timeout_seconds", 60)), 60),
            max_response_bytes=8 * 1024 * 1024,
            allow_private_hosts=bool(http.get("allow_private_hosts", False)),
            allow_benchmark_proxy_hosts=bool(http.get("allow_benchmark_proxy_hosts", False)))
        try:
            return list_ai_models(client, ai, credential)
        finally:
            client.close()

    def test_ai(
        self,
        api_key: str = "",
        *,
        ai_config: Any | None = None,
        http_config: Any | None = None,
    ) -> dict[str, object]:
        """Test a saved or temporary model configuration without persisting it.

        Only the ``ai`` and ``http`` sections may come from the page draft.  All
        other validation context, especially ``sources``, is loaded from the
        saved configuration.  This keeps an unfinished custom-source editor from
        blocking an otherwise independent model connectivity test.
        """

        if not isinstance(api_key, str):
            raise ValueError("api_key 必须是字符串")
        if len(api_key) > 16_384 or "\x00" in api_key:
            raise ValueError("API Key 长度或格式无效")
        config = load_config(self.config_path)
        candidate = copy.deepcopy(config.data)
        for section_name, supplied in (("ai", ai_config), ("http", http_config)):
            if supplied is None:
                continue
            if not isinstance(supplied, dict):
                raise ValueError(f"{section_name} 必须是 JSON 对象")
            if _contains_secret(supplied):
                raise ValueError(
                    f"临时 {section_name} 配置不能包含明文密钥；API Key 必须单独填写"
                )
            candidate[section_name] = copy.deepcopy(supplied)

        validate_config_data(candidate, require_ready=True)
        ai = candidate.get("ai", {})
        if not isinstance(ai, dict) or not ai.get("enabled", False):
            raise ValueError("请先启用 AI 分类")
        key_env = str(ai.get("api_key_env", "TENDER_AI_API_KEY")).strip()
        credential = self._resolve_ai_key(ai, api_key)
        if not credential:
            raise ValueError("请先填写或保存 API Key 后再测试连接")

        http = candidate.get("http", {})
        if not isinstance(http, dict):
            http = {}
        client = HttpClient(
            user_agent=effective_user_agent(http),
            timeout_seconds=float(ai.get("timeout_seconds", 60)),
            delay_seconds=0,
            max_retries=int(http.get("max_retries", 2)),
            max_response_bytes=int(float(http.get("max_response_mb", 25)) * 1024 * 1024),
            max_download_bytes=int(float(http.get("max_download_mb", 1024)) * 1024 * 1024),
            download_timeout_seconds=float(http.get("download_timeout_seconds", 900)),
            allow_private_hosts=bool(http.get("allow_private_hosts", False)),
            allow_benchmark_proxy_hosts=bool(
                http.get("allow_benchmark_proxy_hosts", False)
            ),
        )
        try:
            return test_ai_connection(client, ai, credential)
        finally:
            client.close()

    @staticmethod
    def _source_credentials(
        config_data: dict[str, Any], supplied: Any
    ) -> dict[str, dict[str, str]]:
        if supplied is None:
            supplied = {}
        if not isinstance(supplied, dict):
            raise ValueError("source_credentials 必须是 JSON 对象")
        expected: dict[str, str] = {}
        for source in config_data.get("sources", []):
            if not isinstance(source, dict) or not source.get("enabled", True):
                continue
            if source.get("type") != "custom_web":
                continue
            auth = source.get("auth", {})
            mode = auth.get("mode", "none") if isinstance(auth, dict) else "none"
            if mode in {"basic", "form"}:
                expected[str(source.get("id", ""))] = str(source.get("name", "自定义网站"))

        unknown = set(map(str, supplied)) - set(expected)
        if unknown:
            raise ValueError(f"收到未知自定义来源的登录信息: {', '.join(sorted(unknown))}")

        normalized: dict[str, dict[str, str]] = {}
        for source_id, source_name in expected.items():
            raw = supplied.get(source_id)
            if not isinstance(raw, dict):
                raise ValueError(f"请填写“{source_name}”的本次运行账号和密码")
            username = raw.get("username", "")
            password = raw.get("password", "")
            if not isinstance(username, str) or not isinstance(password, str):
                raise ValueError(f"“{source_name}”的账号和密码必须是字符串")
            if not username.strip() or not password:
                raise ValueError(f"请填写“{source_name}”的本次运行账号和密码")
            if len(username) > 512 or len(password) > 4096 or "\x00" in username + password:
                raise ValueError(f"“{source_name}”的登录信息长度或格式无效")
            normalized[source_id] = {"username": username, "password": password}
        return normalized

    def start(
        self,
        operation: str,
        api_key: str = "",
        source_credentials: Any = None,
    ) -> None:
        if not isinstance(api_key, str) or len(api_key) > 16_384 or "\x00" in api_key:
            raise ValueError("API Key 长度或格式无效")
        api_key = api_key.strip()
        config = load_config(self.config_path)
        if operation == "sample":
            config = sample_config(config)
        validate_config_data(config.data, require_ready=True)
        ai = config.data.get("ai", {})
        key_env = ""
        if operation == "run" and isinstance(ai, dict) and ai.get("enabled", False):
            key_env = str(ai.get("api_key_env", "TENDER_AI_API_KEY")).strip()
            api_key = self._resolve_ai_key(ai, api_key)
            if not api_key:
                raise ValueError("AI 已启用，请填写或保存模型 Key")
        credentials = (
            self._source_credentials(config.data, source_credentials)
            if operation == "run"
            else {}
        )
        browser_sources = (
            self._browser_sources(config.data)
            if operation == "run"
            else {}
        )
        browser_profile_ids = (
            self._browser_profile_ids(config.data)
            if operation == "run"
            else {}
        )
        browser_source_ids = list(browser_sources)
        browser_lease_token = secrets.token_urlsafe(24) if browser_source_ids else ""
        source_sessions = (
            self.browser_logins.lease_for_sources(
                browser_source_ids,
                expected_login_urls=browser_sources,
                expected_profile_ids=browser_profile_ids,
                lease_token=browser_lease_token,
            )
            if browser_sources
            else {}
        )
        try:
            with self._download_states_lock:
                if self._download_states:
                    raise AlreadyRunningError("原文件正在下载，请完成后再启动采集或校验")
                self.runner.start(
                    operation,
                    api_key=api_key,
                    api_key_env=key_env,
                    source_credentials=credentials,
                    source_sessions=source_sessions,
                    finished_callback=(
                        lambda: self.browser_logins.release_leases(
                            browser_source_ids,
                            restore=True,
                            lease_token=browser_lease_token,
                        )
                        if browser_source_ids
                        else None
                    ),
                )
        except Exception:
            if browser_source_ids:
                # Popen did not take ownership; keep the user's live login so a
                # corrected configuration can be retried without another CAPTCHA.
                self.browser_logins.release_leases(
                    browser_source_ids,
                    restore=True,
                    lease_token=browser_lease_token,
                )
            raise

    @staticmethod
    def _browser_sources(config_data: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for source in config_data.get("sources", []):
            if not isinstance(source, dict) or not source.get("enabled", True):
                continue
            if source.get("type") != "custom_web":
                continue
            auth = source.get("auth", {})
            if isinstance(auth, dict) and auth.get("mode", "none") == "browser":
                start_urls = source.get("start_urls", [])
                login_url = str(auth.get("login_url", "")).strip()
                if not login_url and isinstance(start_urls, list) and start_urls:
                    login_url = str(start_urls[0]).strip()
                result[str(source.get("id", ""))] = login_url
        return result

    @staticmethod
    def _browser_profile_ids(config_data: dict[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for source in config_data.get("sources", []):
            if not isinstance(source, dict) or not source.get("enabled", True):
                continue
            if source.get("type") != "custom_web":
                continue
            auth = source.get("auth", {})
            if isinstance(auth, dict) and auth.get("mode", "none") == "browser":
                source_id = str(source.get("id", ""))
                result[source_id] = str(auth.get("profile_id", "")).strip() or source_id
        return result

    def _browser_source(self, source_id: str) -> tuple[str, str, str, str]:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("source_id 不能为空")
        config = load_config(self.config_path)
        for source in config.data.get("sources", []):
            if not isinstance(source, dict) or not source.get("enabled", True):
                continue
            if source.get("type") != "custom_web" or source.get("id") != source_id:
                continue
            auth = source.get("auth", {})
            if not isinstance(auth, dict) or auth.get("mode", "none") != "browser":
                raise ValueError("该来源没有启用浏览器登录")
            start_urls = source.get("start_urls", [])
            login_url = str(auth.get("login_url", "")).strip()
            if not login_url and isinstance(start_urls, list) and start_urls:
                login_url = str(start_urls[0]).strip()
            if not login_url:
                raise ValueError("该来源没有可用的登录地址")
            check_url = str(auth.get("check_url", "")).strip()
            if not check_url and isinstance(start_urls, list) and start_urls:
                check_url = str(start_urls[0]).strip()
            if not check_url:
                check_url = login_url
            profile_id = str(auth.get("profile_id", "")).strip() or source_id
            return str(source.get("name", source_id)), login_url, check_url, profile_id
        raise ValueError("没有找到已启用的浏览器登录来源")

    def start_browser_login(self, source_id: str) -> dict[str, Any]:
        _, login_url, check_url, profile_id = self._browser_source(source_id)
        return self.browser_logins.start(
            source_id,
            login_url,
            profile_id=profile_id,
            check_url=check_url,
        )

    def complete_browser_login(self, source_id: str) -> dict[str, Any]:
        self._browser_source(source_id)
        return self.browser_logins.complete(source_id)

    def probe_browser_login(self, source_id: str) -> dict[str, Any]:
        self._browser_source(source_id)
        # An explicit UI probe is also the pre-run freshness check. It can move
        # an expired ready session back to waiting/challenge_required.
        return self.browser_logins.probe(source_id, force=True)

    def focus_browser_login(self, source_id: str) -> dict[str, Any]:
        _, login_url, check_url, profile_id = self._browser_source(source_id)
        return self.browser_logins.focus(
            source_id,
            login_url,
            profile_id=profile_id,
            check_url=check_url,
        )

    def cancel_browser_login(self, source_id: str) -> dict[str, Any]:
        self._browser_source(source_id)
        return self.browser_logins.cancel(source_id)

    def clear_browser_login(self, source_id: str) -> dict[str, Any]:
        _, login_url, _check_url, profile_id = self._browser_source(source_id)
        return self.browser_logins.clear(
            source_id, login_url, profile_id=profile_id
        )

    def browser_login_maintenance(self) -> dict[str, int]:
        config = load_config(self.config_path)
        return self.browser_logins.orphaned_profile_summary(
            self._browser_sources(config.data),
            profile_ids=self._browser_profile_ids(config.data),
        )

    def clear_orphaned_browser_logins(self) -> dict[str, int]:
        config = load_config(self.config_path)
        return self.browser_logins.clear_orphaned_profiles(
            self._browser_sources(config.data),
            profile_ids=self._browser_profile_ids(config.data),
        )

    def browser_login_checkpoint(self) -> dict[str, Any]:
        config = load_config(self.config_path)
        return self.browser_logins.checkpoint(
            self._browser_sources(config.data),
            profile_ids=self._browser_profile_ids(config.data),
        )

    def wait_browser_logins(self, timeout_seconds: Any = 20) -> dict[str, Any]:
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)):
            raise ValueError("timeout_seconds 必须是数字")
        timeout = float(timeout_seconds)
        if not 0 <= timeout <= 25:
            raise ValueError("timeout_seconds 必须在 0 到 25 之间")
        config = load_config(self.config_path)
        browser_sources = self._browser_sources(config.data)
        browser_profile_ids = self._browser_profile_ids(config.data)
        ready = self.browser_logins.wait_for_sources(
            list(browser_sources), timeout=timeout
        )
        return {
            "ready": ready,
            "checkpoint": self.browser_logins.checkpoint(
                browser_sources, profile_ids=browser_profile_ids
            ),
        }

    def open_output(self) -> Path:
        config = load_config(self.config_path)
        path = config.output_dir
        path.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], shell=False)
        else:
            subprocess.Popen(["xdg-open", str(path)], shell=False)
        return path

    @staticmethod
    def _query_value(params: dict[str, list[str]], name: str, default: str = "") -> str:
        values = params.get(name, [])
        return str(values[0]) if values else default

    def start_query(self, criteria: object, api_key: str = "", source_credentials: Any = None) -> dict:
        criteria = normalise_query(criteria)
        if not isinstance(api_key, str) or len(api_key) > 16384 or "\x00" in api_key:
            raise ValueError("API Key 长度或格式无效")
        config = load_config(self.config_path)
        query_id = secrets.token_hex(16)
        effective = query_config(config, criteria, query_id)
        validate_config_data(effective.data, require_ready=True)
        key_env = ""
        if criteria.get("mode") == "ai_recall":
            key_env = str(effective.data["ai"].get("api_key_env", "TENDER_AI_API_KEY")).strip()
            api_key = self._resolve_ai_key(effective.data["ai"], api_key)
            if not api_key:
                raise ValueError("AI 防漏检测需要可用模型，请先在 AI 分类中填写接口、模型并填写或保存 Key")
        specification = {**effective.data["_query"], "started_at": _now(),
                         "sources": [source_display_name(source) for source in
                                     effective.data["sources"] if source.get("enabled", True)]}
        access_config = {**effective.data, "sources": [source for source in effective.data["sources"]
            if not source.get("_query_unsupported")]}
        browser_sources = self._browser_sources(access_config)
        credentials = self._source_credentials(access_config, source_credentials)
        ids = list(browser_sources)
        lease_token = secrets.token_urlsafe(24) if ids else ""
        try:
            sessions = (self.browser_logins.lease_for_sources(ids, expected_login_urls=browser_sources,
                expected_profile_ids=self._browser_profile_ids(effective.data), lease_token=lease_token)
                if ids else {})
        except BrowserLoginError:
            # A logged-in OKCIS member session that merely hit the site's slider
            # challenge is answered automatically once before giving up; a real
            # login expiry still surfaces to the user.
            slider_ids = {sid: src for sid, src in ((s.get("id"), s) for s in access_config["sources"])
                          if sid in ids and src.get("adapter") == "okcis"}
            answered = [sid for sid in slider_ids
                        if self.browser_logins.answer_slider_challenge(sid, OKCIS_CAPTCHA_SOLVE)]
            if not answered:
                raise
            sessions = self.browser_logins.lease_for_sources(ids, expected_login_urls=browser_sources,
                expected_profile_ids=self._browser_profile_ids(effective.data), lease_token=lease_token,
                assume_ready=True)
        try:
            with self._download_states_lock:
                if self._download_states:
                    raise AlreadyRunningError("原文件正在下载，请完成后再启动新的查询")
                options = {"source_sessions": sessions, "finished_callback": lambda:
                    self.browser_logins.release_leases(ids, restore=True, lease_token=lease_token)} if ids else {}
                if credentials:
                    options["source_credentials"] = credentials
                self.runner.start("query", query_spec=specification, **options,
                                  **({"api_key": api_key.strip(), "api_key_env": key_env} if key_env else {}))
                path = query_state_path(config)
                path.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_json(path, specification)
        except Exception:
            if ids:
                self.browser_logins.release_leases(ids, restore=True, lease_token=lease_token)
            raise
        return specification

    def query_status(self) -> dict:
        config = load_config(self.config_path)
        path = query_state_path(config)
        if not path.exists():
            return {"query": None, "sources": []}
        specification = json.loads(path.read_text(encoding="utf-8"))
        db = Database(config.database_path)
        try:
            rows = [dict(row) for row in db.connection.execute(
                "SELECT * FROM coverage WHERE run_id=?", (specification["id"],)).fetchall()]
        finally:
            db.close()
        known = {row["source"]: row for row in rows}
        # Also read query descriptors written before source display names were saved.
        source_names = {source["type"]: source_display_name(source) for source in
                        config.data["sources"]}
        running = self.runner.status().get("running", False)
        sources = [known.get(source_names.get(name, name), {
            "source": source_names.get(name, name), "status": "queued" if running else "not_started",
            "notices": 0, "pages": 0}) for name in specification["sources"]]
        if not running:
            for source in sources:
                if source["status"] in {"running", "waiting_verification"}:
                    source["status"] = "interrupted"
        return {"query": specification, "sources": sources}

    def list_notices(self, params: dict[str, list[str]]) -> dict[str, Any]:
        try:
            page = int(self._query_value(params, "page", "1"))
            page_size = int(self._query_value(params, "page_size", "50"))
        except ValueError as exc:
            raise ValueError("page 和 page_size 必须是整数") from exc
        config = load_config(self.config_path)
        db = Database(config.database_path)
        try:
            result = db.list_notices(
                page=page,
                page_size=page_size,
                query=self._query_value(params, "query"),
                notice_type=self._query_value(params, "notice_type"),
                industry=self._query_value(params, "industry"),
                category=self._query_value(params, "category"),
                city=self._query_value(params, "city"),
                status=self._query_value(params, "status"),
                download_status=self._query_value(params, "download_status"),
                source=self._query_value(params, "source"),
                date_from=self._query_value(params, "date_from"),
                date_to=self._query_value(params, "date_to"),
                relevance=self._query_value(params, "relevance", "all"),
                query_id=self._query_value(params, "query_id"),
            )
            with self._download_states_lock:
                for item in result["items"]:
                    state = self._download_states.get(item["identity"])
                    if state:
                        item["download_status"] = state
            return result
        finally:
            db.close()

    def export_notices(self, params: dict[str, list[str]], notice_ids: list[str] | None = None) -> bytes:
        filtered = {key: list(value) for key, value in params.items()}
        filtered["page_size"] = ["200"]
        filtered["page"] = ["1"]
        first = self.list_notices(filtered)
        items = list(first["items"])
        for page in range(2, int(first["pages"]) + 1):
            filtered["page"] = [str(page)]
            items.extend(self.list_notices(filtered)["items"])

        if notice_ids is not None:
            selected = set(notice_ids)
            items = [item for item in items if item.get("notice_id") in selected]
        if not items:
            raise ValueError("本次没有可导出的结果。请先查询平台并检查查询条件及各平台状态。")
        config = load_config(self.config_path)
        db = Database(config.database_path)
        try:
            link_rows = csv.DictReader(io.StringIO(download_links_csv(
                db, [item["notice_id"] for item in items]).decode("utf-8-sig")))
            links_by_notice: dict[str, list[dict]] = {}
            for link in link_rows:
                links_by_notice.setdefault(link["标讯ID"], []).append(link)
        finally:
            db.close()

        fields = (
            "公告时间", "数据标题", "公告类别", "客户名称", "一级行业标签", "城市",
            "客户联系人", "客户电话", "中标单位", "中标单位联系人-企业公示",
            "中标单位电话-企业公示", "项目标签", "公告页面缓存", "项目信息",
            "金额", "金额类型", "来源", "AI状态", "下载状态", "标讯ID", "原公告URL",
            "文件名称", "下载URL",
            "AI需求复核", "AI复核说明",
            "链接类型", "链接说明", "访问条件",
        )
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()

        def safe(value: object) -> object:
            if isinstance(value, (list, tuple)):
                value = " | ".join(str(item) for item in value)
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
                return "'" + value
            return value

        for item in items:
            review = item.get("query_review") or {}
            minor = item.get("amount_minor")
            if minor is None:
                amount = ""
            else:
                minor_value = int(minor)
                sign = "-" if minor_value < 0 else ""
                yuan, cents = divmod(abs(minor_value), 100)
                amount = f"{sign}{yuan}.{cents:02d}"
            row = {
                "公告时间": item.get("published_at", ""),
                "数据标题": item.get("title", ""),
                "公告类别": item.get("notice_type", ""),
                "客户名称": item.get("buyer", ""),
                "一级行业标签": item.get("industry", ""),
                "AI需求复核": {"match": "AI 判断匹配", "suspect": "待人工复核",
                    "no_match": "AI 判断不匹配", "error": "AI 失败待复核",
                    "unread": "正文缺失待复核", "not_run": "AI 未执行待复核"}.get(review.get("status"), "未启用"),
                "AI复核说明": review.get("reason", ""),
                "城市": item.get("city", ""),
                "客户联系人": item.get("buyer_contact", ""),
                "客户电话": item.get("buyer_phone", ""),
                "中标单位": item.get("winning_vendor", ""),
                "中标单位联系人-企业公示": item.get("winning_vendor_contact", ""),
                "中标单位电话-企业公示": item.get("winning_vendor_phone", ""),
                "项目标签": item.get("project_tags", []),
                "公告页面缓存": item.get("original_notice_url", ""),
                "项目信息": item.get("project_summary", ""),
                "金额": amount,
                "金额类型": {
                    "contract": "合同",
                    "award": "中标成交",
                    "budget": "预算",
                    "max_price": "最高限价",
                    "intention": "采购意向",
                }.get(str(item.get("amount_type", "")), ""),
                "来源": item.get("source", ""),
                "AI状态": item.get("ai_status", ""),
                "下载状态": item.get("download_status", ""),
                "标讯ID": item.get("notice_id", ""),
                "原公告URL": item.get("original_notice_url", ""),
            }
            # One row per file makes every URL individually copyable in a sheet.
            # A notice with no direct attachment remains visible with its original
            # page and an explicit explanation, never a fake file URL.
            for link in links_by_notice.get(str(item.get("notice_id")), [{}]):
                row.update({
                    "文件名称": link.get("文件名称", ""),
                    "下载URL": link.get("下载URL", ""),
                    "链接类型": link.get("链接类型", "公告入口"),
                    "链接说明": link.get("失败或待办原因", ""),
                    "访问条件": link.get("访问条件", ""),
                })
                writer.writerow({key: safe(value) for key, value in row.items()})
        return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")

    def download_notices(
        self,
        notice_ids: list[str],
        *,
        include_notice: bool = True,
        include_attachments: bool = True,
        source_credentials: Any = None,
    ) -> list[dict[str, object]]:
        with self._download_states_lock:
            if any(identity in self._download_states for identity in notice_ids):
                raise AlreadyRunningError("所选公告已在下载队列中，请勿重复提交")
            self._check_download_allowed(notice_ids)
            self._download_states.update(dict.fromkeys(notice_ids, "queued"))
        try:
            with self._download_lock:
                with self._download_states_lock:
                    self._check_download_allowed(notice_ids)
                    self._download_states.update(dict.fromkeys(notice_ids, "downloading"))
                return self._download_notices_locked(
                    notice_ids,
                    include_notice=include_notice,
                    include_attachments=include_attachments,
                    source_credentials=source_credentials,
                )
        finally:
            with self._download_states_lock:
                for identity in notice_ids:
                    self._download_states.pop(identity, None)

    def _check_download_allowed(self, notice_ids: list[str]) -> None:
        status = self.runner.status()
        if not status.get("running"):
            return
        if status.get("operation") != "query":
            raise AlreadyRunningError("采集或校验任务正在运行，请完成后再下载原件")
        config = load_config(self.config_path)
        path = query_state_path(config)
        query_id = json.loads(path.read_text(encoding="utf-8")).get("id") if path.exists() else None
        db = Database(config.database_path)
        try:
            for identity in notice_ids:
                notice = db.get_notice(identity)
                if not query_id or notice is None or notice.metadata.get("query_id") != query_id:
                    raise AlreadyRunningError("采集中只能下载本轮已完成解析、已显示在清单中的公告")
        finally:
            db.close()

    def export_download_links(self, notice_ids: list[str] | None = None) -> bytes:
        config = load_config(self.config_path)
        db = Database(config.database_path)
        try:
            body = download_links_csv(db, notice_ids)
            if not list(csv.DictReader(io.StringIO(body.decode("utf-8-sig")))):
                raise ValueError("本次没有可导出的链接。请先查询平台并选择结果。")
            return body
        finally:
            db.close()

    def save_export(self, body: bytes, name: str) -> None:
        directory = load_config(self.config_path).output_dir / "exports"
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (directory / f"{name}_{stamp}.csv").write_bytes(body)

    def _download_notices_locked(
        self,
        notice_ids: list[str],
        *,
        include_notice: bool,
        include_attachments: bool,
        source_credentials: Any = None,
    ) -> list[dict[str, object]]:
        config = load_config(self.config_path)
        http = config.data.get("http", {})
        client = HttpClient(
            user_agent=effective_user_agent(http),
            timeout_seconds=float(http.get("timeout_seconds", 35)),
            delay_seconds=float(http.get("delay_seconds", 3)),
            max_retries=int(http.get("max_retries", 2)),
            max_response_bytes=int(float(http.get("max_response_mb", 25)) * 1024 * 1024),
            max_download_bytes=int(float(http.get("max_download_mb", 1024)) * 1024 * 1024),
            download_timeout_seconds=float(http.get("download_timeout_seconds", 900)),
            allow_private_hosts=bool(http.get("allow_private_hosts", False)),
            allow_benchmark_proxy_hosts=bool(http.get("allow_benchmark_proxy_hosts", False)),
        )
        db = Database(config.database_path)
        sources = []
        leases: list[tuple[str, str]] = []
        try:
            sources = build_sources(client, config.data["sources"], config.path.parent)
            selected_names = {notice.source for identity in notice_ids
                              if (notice := db.get_notice(identity)) is not None}
            errors: dict[str, str] = {}
            for source in sources:
                if source.name not in selected_names or not isinstance(source, CustomWebSource):
                    continue
                if source.auth_mode == "browser":
                    token = secrets.token_urlsafe(24)
                    subset = {"sources": [source.config]}
                    try:
                        sessions = self.browser_logins.lease_for_sources(
                            [source.source_id],
                            expected_login_urls=self._browser_sources(subset),
                            expected_profile_ids=self._browser_profile_ids(subset),
                            lease_token=token,
                        )
                        leases.append((source.source_id, token))
                        source.runtime_browser_session = sessions[source.source_id]
                    except BrowserLoginError as exc:
                        errors[source.name] = str(exc)
                elif source.auth_mode in {"basic", "form"}:
                    try:
                        credentials = self._source_credentials(
                            {"sources": [source.config]}, source_credentials or {},
                        )
                        source.runtime_credentials = credentials.get(source.source_id)
                    except ValueError as exc:
                        errors[source.name] = str(exc)
            pipeline = Pipeline(
                config=config,
                client=client,
                db=db,
                store=ImmutableStore(config.output_dir),
                classifier=RuleClassifier(),
                sources=sources,
            )
            pipeline.download_source_errors = errors
            return pipeline.download_notices(
                notice_ids,
                include_notice=include_notice,
                include_attachments=include_attachments,
            )
        finally:
            for source in sources:
                source.close()
            for source_id, token in leases:
                self.browser_logins.release_leases([source_id], restore=True, lease_token=token)
            db.close()
            client.close()

    def close(self) -> None:
        # Stop/wait for the child before revoking the live CDP capability it may
        # still be using. The watcher normally releases the lease; the manager
        # close below is the idempotent crash/shutdown fallback.
        self.runner.close()
        self.browser_logins.close()


class LocalThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # POSIX SO_REUSEADDR permits restart after TIME_WAIT, without REUSEPORT.
    # Windows instead requires exclusive ownership to prevent two listeners.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()

    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        app: ConfigWebApp,
    ) -> None:
        self.app = app
        super().__init__(server_address, handler_class)

    def server_close(self) -> None:
        self.app.close()
        super().server_close()


class WebUIHandler(BaseHTTPRequestHandler):
    server: LocalThreadingHTTPServer
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        # Avoid printing the CSRF token or request data. Normal run output is
        # already visible in the UI, so the local access log is intentionally quiet.
        return

    def _security_headers(self, *, no_store: bool = True) -> None:
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'",
        )
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        if no_store:
            self.send_header("Cache-Control", "no-store")

    def _send_bytes(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        no_store: bool = True,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self._security_headers(no_store=no_store)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if not head_only:
            self.wfile.write(body)

    def _send_json(self, status: int, payload: Any, *, head_only: bool = False) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send_bytes(
            status,
            body,
            "application/json; charset=utf-8",
            head_only=head_only,
        )

    def _local_host_ok(self) -> bool:
        value = self.headers.get("Host", "")
        try:
            hostname = (urlsplit(f"//{value}").hostname or "").lower()
        except ValueError:
            return False
        return hostname in {"127.0.0.1", "localhost", "::1"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlsplit(origin)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return False
        return (
            parsed.scheme == "http"
            and hostname in {"127.0.0.1", "localhost", "::1"}
            and port == int(self.server.server_address[1])
        )

    def _check_request(self, *, mutation: bool = False) -> None:
        if not self._local_host_ok():
            raise ApiError(HTTPStatus.BAD_REQUEST, "拒绝非本机 Host")
        if not mutation:
            return
        if not self._origin_ok():
            raise ApiError(HTTPStatus.FORBIDDEN, "拒绝跨站请求")
        supplied = self.headers.get("X-CSRF-Token", "")
        if not supplied or not hmac.compare_digest(supplied, self.server.app.csrf_token):
            raise ApiError(HTTPStatus.FORBIDDEN, "会话校验失败，请刷新页面后重试")
        content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from exc
        if content_length > 0 and content_type != "application/json":
            raise ApiError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "请求必须使用 application/json")

    def _read_json(self) -> Any:
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(HTTPStatus.BAD_REQUEST, "不支持分块请求体")
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Content-Length 无效") from exc
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大")
        if not length:
            return {}
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "JSON 格式无效") from exc

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._send_json(int(exc.status), {"ok": False, "error": exc.message})
        elif isinstance(exc, AlreadyRunningError):
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
        elif isinstance(exc, BrowserLoginBusyError):
            self._send_json(HTTPStatus.CONFLICT, {"ok": False, "error": str(exc)})
        elif isinstance(exc, BrowserLoginError):
            self._send_json(
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"ok": False, "error": str(exc)},
            )
        elif isinstance(exc, (ValueError, FileNotFoundError, json.JSONDecodeError)):
            self._send_json(HTTPStatus.UNPROCESSABLE_ENTITY, {"ok": False, "error": str(exc)})
        else:
            self._send_json(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"ok": False, "error": "本地服务处理失败，请查看启动窗口"},
            )

    def do_HEAD(self) -> None:  # noqa: N802
        try:
            self._check_request()
            self._handle_get(head_only=True)
        except Exception as exc:
            self._handle_error(exc)

    def do_GET(self) -> None:  # noqa: N802
        try:
            self._check_request()
            self._handle_get(head_only=False)
        except Exception as exc:
            self._handle_error(exc)

    def _handle_get(self, *, head_only: bool) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        if path == "/api/config":
            self._send_json(HTTPStatus.OK, self.server.app.read_config(), head_only=head_only)
            return
        if path == "/api/ai-presets":
            from ..ai_presets import AI_PRESETS

            self._send_json(
                HTTPStatus.OK,
                {"presets": AI_PRESETS},
                head_only=head_only,
            )
            return
        if path == "/api/status":
            params = parse_qs(parsed.query)
            try:
                after = max(0, int(params.get("after", ["0"])[0]))
            except ValueError:
                after = 0
            self._send_json(
                HTTPStatus.OK,
                self.server.app.status(after=after),
                head_only=head_only,
            )
            return
        if path == "/api/regions":
            from ..geography import region_payload
            self._send_json(HTTPStatus.OK, region_payload(), head_only=head_only)
            return
        if path == "/api/query":
            self._send_json(HTTPStatus.OK, self.server.app.query_status(), head_only=head_only)
            return
        if path == "/api/notices":
            params = parse_qs(parsed.query)
            self._send_json(
                HTTPStatus.OK,
                self.server.app.list_notices(params),
                head_only=head_only,
            )
            return
        if path == "/api/notices/export":
            params = parse_qs(parsed.query)
            body = self.server.app.export_notices(params)
            self._send_bytes(
                HTTPStatus.OK,
                body,
                "text/csv; charset=utf-8",
                head_only=head_only,
                extra_headers={
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''tender-catalog.csv"
                    )
                },
            )
            return
        if path == "/api/notices/links":
            self._send_bytes(
                HTTPStatus.OK, self.server.app.export_download_links(),
                "text/csv; charset=utf-8", head_only=head_only,
                extra_headers={"Content-Disposition": "attachment; filename=download_links.csv"},
            )
            return
        if path == "/api/browser-login/status":
            checkpoint = self.server.app.browser_login_checkpoint()
            self._send_json(
                HTTPStatus.OK,
                {
                    "sessions": self.server.app.browser_logins.status(auto_probe=False),
                    "checkpoint": checkpoint,
                    "maintenance": self.server.app.browser_login_maintenance(),
                },
                head_only=head_only,
            )
            return
        if path == "/api/browser-login/checkpoint":
            self._send_json(
                HTTPStatus.OK,
                {"checkpoint": self.server.app.browser_login_checkpoint()},
                head_only=head_only,
            )
            return
        static = STATIC_FILES.get(path)
        if static is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "页面不存在")
        name, content_type = static
        file_path = self.server.app.static_dir / name
        body = file_path.read_bytes()
        if name == "index.html":
            body = body.replace(
                b"__CSRF_TOKEN__",
                self.server.app.csrf_token.encode("ascii"),
            )
        self._send_bytes(
            HTTPStatus.OK,
            body,
            content_type,
            # This UI is a loopback-only configuration tool and its JavaScript
            # changes together with the Python backend.  Keeping an older
            # app.js in the browser cache can resurrect fixed login/run-state
            # bugs after a service restart, so all bundled assets are served
            # as no-store.
            no_store=True,
            head_only=head_only,
        )

    def do_PUT(self) -> None:  # noqa: N802
        try:
            self._check_request(mutation=True)
            parsed = urlsplit(self.path)
            if parsed.path != "/api/config":
                raise ApiError(HTTPStatus.NOT_FOUND, "接口不存在")
            payload = self._read_json()
            config_data = payload.get("config") if isinstance(payload, dict) and "config" in payload else payload
            saved = self.server.app.save_config(config_data)
            self._send_json(
                HTTPStatus.OK,
                {"ok": True, "message": "配置已安全保存", "config": saved},
            )
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        try:
            self._check_request(mutation=True)
            path = urlsplit(self.path).path
            payload = self._read_json()
            if not isinstance(payload, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "请求必须是 JSON 对象")

            if path == "/api/validate":
                candidate = payload.get("config") if "config" in payload else None
                result = self.server.app.validate_config(candidate)
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "message": "配置校验通过", **result},
                )
                return
            if path == "/api/browser-login/clear-orphans":
                result = self.server.app.clear_orphaned_browser_logins()
                removed = int(result.get("removed_count", 0))
                failed = int(result.get("failed_count", 0))
                message = f"已清理 {removed} 项已移除网站的持久登录数据"
                if failed:
                    message += f"；另有 {failed} 项未能清理，请关闭相关浏览器后重试"
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": failed == 0, "message": message, "maintenance": result},
                )
                return
            if path in {
                "/api/browser-login/start",
                "/api/browser-login/complete",
                "/api/browser-login/probe",
                "/api/browser-login/focus",
                "/api/browser-login/cancel",
                "/api/browser-login/clear",
            }:
                source_id = payload.get("source_id", "")
                if not isinstance(source_id, str) or not source_id.strip():
                    raise ApiError(HTTPStatus.BAD_REQUEST, "source_id 必须是非空字符串")
                if path.endswith("/start"):
                    session = self.server.app.start_browser_login(source_id.strip())
                    status = HTTPStatus.ACCEPTED
                    message = "登录窗口已打开；完成登录或验证码后会自动检测"
                elif path.endswith("/complete"):
                    session = self.server.app.complete_browser_login(source_id.strip())
                    status = HTTPStatus.OK
                    message = "登录状态已捕获，可开始采集"
                elif path.endswith("/probe"):
                    session = self.server.app.probe_browser_login(source_id.strip())
                    status = HTTPStatus.OK
                    message = "已检查登录窗口；不会自动填写或提交验证码"
                elif path.endswith("/focus"):
                    session = self.server.app.focus_browser_login(source_id.strip())
                    status = HTTPStatus.OK
                    message = "已切换到登录或验证窗口"
                elif path.endswith("/clear"):
                    session = self.server.app.clear_browser_login(source_id.strip())
                    status = HTTPStatus.OK
                    message = "已清除该来源和登录域名的持久浏览器数据"
                else:
                    session = self.server.app.cancel_browser_login(source_id.strip())
                    status = HTTPStatus.OK
                    message = "登录窗口已关闭，持久登录数据已保留"
                self._send_json(
                    status,
                    {"ok": True, "message": message, "session": session},
                )
                return
            if path in {"/api/ai-models", "/api/ai-key/status", "/api/ai-key/save", "/api/ai-key/delete"}:
                app = self.server.app
                ai, http = payload.get("ai"), payload.get("http")
                if path == "/api/ai-models":
                    result = app.fetch_ai_models(payload.get("api_key", ""), ai, http)
                elif path.endswith("/save"):
                    result = app.save_ai_key(payload.get("api_key", ""), ai, http)
                elif path.endswith("/delete"):
                    result = app.delete_ai_key(ai, http)
                else:
                    result = app.ai_key_status(ai, http)
                self._send_json(HTTPStatus.OK if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY, result)
                return
            if path == "/api/ai-test":
                api_key = payload.get("api_key", "")
                if not isinstance(api_key, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "api_key 必须是字符串")
                draft = payload.get("config") if "config" in payload else None
                if draft is not None and not isinstance(draft, dict):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "config 必须是 JSON 对象")
                ai_config = (
                    payload.get("ai")
                    if "ai" in payload
                    else draft.get("ai") if isinstance(draft, dict) and "ai" in draft else None
                )
                http_config = (
                    payload.get("http")
                    if "http" in payload
                    else draft.get("http")
                    if isinstance(draft, dict) and "http" in draft
                    else None
                )
                result = self.server.app.test_ai(
                    api_key,
                    ai_config=ai_config,
                    http_config=http_config,
                )
                self._send_json(
                    HTTPStatus.OK if result.get("ok") else HTTPStatus.UNPROCESSABLE_ENTITY,
                    result,
                )
                return
            if path == "/api/browser-login/wait":
                result = self.server.app.wait_browser_logins(
                    payload.get("timeout_seconds", 20)
                )
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, **result},
                )
                return
            if path == "/api/query":
                specification = self.server.app.start_query(
                    payload.get("criteria", payload),
                    **({"source_credentials": payload["source_credentials"]} if "source_credentials" in payload else {}),
                    **({"api_key": payload["api_key"]} if "api_key" in payload else {}))
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "query": specification})
                return
            if path == "/api/sample":
                self.server.app.start("sample")
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "message": "已开始试采，最多10条，不使用AI、不修改原采集范围。"})
                return
            if path == "/api/run":
                api_key = payload.get("api_key", "")
                if not isinstance(api_key, str):
                    raise ApiError(HTTPStatus.BAD_REQUEST, "api_key 必须是字符串")
                self.server.app.start(
                    "run",
                    api_key=api_key,
                    source_credentials=payload.get("source_credentials", {}),
                )
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "message": "采集任务已启动"})
                return
            if path in {"/api/notices/links", "/api/notices/export"}:
                notice_ids = payload.get("notice_ids")
                if notice_ids is not None and (
                    not isinstance(notice_ids, list) or len(notice_ids) > 10000
                    or any(not isinstance(value, str) or not value or len(value) > 512 for value in notice_ids)
                ):
                    raise ApiError(HTTPStatus.UNPROCESSABLE_ENTITY, "notice_ids 必须是标讯 ID 数组")
                params = {}
                query_id = payload.get("query_id")
                if query_id is not None:
                    current = self.server.app.query_status()["query"]
                    if not current or query_id != current["id"]:
                        raise ValueError("查询结果已更新，请刷新后重新选择导出。")
                    params = {"query_id": [query_id]}
                    # Membership is checked on the server too, so an old tab cannot
                    # export sample records or records from another request.
                    allowed = set()
                    page = 1
                    while True:
                        result = self.server.app.list_notices({**params, "page_size": ["200"], "page": [str(page)]})
                        allowed.update(item["notice_id"] for item in result["items"])
                        if page >= result["pages"]:
                            break
                        page += 1
                    notice_ids = sorted(allowed if notice_ids is None else allowed.intersection(notice_ids))
                    if not notice_ids:
                        raise ValueError("本次没有可导出的结果，请重新查询并选择。")
                links = path.endswith("/links")
                body = (self.server.app.export_download_links(notice_ids) if links
                        else self.server.app.export_notices(params, notice_ids))
                self.server.app.save_export(body, "标书下载链接" if links else "标讯信息及下载URL")
                exported_rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
                self._send_bytes(
                    HTTPStatus.OK, body,
                    "text/csv; charset=utf-8",
                    extra_headers={
                        "Content-Disposition": "attachment; filename=" + ("download_links.csv" if links else "notices.csv"),
                        "X-Export-Notices": str(len({row["标讯ID"] for row in exported_rows})),
                        "X-Export-Rows": str(len(exported_rows)),
                        "X-Export-URL-Rows": str(sum(bool(row.get("下载URL")) for row in exported_rows)),
                    },
                )
                return
            if path == "/api/notices/download":
                raw_ids = payload.get("notice_ids", payload.get("identities"))
                if not isinstance(raw_ids, list):
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "notice_ids 必须是非空字符串数组",
                    )
                if not raw_ids or len(raw_ids) > 200:
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "每次请选择 1 到 200 条标讯",
                    )
                if any(
                    not isinstance(value, str)
                    or not value.strip()
                    or len(value) > 512
                    or "\x00" in value
                    for value in raw_ids
                ):
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "notice_ids 中每一项必须是有效字符串",
                    )
                notice_ids = list(dict.fromkeys(value.strip() for value in raw_ids))
                include_notice = payload.get("include_notice", True)
                include_attachments = payload.get("include_attachments", True)
                if not isinstance(include_notice, bool) or not isinstance(include_attachments, bool):
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "include_notice 和 include_attachments 必须是布尔值",
                    )
                if not include_notice and not include_attachments:
                    raise ApiError(
                        HTTPStatus.UNPROCESSABLE_ENTITY,
                        "原公告和附件至少选择一项",
                    )
                items = self.server.app.download_notices(
                    notice_ids,
                    include_notice=include_notice,
                    include_attachments=include_attachments,
                    **({"source_credentials": payload["source_credentials"]}
                       if "source_credentials" in payload else {}),
                )
                counts = {
                    state: sum(item.get("status") == state for item in items)
                    for state in ("downloaded", "partial", "failed")
                }
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": counts["failed"] == 0 and counts["partial"] == 0,
                        "items": items,
                        "summary": counts,
                    },
                )
                return
            if path == "/api/verify":
                self.server.app.start("verify")
                self._send_json(HTTPStatus.ACCEPTED, {"ok": True, "message": "原件校验已启动"})
                return
            if path == "/api/stop":
                stopped = self.server.app.runner.stop()
                self._send_json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "message": "正在停止任务" if stopped else "当前没有运行中的任务",
                    },
                )
                return
            if path == "/api/open-output":
                opened = self.server.app.open_output()
                self._send_json(
                    HTTPStatus.OK,
                    {"ok": True, "message": "已打开输出目录", "path": str(opened)},
                )
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "接口不存在")
        except Exception as exc:
            self._handle_error(exc)


def create_server(
    config_path: str | Path,
    *,
    port: int = 0,
    browser_login_manager: BrowserLoginManager | None = None,
) -> tuple[LocalThreadingHTTPServer, ConfigWebApp]:
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port <= 65_535:
        raise ValueError("端口必须是 0 到 65535 的整数")
    app = ConfigWebApp(
        config_path,
        browser_login_manager=browser_login_manager,
    )
    try:
        server = LocalThreadingHTTPServer(("127.0.0.1", port), WebUIHandler, app)
    except OSError as exc:
        app.close()
        address_in_use = exc.errno == errno.EADDRINUSE or getattr(
            exc, "winerror", None
        ) == 10_048
        if address_in_use:
            raise WebUIAlreadyRunningError(
                f"本地配置界面端口 {port} 已被占用。"
                "如果界面已经打开，请直接使用原窗口；"
                "需要加载新版本时，请先关闭旧的启动窗口再重试。"
            ) from None
        raise
    return server, app


def serve_config_ui(
    config_path: str | Path,
    *,
    port: int = 8_765,
    open_browser: bool = True,
) -> None:
    path = Path(config_path).resolve()
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")
    try:
        server, _ = create_server(path, port=port)
    except WebUIAlreadyRunningError as exc:
        # Treat an existing dashboard as an expected startup condition.  Using
        # SystemExit keeps the CLI from printing a frightening Python traceback
        # while still returning a non-zero code to start-ui.cmd.
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    actual_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{actual_port}/"
    print(f"本地配置台已启动：{url}")
    print(f"配置文件：{path}")
    print("仅监听本机地址；关闭此窗口或按 Ctrl+C 可退出。")
    if open_browser:
        threading.Timer(0.4, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n正在关闭本地配置台……")
    finally:
        server.shutdown()
        server.server_close()
