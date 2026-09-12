from __future__ import annotations

import html
import json
import re
from dataclasses import replace
from datetime import date, timedelta
from typing import Iterator
from urllib.parse import urlencode, urlsplit, urlunsplit

from ..http_client import FetchError
from ..models import AttachmentRef, Coverage, Notice, RawDocument
from ..utils import stable_id
from .base import SourceAdapter


CHANNELS = (
    ("政府采购", "采购公告"),
    ("政府采购", "单一来源论证公示"),
    ("政府采购", "中标公告"),
    ("政府采购", "变更公告"),
    ("政府采购", "终止（废标）公告"),
    ("政府采购", "合同公示"),
    ("工程建设", "招标文件预公示"),
    ("工程建设", "招标计划"),
    ("工程建设", "招标公告"),
    ("工程建设", "变更公告"),
    ("工程建设", "中标候选人公示"),
    ("工程建设", "中标结果公告"),
    ("工程建设", "合同公示"),
)


# WAS 的历史数据仍会返回 HTTP 地址，但这些页面在吉林省政府官网已支持
# HTTPS。只升级明确列出的官方主机；不能用 ``endswith("jl.gov.cn")``，
# 否则容易把拼接或仿冒主机误认为官方网站。
_JILIN_HTTPS_HOSTS = {
    "jl.gov.cn": "jl.gov.cn",
    "www.jl.gov.cn": "www.jl.gov.cn",
    # WAS 旧数据中的测试域名沿用官网路径，保持原有的迁移行为。
    "ceshi5.jl.gov.cn": "www.jl.gov.cn",
}


def _upgrade_jilin_official_https(value: str) -> str:
    """Upgrade legacy HTTP URLs only for exact, registered Jilin hosts."""

    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
    except ValueError:
        return value
    target_host = _JILIN_HTTPS_HOSTS.get(host)
    if (
        parsed.scheme.lower() != "http"
        or target_host is None
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80}
    ):
        return value
    return urlunsplit((
        "https",
        target_host,
        parsed.path,
        parsed.query,
        parsed.fragment,
    ))


def _plain(value: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(value or "")).strip()


def _parse_date(value: str) -> date | None:
    match = re.search(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})", value or "")
    if not match:
        return None
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


class JilinGGZYSource(SourceAdapter):
    source_type = "jilin_ggzy"
    display_name = "吉林省公共资源交易公共服务平台"
    endpoint = "https://was.jl.gov.cn/was5/web/search"

    def prepare(self) -> None:
        # WAS 会校验公开浏览会话。这里只访问公开栏目建立 Cookie，不登录、不解验证码。
        self.client.request("https://www.jl.gov.cn/ggzy/zfcg/cggg/")

    def discover_artifacts(
        self, notice: Notice, detail: RawDocument
    ) -> list[AttachmentRef]:
        artifacts = super().discover_artifacts(notice, detail)
        return [
            replace(
                artifact,
                url=_upgrade_jilin_official_https(artifact.url),
                referer=(
                    _upgrade_jilin_official_https(artifact.referer)
                    if artifact.referer
                    else None
                ),
            )
            for artifact in artifacts
        ]

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        max_pages = int(self.config.get("max_pages_per_channel", 300))
        page_size = min(100, max(1, int(self.config.get("page_size", 100))))
        seen: set[str] = set()
        earliest = ""
        latest = ""
        all_reached_start = True
        all_reached_end = True
        configured_channels = self.config.get("channels")
        channels = CHANNELS
        if isinstance(configured_channels, list):
            parsed_channels = tuple(
                (str(item.get("trade_type", "")), str(item.get("info_type", "")))
                for item in configured_channels
                if isinstance(item, dict) and item.get("trade_type") and item.get("info_type")
            )
            if parsed_channels:
                channels = parsed_channels
        empty_channels: list[str] = []
        invalid_date_records = 0
        if self.config.get("_query_all_channels"):
            channels = (("", ""),)
        for trade_type, info_type in channels:
            channel_reached_start = False
            channel_reached_end = False
            channel_exhausted = False
            channel_total: int | None = None
            for page in range(1, max_pages + 1):
                expression = (
                    "modal<>3 and gtitle<>'' and gtitle<>'null' "
                    f"and tType='{trade_type}' and iType='{info_type}'"
                )
                if self.config.get("_query_all_channels"):
                    terms = [term for term in self.config["_query_terms"] if term]
                    if any(not re.fullmatch(r"[\w\s./()+\-]{1,100}", term) for term in terms):
                        raise ValueError("查询关键词格式无效")
                    expression = "modal<>3 and gtitle<>'' and gtitle<>'null'"
                    expression += " and (tType='政府采购' or tType='工程建设')"
                    if terms:
                        expression += " and (" + " or ".join(f"gtitle='{term}'" for term in terms) + ")"
                    expression += (
                        f" and timestamp >'{start - timedelta(days=1)} 23:59:59'"
                        f" and timestamp <'{end + timedelta(days=1)} 00:00:00'"
                    )
                query = urlencode({
                    "channelid": "237687",
                    "page": str(page),
                    "prepage": str(page_size),
                    "searchword": expression,
                    "callback": "result",
                })
                result = self.client.request(
                    f"{self.endpoint}?{query}",
                    headers={"Referer": "https://www.jl.gov.cn/ggzy/"},
                )
                coverage.pages += 1
                text = result.body.decode("utf-8", errors="replace").strip()
                match = re.match(r"^[\w$]+\((.*)\)\s*;?$", text, flags=re.S)
                if match:
                    text = match.group(1)
                payload = json.loads(text)
                if (
                    not isinstance(payload, dict)
                    or "datas" not in payload
                    or "recordnum" not in payload
                    or not isinstance(payload.get("datas"), list)
                ):
                    raise FetchError(
                        f"吉林 WAS 响应结构变化：{trade_type}/{info_type} page={page}"
                    )
                total_raw = payload.get("recordnum")
                try:
                    total = int(total_raw)
                except (TypeError, ValueError) as exc:
                    raise FetchError("吉林 WAS recordnum 不是有效整数") from exc
                if channel_total is None:
                    channel_total = total
                records = payload.get("datas") or []
                if total == 0 and records:
                    raise FetchError("吉林 WAS recordnum=0 但 datas 非空")
                if not records:
                    if total > 0:
                        raise FetchError(
                            f"吉林 WAS recordnum={total} 但当前页 datas 为空"
                        )
                    channel_exhausted = True
                    channel_reached_start = True
                    channel_reached_end = True
                    break
                reached_start = False
                for record in records:
                    published = str(record.get("timestamp") or record.get("pubdate") or "")
                    published_date = _parse_date(published)
                    if published_date is None:
                        invalid_date_records += 1
                        continue
                    if published_date and published_date <= end:
                        channel_reached_end = True
                    if published_date and published_date < start:
                        reached_start = True
                        continue
                    if published_date and published_date > end:
                        continue
                    raw_url = str(record.get("docpuburl") or record.get("url") or "")
                    url = _upgrade_jilin_official_https(raw_url)
                    if not url or url in seen:
                        continue
                    seen.add(url)
                    iso_date = published_date.isoformat() if published_date else published[:10]
                    earliest = min(filter(None, (earliest, iso_date)), default=iso_date)
                    latest = max(latest, iso_date)
                    title = _plain(str(record.get("title") or record.get("gtitle") or ""))
                    external_id = stable_id(url)
                    area_raw = str(record.get("area") or "").strip()
                    region = area_raw if any(
                        name in area_raw
                        for name in (
                            "吉林省", "长春", "吉林市", "四平", "辽源", "通化",
                            "白山", "松原", "白城", "延边", "长白山", "梅河口",
                        )
                    ) else "吉林省"
                    coverage.notices += 1
                    yield Notice(
                        source=self.name,
                        authority_rank=self.authority_rank,
                        external_id=external_id,
                        title=title,
                        published_at=iso_date,
                        url=url,
                        region=region,
                        notice_type=f"{trade_type or record.get('tType', '')}/{info_type or record.get('iType', '')}",
                        buyer=str(record.get("purchaser") or ""),
                        metadata={
                            "trade_type": trade_type or record.get("tType", ""),
                            "info_type": info_type or record.get("iType", ""),
                            "area_raw": area_raw,
                        },
                    )
                if reached_start:
                    channel_reached_start = True
                if total is not None and page * page_size >= total:
                    channel_exhausted = True
                    channel_reached_start = True
                    channel_reached_end = True
                if reached_start or channel_exhausted:
                    break
            if not channel_exhausted and not channel_reached_start:
                coverage.truncated = True
            all_reached_start = all_reached_start and channel_reached_start
            all_reached_end = all_reached_end and channel_reached_end
            if channel_total == 0:
                empty_channels.append(f"{trade_type}/{info_type}")
        coverage.first_date = earliest
        coverage.last_date = latest
        coverage.reached_start = all_reached_start
        coverage.reached_end = all_reached_end
        if coverage.truncated:
            coverage.message = "至少一个公告频道达到分页上限，未证明已覆盖到目标开始日期"
        if empty_channels:
            warning = "频道全历史返回0条，请核对官网栏目值：" + "、".join(empty_channels)
            coverage.message = "；".join(filter(None, (coverage.message, warning)))
            if not self.config.get("allow_empty_channels", False):
                coverage.status = "partial"
        if invalid_date_records:
            warning = f"有 {invalid_date_records} 条记录缺少可解析发布时间，已跳过"
            coverage.message = "；".join(filter(None, (coverage.message, warning)))
            coverage.status = "partial"
