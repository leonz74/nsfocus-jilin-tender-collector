from __future__ import annotations

import json
import copy
import math
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .official_registry import UrlPolicyViolation, trusted_configured_official_host


KNOWN_SOURCE_TYPES = {
    "jilin_ggzy",
    "ccgp_search",
    "ccgp_archive",
    "national_ggzy",
    "url_seed",
    "custom_web",
}
DEFAULT_USER_AGENT = "JilinTenderDownloader/0.1"


def sample_config(config):
    """A bounded public-site trial; keep the saved configuration unchanged."""
    data = copy.deepcopy(config.data)
    source = next((s for s in data.get("sources", []) if s.get("type") == "jilin_ggzy"),
                  {"type": "jilin_ggzy", "authority_rank": 100})
    source.update(enabled=True, page_size=10, max_pages_per_channel=1,
                  channels=[{"trade_type": "政府采购", "info_type": "采购公告"}])
    data["sources"] = [source]
    data.setdefault("ai", {})["enabled"] = False
    data.setdefault("delivery", {})["mode"] = "on_demand"
    data.setdefault("recall", {})["mode"] = "p0_complete"
    return AppConfig(config.path, data)

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")
RESERVED_ENV_NAMES = {
    "PATH", "PYTHONPATH", "PYTHONHOME", "SYSTEMROOT", "COMSPEC", "PATHEXT",
    "HOME", "USERPROFILE", "TEMP", "TMP",
}
SECRET_QUERY_NAMES = {"key", "api_key", "apikey", "token", "access_token", "password"}


def _has_secret_query(url: str) -> bool:
    return any(name.lower() in SECRET_QUERY_NAMES for name, _ in parse_qsl(urlsplit(url).query))


def _number(
    value: Any,
    name: str,
    *,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ValueError(f"{name} 必须在 {minimum:g} 到 {maximum:g} 之间")
    return number


def _integer(value: Any, name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} 必须在 {minimum} 到 {maximum} 之间")
    return value


def validate_config_data(data: Any, *, require_ready: bool = False) -> None:
    if not isinstance(data, dict):
        raise ValueError("配置根节点必须是 JSON 对象")
    required = ("start_date", "end_date", "sources")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"配置缺少字段: {', '.join(missing)}")
    try:
        start = date.fromisoformat(str(data["start_date"]))
        end = date.fromisoformat(str(data["end_date"]))
    except ValueError as exc:
        raise ValueError("start_date/end_date 必须是 YYYY-MM-DD") from exc
    if start > end:
        raise ValueError("start_date 不能晚于 end_date")

    for name in ("output_dir", "database"):
        if name in data and (not isinstance(data[name], str) or not data[name].strip()):
            raise ValueError(f"{name} 必须是非空路径")

    sources = data["sources"]
    if not isinstance(sources, list) or not sources:
        raise ValueError("sources 必须是非空数组")
    enabled_count = 0
    source_names: set[str] = set()
    source_ids: set[str] = set()
    browser_profile_ids: set[str] = set()
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"第 {index} 个来源必须是 JSON 对象")
        source_type = source.get("type")
        if source_type not in KNOWN_SOURCE_TYPES:
            raise ValueError(f"未知数据源类型: {source_type}")
        source_role = source.get("source_role", "auto")
        if source_role not in {
            "auto", "official", "commercial_lead"
        }:
            raise ValueError(
                f"来源 {source_type} 的 source_role 必须是 auto、official 或 commercial_lead"
            )
        enabled = source.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"来源 {source_type} 的 enabled 必须是布尔值")
        enabled_count += int(enabled)
        _number(
            source.get("authority_rank", 50),
            f"来源 {source_type} 的 authority_rank",
            minimum=0,
            maximum=100,
        )
        source_name = str(source.get("name", source_type)).strip()
        if not source_name:
            raise ValueError(f"来源 {source_type} 的 name 不能为空")
        if enabled and source_name in source_names:
            raise ValueError(f"启用的数据源名称不能重复: {source_name}")
        if enabled:
            source_names.add(source_name)
        numeric_fields = {
            "page_size": (1, 100),
            "max_pages_per_channel": (1, 10_000),
            "max_pages_per_query": (1, 10_000),
            "max_pages_per_category": (1, 10_000),
            "max_query_pages_per_category": (1, 10_000),
            "max_pages": (1, 10_000),
            "verification_wait_seconds": (10, 900),
            "max_pages_per_window": (1, 10_000),
            "window_days": (1, 366),
        }
        for key, (minimum, maximum) in numeric_fields.items():
            if key in source:
                _integer(
                    source[key],
                    f"来源 {source_type} 的 {key}",
                    minimum=minimum,
                    maximum=maximum,
                )
        if "keywords" in source:
            keywords = source["keywords"]
            if not isinstance(keywords, list) or not keywords or not all(
                isinstance(item, str) and item.strip() for item in keywords
            ):
                raise ValueError(f"来源 {source_type} 的 keywords 必须是非空字符串数组")
        if source_type == "custom_web":
            source_id = str(source.get("id", "")).strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,63}", source_id):
                raise ValueError(
                    "自定义网站 id 必须为 3-64 位字母、数字、点、下划线或连字符"
                )
            if source_id in source_ids:
                raise ValueError(f"自定义网站 id 不能重复: {source_id}")
            source_ids.add(source_id)
            required_terms = source.get("required_terms", [])
            if not isinstance(required_terms, list) or not all(
                isinstance(item, str) and item.strip() for item in required_terms
            ):
                raise ValueError(
                    f"自定义网站 {source_name} 的 required_terms 必须是字符串数组"
                )
            start_urls = source.get("start_urls")
            if not isinstance(start_urls, list) or not 1 <= len(start_urls) <= 100:
                raise ValueError(f"自定义网站 {source_name} 必须填写 1-100 个网址")
            start_origins: set[tuple[str, str, int]] = set()
            for url in start_urls:
                if not isinstance(url, str) or not url.strip().startswith(("https://", "http://")):
                    raise ValueError(f"自定义网站 {source_name} 的网址必须以 http(s):// 开头")
                parsed_url = urlsplit(url.strip())
                if not parsed_url.hostname or parsed_url.username or parsed_url.password:
                    raise ValueError(f"自定义网站 {source_name} 的网址无效或含账号密码")
                try:
                    start_port = parsed_url.port or (
                        443 if parsed_url.scheme.lower() == "https" else 80
                    )
                except ValueError as exc:
                    raise ValueError(f"自定义网站 {source_name} 的网址端口无效") from exc
                if _has_secret_query(url.strip()):
                    raise ValueError(f"自定义网站 {source_name} 的网址不能包含密钥参数")
                if source_role == "official":
                    try:
                        trusted_configured_official_host(url.strip())
                    except (UrlPolicyViolation, ValueError) as exc:
                        raise ValueError(
                            f"自定义网站 {source_name} 不能标记为官方来源：{exc}"
                        ) from exc
                start_origins.add(
                    (parsed_url.scheme.lower(), parsed_url.hostname.lower(), start_port)
                )
            auth = source.get("auth", {"mode": "none"})
            if not isinstance(auth, dict):
                raise ValueError(f"自定义网站 {source_name} 的 auth 必须是 JSON 对象")
            auth_mode = auth.get("mode", "none")
            if source.get("adapter") == "okcis" and (
                start_urls != ["https://www.okcis.cn/search/"] or auth_mode != "browser"
                or source_role != "commercial_lead"
            ):
                raise ValueError("导航网需使用高级搜索入口、浏览器登录和商业线索来源性质")
            if auth_mode not in {"none", "basic", "form", "browser"}:
                raise ValueError(f"自定义网站 {source_name} 的登录模式无效")
            raw_profile_id = auth.get("profile_id")
            profile_id = ""
            if raw_profile_id is not None:
                if not isinstance(raw_profile_id, str) or not PROFILE_ID_RE.fullmatch(
                    raw_profile_id.strip()
                ):
                    raise ValueError(
                        f"自定义网站 {source_name} 的浏览器登录配置标识无效"
                    )
                profile_id = raw_profile_id.strip()
            if auth_mode == "browser":
                effective_profile_id = profile_id or source_id
                if effective_profile_id in browser_profile_ids:
                    raise ValueError("浏览器登录配置标识不能重复")
                browser_profile_ids.add(effective_profile_id)
            if auth_mode != "none":
                if len(start_origins) != 1:
                    raise ValueError(
                        f"需要登录的自定义网站 {source_name} 只能配置一个协议、主机和端口"
                    )
                if next(iter(start_origins))[0] != "https":
                    raise ValueError(
                        f"需要登录的自定义网站 {source_name} 必须使用 HTTPS"
                    )
            login_url = str(auth.get("login_url", "")).strip()
            if auth_mode == "form" or (auth_mode == "browser" and login_url):
                if not login_url.startswith(("https://", "http://")):
                    raise ValueError(f"自定义网站 {source_name} 必须填写登录地址")
                parsed_login = urlsplit(login_url)
                try:
                    login_port = parsed_login.port or (
                        443 if parsed_login.scheme.lower() == "https" else 80
                    )
                except ValueError as exc:
                    raise ValueError(f"自定义网站 {source_name} 的登录地址端口无效") from exc
                if (
                    not parsed_login.hostname
                    or parsed_login.username
                    or parsed_login.password
                    or (
                        parsed_login.scheme.lower(),
                        parsed_login.hostname.lower(),
                        login_port,
                    ) not in start_origins
                ):
                    raise ValueError(
                        f"自定义网站 {source_name} 的登录地址必须与采集网址使用同一来源（协议、主机和端口）"
                    )
                if _has_secret_query(login_url):
                    raise ValueError(f"自定义网站 {source_name} 的登录地址不能包含密钥参数")
            check_url = str(auth.get("check_url", "")).strip()
            if check_url:
                if auth_mode != "browser":
                    raise ValueError(
                        f"自定义网站 {source_name} 只有浏览器登录模式可以配置检查地址"
                    )
                if not check_url.startswith(("https://", "http://")):
                    raise ValueError(f"自定义网站 {source_name} 的登录检查地址无效")
                parsed_check = urlsplit(check_url)
                try:
                    check_port = parsed_check.port or (
                        443 if parsed_check.scheme.lower() == "https" else 80
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"自定义网站 {source_name} 的登录检查地址端口无效"
                    ) from exc
                if (
                    not parsed_check.hostname
                    or parsed_check.username
                    or parsed_check.password
                    or (
                        parsed_check.scheme.lower(),
                        parsed_check.hostname.lower(),
                        check_port,
                    ) not in start_origins
                ):
                    raise ValueError(
                        f"自定义网站 {source_name} 的登录检查地址必须与采集网址使用同一来源（协议、主机和端口）"
                    )
                if _has_secret_query(check_url):
                    raise ValueError(
                        f"自定义网站 {source_name} 的登录检查地址不能包含密钥参数"
                    )
            if auth_mode == "form":
                for key in ("username_field", "password_field"):
                    field_name = str(auth.get(key, "")).strip()
                    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", field_name):
                        raise ValueError(f"自定义网站 {source_name} 的 {key} 无效")
                extra_fields = auth.get("extra_fields", {})
                if not isinstance(extra_fields, dict) or not all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in extra_fields.items()
                ):
                    raise ValueError(f"自定义网站 {source_name} 的 extra_fields 必须是字符串对象")
    if require_ready and enabled_count == 0:
        raise ValueError("至少启用一个数据源")

    http = data.get("http", {})
    if not isinstance(http, dict):
        raise ValueError("http 必须是 JSON 对象")
    for key in ("allow_private_hosts", "allow_benchmark_proxy_hosts"):
        if key in http and not isinstance(http[key], bool):
            raise ValueError(f"http.{key} 必须是布尔值")
    if "user_agent" in http and not isinstance(http["user_agent"], str):
        raise ValueError("http.user_agent 必须是字符串")
    for key, minimum, maximum in (
        ("timeout_seconds", 1, 600),
        ("delay_seconds", 0, 3600),
        ("max_response_mb", 1, 1024),
        ("max_download_mb", 1, 10240),
        ("download_timeout_seconds", 1, 86400),
    ):
        if key in http:
            _number(http[key], f"http.{key}", minimum=minimum, maximum=maximum)
    if "max_retries" in http:
        _integer(http["max_retries"], "http.max_retries", minimum=0, maximum=20)
    ai = data.get("ai", {})
    if not isinstance(ai, dict):
        raise ValueError("ai 必须是 JSON 对象")
    enabled = ai.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("ai.enabled 必须是布尔值")
    thresholds: dict[str, float] = {}
    for key in ("auto_accept_threshold", "second_review_threshold"):
        if key in ai:
            thresholds[key] = _number(ai[key], f"ai.{key}", minimum=0, maximum=1)
    accept = thresholds.get("auto_accept_threshold", 0.85)
    review = thresholds.get("second_review_threshold", 0.60)
    if review > accept:
        raise ValueError("ai.second_review_threshold 不能高于 ai.auto_accept_threshold")
    if "reclassify" in ai and not isinstance(ai["reclassify"], bool):
        raise ValueError("ai.reclassify 必须是布尔值")
    if "timeout_seconds" in ai:
        _number(ai["timeout_seconds"], "ai.timeout_seconds", minimum=1, maximum=600)
    if "provider" in ai and not isinstance(ai["provider"], str):
        raise ValueError("ai.provider 必须是字符串")
    protocol = ai.get("protocol", "openai_compatible")
    if protocol not in {"openai_compatible", "anthropic", "gemini"}:
        raise ValueError("ai.protocol 必须是 openai_compatible、anthropic 或 gemini")
    endpoint = str(ai.get("endpoint", "")).strip()
    model = str(ai.get("model", "")).strip()
    key_env = str(ai.get("api_key_env", "")).strip()
    if key_env and (
        not ENV_NAME_RE.fullmatch(key_env) or key_env.upper() in RESERVED_ENV_NAMES
    ):
        raise ValueError("ai.api_key_env 必须是专用的环境变量名，例如 TENDER_AI_API_KEY")
    if endpoint.startswith(("https://", "http://")):
        parsed_endpoint = urlsplit(endpoint)
        try:
            parsed_endpoint.port
        except ValueError as exc:
            raise ValueError("ai.endpoint 的端口无效") from exc
        if parsed_endpoint.username or parsed_endpoint.password:
            raise ValueError("ai.endpoint 不能包含账号密码")
        if _has_secret_query(endpoint):
            raise ValueError("ai.endpoint 不能在 URL 中包含 API Key 或 Token")
    if enabled and require_ready:
        if not endpoint.startswith(("https://", "http://")) or "your-model-provider" in endpoint:
            raise ValueError("启用 AI 后必须填写有效的 ai.endpoint")
        parsed_endpoint = urlsplit(endpoint)
        if not parsed_endpoint.hostname or parsed_endpoint.username or parsed_endpoint.password:
            raise ValueError("ai.endpoint 不能包含账号密码，且必须包含有效主机名")
        if parsed_endpoint.scheme.lower() != "https":
            local_endpoint = parsed_endpoint.hostname.lower() in {
                "127.0.0.1", "localhost", "::1",
            }
            if not (local_endpoint and http.get("allow_private_hosts", False)):
                raise ValueError(
                    "ai.endpoint 必须使用 HTTPS；仅显式允许私网时可使用本机 HTTP 模型"
                )
        if not model or "replace-with" in model:
            raise ValueError("启用 AI 后必须填写 ai.model")
        if not ENV_NAME_RE.fullmatch(key_env) or key_env.upper() in RESERVED_ENV_NAMES:
            raise ValueError("ai.api_key_env 必须是专用的环境变量名，例如 TENDER_AI_API_KEY")

    recall = data.get("recall", {})
    if not isinstance(recall, dict):
        raise ValueError("recall 必须是 JSON 对象")
    if recall.get("mode", "p0_complete") not in {"fast", "p0_complete", "complete"}:
        raise ValueError("recall.mode 必须是 fast、p0_complete 或 complete")

    provenance = data.get("provenance", {})
    if not isinstance(provenance, dict):
        raise ValueError("provenance 必须是 JSON 对象")
    if "max_gate_file_mb" in provenance:
        _number(
            provenance["max_gate_file_mb"],
            "provenance.max_gate_file_mb",
            minimum=1,
            maximum=2048,
        )
    if "allow_registered_http_fallback" in provenance:
        fallback = provenance["allow_registered_http_fallback"]
        if not isinstance(fallback, bool):
            raise ValueError("provenance.allow_registered_http_fallback 必须是布尔值")
        if fallback:
            raise ValueError(
                "正式原件只允许 HTTPS，不能启用 registered HTTP fallback"
            )

    delivery = data.get("delivery", {})
    if not isinstance(delivery, dict):
        raise ValueError("delivery 必须是 JSON 对象")
    if delivery.get("mode", "on_demand") not in {"on_demand", "automatic"}:
        raise ValueError("delivery.mode 必须是 on_demand 或 automatic")
    for key in (
        "include_notice_html_when_no_attachment",
        "include_official_notice_with_attachments",
        "copy_uncertain_to_review",
    ):
        if key in delivery and not isinstance(delivery[key], bool):
            raise ValueError(f"delivery.{key} 必须是布尔值")


@dataclass(frozen=True, slots=True)
class AppConfig:
    path: Path
    data: dict[str, Any]

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.data["start_date"])

    @property
    def end_date(self) -> date:
        return date.fromisoformat(self.data["end_date"])

    @property
    def output_dir(self) -> Path:
        value = Path(self.data.get("output_dir", "output"))
        return value if value.is_absolute() else (self.path.parent / value).resolve()

    @property
    def database_path(self) -> Path:
        value = Path(self.data.get("database", "output/state.sqlite3"))
        return value if value.is_absolute() else (self.path.parent / value).resolve()


def effective_user_agent(http_config: dict[str, Any]) -> str:
    """Return an internal request identifier without requiring personal details."""
    configured = str(http_config.get("user_agent", "")).strip()
    if not configured or "replace-with" in configured.lower():
        return DEFAULT_USER_AGENT
    return configured


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    validate_config_data(data)
    return AppConfig(config_path, data)
