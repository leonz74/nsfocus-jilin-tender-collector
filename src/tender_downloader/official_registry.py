from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit, urlunsplit


class SourceRole(str, Enum):
    """A source's provenance role, not merely its perceived authority."""

    OFFICIAL = "official"
    COMMERCIAL_LEAD = "commercial_lead"
    UNKNOWN = "unknown"


class UrlPolicyViolation(ValueError):
    """Raised when a URL is structurally unsafe or violates transport policy."""


@dataclass(frozen=True, slots=True)
class SourceRegistration:
    key: str
    display_name: str
    role: SourceRole
    domains: tuple[str, ...]
    https_only: bool = True
    exact_hosts_only: bool = False
    official_notice_eligible: bool = True
    attachment_parent_keys: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.key.strip():
            raise ValueError("来源注册项 key 不能为空")
        if not self.domains:
            raise ValueError("来源注册项必须至少包含一个域名")
        if not isinstance(self.exact_hosts_only, bool):
            raise ValueError("exact_hosts_only 必须是布尔值")
        if not isinstance(self.official_notice_eligible, bool):
            raise ValueError("official_notice_eligible 必须是布尔值")
        if self.attachment_parent_keys and self.role is not SourceRole.OFFICIAL:
            raise ValueError("只有官方来源可以声明附件上级来源")
        normalized_parent_keys: list[str] = []
        for parent_key in self.attachment_parent_keys:
            value = str(parent_key).strip()
            if not value:
                raise ValueError("附件上级来源 key 不能为空")
            if value in normalized_parent_keys:
                raise ValueError(f"附件上级来源 key 重复: {parent_key}")
            normalized_parent_keys.append(value)
        object.__setattr__(
            self, "attachment_parent_keys", tuple(normalized_parent_keys)
        )
        normalized: list[str] = []
        for domain in self.domains:
            value = _normalize_registry_domain(domain)
            if value in normalized:
                raise ValueError(f"来源注册项包含重复域名: {domain}")
            normalized.append(value)
        object.__setattr__(self, "domains", tuple(normalized))


def _normalize_registry_domain(domain: str) -> str:
    value = str(domain).strip().lower()
    if not value or value.startswith(".") or value.endswith("."):
        raise ValueError(f"注册表域名无效: {domain}")
    if "/" in value or "\\" in value or ":" in value or "@" in value:
        raise ValueError(f"注册表域名无效: {domain}")
    try:
        ascii_value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError(f"注册表域名无效: {domain}") from exc
    labels = ascii_value.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels
    ):
        raise ValueError(f"注册表域名无效: {domain}")
    return ascii_value


def normalize_hostname(hostname: str) -> str:
    """Normalize a parsed hostname while rejecting ambiguous spellings."""

    value = (hostname or "").strip().lower()
    if not value or value.endswith(".") or "\\" in value:
        raise UrlPolicyViolation("URL 主机名无效")
    try:
        value = value.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise UrlPolicyViolation("URL 主机名无效") from exc
    # Registry entries intentionally contain DNS names, never raw IP literals.
    labels = value.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not all(ch.isalnum() or ch == "-" for ch in label)
        for label in labels
    ):
        raise UrlPolicyViolation("URL 主机名无效")
    return value


def host_matches_domain(hostname: str, registered_domain: str) -> bool:
    """Return true only for an exact host or a real DNS subdomain.

    Delimiting with a literal dot is important: ``ccgp.gov.cn.evil.test`` and
    ``fakeccgp.gov.cn`` must not match ``ccgp.gov.cn``.
    """

    try:
        host = normalize_hostname(hostname)
        domain = _normalize_registry_domain(registered_domain)
    except (UrlPolicyViolation, ValueError):
        return False
    return host == domain or host.endswith(f".{domain}")


class SourceRegistry:
    def __init__(self, registrations: tuple[SourceRegistration, ...]) -> None:
        self._registrations = tuple(registrations)
        keys: set[str] = set()
        claimed_domains: dict[str, str] = {}
        for registration in self._registrations:
            if registration.key in keys:
                raise ValueError(f"来源注册表 key 重复: {registration.key}")
            keys.add(registration.key)
            for domain in registration.domains:
                prior = claimed_domains.get(domain)
                if prior is not None:
                    raise ValueError(
                        f"来源注册表域名 {domain} 同时属于 {prior} 和 {registration.key}"
                    )
                claimed_domains[domain] = registration.key
        for registration in self._registrations:
            missing = set(registration.attachment_parent_keys).difference(keys)
            if missing:
                raise ValueError(
                    f"来源注册项 {registration.key} 引用了不存在的附件上级来源: "
                    + ",".join(sorted(missing))
                )

    @property
    def registrations(self) -> tuple[SourceRegistration, ...]:
        return self._registrations

    def registration_for_host(self, hostname: str) -> SourceRegistration | None:
        try:
            host = normalize_hostname(hostname)
        except UrlPolicyViolation:
            return None
        matches: list[tuple[int, SourceRegistration]] = []
        for registration in self._registrations:
            for domain in registration.domains:
                if host == domain or (
                    not registration.exact_hosts_only
                    and host.endswith(f".{domain}")
                ):
                    matches.append((len(domain), registration))
        if not matches:
            return None
        # The longest registered suffix is the most specific registration.
        return max(matches, key=lambda item: item[0])[1]

    def registration_for_url(
        self,
        url: str,
        *,
        enforce_transport: bool = True,
    ) -> SourceRegistration | None:
        parsed = _split_web_url(url)
        registration = self.registration_for_host(parsed.hostname or "")
        if registration is None:
            return None
        if enforce_transport and registration.https_only and parsed.scheme.lower() != "https":
            raise UrlPolicyViolation(
                f"来源 {registration.display_name} 仅允许 HTTPS URL"
            )
        return registration

    def role_for_url(
        self,
        url: str,
        *,
        enforce_transport: bool = False,
    ) -> SourceRole:
        registration = self.registration_for_url(
            url,
            enforce_transport=enforce_transport,
        )
        return registration.role if registration is not None else SourceRole.UNKNOWN

    def official_notice_registration(
        self,
        url: str,
        *,
        enforce_transport: bool = True,
    ) -> SourceRegistration | None:
        """Return an official registration that may represent a notice page.

        Some exact official infrastructure hosts are trusted only for files
        linked by a verified notice.  Registering those hosts must not make an
        arbitrary object on the host usable as a standalone official notice.
        """

        registration = self.registration_for_url(
            url, enforce_transport=enforce_transport
        )
        if (
            registration is None
            or registration.role is not SourceRole.OFFICIAL
            or not registration.official_notice_eligible
        ):
            return None
        return registration

    def official_attachment_registration(
        self,
        attachment_url: str,
        official_notice_url: str,
    ) -> SourceRegistration | None:
        """Validate an HTTPS attachment in the context of its official notice.

        Ordinary registered official hosts remain usable as before.  A
        context-restricted file host is usable only when the already-verified
        parent notice belongs to one of its explicitly named source families.
        """

        try:
            attachment = self.registration_for_url(
                attachment_url, enforce_transport=True
            )
            parent = self.official_notice_registration(
                official_notice_url, enforce_transport=True
            )
        except UrlPolicyViolation:
            return None
        if (
            attachment is None
            or attachment.role is not SourceRole.OFFICIAL
            or parent is None
        ):
            return None
        if (
            attachment.attachment_parent_keys
            and parent.key not in attachment.attachment_parent_keys
        ):
            return None
        return attachment


def _split_web_url(url: str):
    if not isinstance(url, str) or not url:
        raise UrlPolicyViolation("URL 不能为空")
    if url != url.strip() or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in url):
        raise UrlPolicyViolation("URL 含空白或控制字符")
    if "\\" in url:
        raise UrlPolicyViolation("URL 含反斜杠，主机边界不明确")
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UrlPolicyViolation("URL 主机或端口无效") from exc
    if parsed.scheme.lower() not in {"http", "https"}:
        raise UrlPolicyViolation("URL 协议必须是 HTTP 或 HTTPS")
    if not parsed.netloc or not parsed.hostname:
        raise UrlPolicyViolation("URL 缺少主机名")
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        raise UrlPolicyViolation("URL 不允许包含用户信息")
    normalize_hostname(parsed.hostname)
    if port is not None and port not in {80, 443}:
        raise UrlPolicyViolation("URL 不允许使用非标准端口")
    if parsed.scheme.lower() == "https" and port == 80:
        raise UrlPolicyViolation("HTTPS URL 端口不一致")
    if parsed.scheme.lower() == "http" and port == 443:
        raise UrlPolicyViolation("HTTP URL 端口不一致")
    return parsed


def canonicalize_official_url(
    url: str,
    registry: SourceRegistry | None = None,
) -> tuple[str, SourceRegistration]:
    """Validate and canonicalize a registered official HTTPS URL.

    This is registry validation only.  Callers must still fetch the URL and
    validate the response before calling it an official original document.
    """

    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    parsed = _split_web_url(url)
    registration = active_registry.registration_for_host(parsed.hostname or "")
    if registration is None or registration.role is not SourceRole.OFFICIAL:
        raise UrlPolicyViolation("URL 不属于已注册官方来源")
    if parsed.scheme.lower() != "https" or registration.https_only and parsed.scheme != "https":
        raise UrlPolicyViolation("官方候选 URL 必须使用 HTTPS")
    host = normalize_hostname(parsed.hostname or "")
    port = parsed.port
    netloc = host if port in {None, 443} else f"{host}:{port}"
    path = parsed.path or "/"
    canonical = urlunsplit(("https", netloc, path, parsed.query, ""))
    return canonical, registration


DEFAULT_SOURCE_REGISTRY = SourceRegistry((
    SourceRegistration(
        key="central_procurement_attachment_cdn",
        display_name="中央采购电子卖场公告附件存储",
        role=SourceRole.OFFICIAL,
        domains=("zycgdzmc-pro.obs.cn-north1.ctyun.cn",),
        https_only=True,
        exact_hosts_only=True,
        official_notice_eligible=False,
        attachment_parent_keys=("ccgp",),
    ),
    SourceRegistration(
        key="ccgp",
        display_name="中国政府采购网",
        role=SourceRole.OFFICIAL,
        domains=("ccgp.gov.cn",),
    ),
    SourceRegistration(
        key="jilin_government",
        display_name="吉林省人民政府及公共资源平台",
        role=SourceRole.OFFICIAL,
        domains=("jl.gov.cn",),
    ),
    SourceRegistration(
        key="jilin_government_attachment_cdn",
        display_name="吉林省政府采购官方附件存储",
        role=SourceRole.OFFICIAL,
        domains=("zcy-gov-open-doc.oss-cn-north-2-gov-1.aliyuncs.com",),
        https_only=True,
        exact_hosts_only=True,
        official_notice_eligible=False,
        attachment_parent_keys=("jilin_government", "national_ggzy"),
    ),
    SourceRegistration(
        key="ccgp_jilin",
        display_name="吉林省政府采购网",
        role=SourceRole.OFFICIAL,
        domains=("ccgp-jilin.gov.cn",),
    ),
    SourceRegistration(
        key="pbc",
        display_name="中国人民银行",
        role=SourceRole.OFFICIAL,
        domains=("pbc.gov.cn",),
    ),
    SourceRegistration(
        key="national_ggzy",
        display_name="全国公共资源交易平台",
        role=SourceRole.OFFICIAL,
        domains=("ggzy.gov.cn",),
    ),
    SourceRegistration(
        key="okcis",
        display_name="招标采购导航网",
        role=SourceRole.COMMERCIAL_LEAD,
        domains=("okcis.cn",),
    ),
    SourceRegistration(
        key="qianlima",
        display_name="千里马招标网",
        role=SourceRole.COMMERCIAL_LEAD,
        domains=("qianlima.com",),
    ),
    SourceRegistration(
        key="bidcenter",
        display_name="采招网",
        role=SourceRole.COMMERCIAL_LEAD,
        domains=("bidcenter.com.cn",),
    ),
    SourceRegistration(
        key="chinabidding",
        display_name="中国国际招标网",
        role=SourceRole.COMMERCIAL_LEAD,
        domains=("chinabidding.com", "chinabidding.cn"),
    ),
    SourceRegistration(
        key="bidding",
        display_name="招标网",
        role=SourceRole.COMMERCIAL_LEAD,
        domains=("bidding.cn",),
    ),
))


_CONFIGURED_INSTITUTION_SUFFIXES = (".gov.cn", ".edu.cn")


def trusted_configured_official_host(
    url: str,
    registry: SourceRegistry | None = None,
) -> str:
    """Return the exact host that may be configured as an official source.

    Configuration is not itself a trust grant.  A host is accepted only when it
    is already a statically registered official source, or when the exact HTTPS
    host is beneath China's reserved government/education namespaces.  A
    statically registered commercial source is rejected before the suffix rule,
    so a user can never override its role in ``config.json``.
    """

    active_registry = registry or DEFAULT_SOURCE_REGISTRY
    parsed = _split_web_url(url)
    if parsed.scheme.lower() != "https" or parsed.port not in {None, 443}:
        raise UrlPolicyViolation("自定义官方来源必须使用标准端口 HTTPS")
    host = normalize_hostname(parsed.hostname or "")
    registration = active_registry.registration_for_host(host)
    if registration is not None:
        if registration.role is SourceRole.COMMERCIAL_LEAD:
            raise UrlPolicyViolation(
                f"{registration.display_name} 是商业线索站，不能由配置覆盖为官方来源"
            )
        if registration.role is SourceRole.OFFICIAL:
            if not registration.official_notice_eligible:
                raise UrlPolicyViolation(
                    f"{registration.display_name} 仅允许作为已验证官方公告的附件来源"
                )
            return host
        raise UrlPolicyViolation("该主机未被注册为官方来源")
    if not host.endswith(_CONFIGURED_INSTITUTION_SUFFIXES):
        raise UrlPolicyViolation(
            "未知主机不能直接标记为官方；仅允许内置官方域或 HTTPS .gov.cn/.edu.cn 机构主机"
        )
    return host


__all__ = [
    "DEFAULT_SOURCE_REGISTRY",
    "SourceRegistration",
    "SourceRegistry",
    "SourceRole",
    "UrlPolicyViolation",
    "canonicalize_official_url",
    "host_matches_domain",
    "normalize_hostname",
    "trusted_configured_official_host",
]
