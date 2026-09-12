from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Iterator
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

from ..classify import BROAD_TERMS, STRONG_TERMS
from ..htmlparse import decode_html
from ..http_client import FetchError
from ..models import Coverage, Notice
from ..utils import stable_id
from .base import SourceAdapter


_BASE_KEYWORDS = (
    "网络安全",
    "信息安全",
    "数据安全",
    "等级保护",
    "等保",
    "密码",
    "安全运维",
    "安全服务",
    "安全设备",
    "防火墙",
    "漏洞",
    "渗透测试",
    "态势感知",
    "终端安全",
)
DEFAULT_KEYWORDS = tuple(dict.fromkeys((*_BASE_KEYWORDS, *STRONG_TERMS, *BROAD_TERMS)))


@dataclass(slots=True)
class _SearchRecord:
    url: str
    title: str
    text: str


class _SearchResultParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.records: list[_SearchRecord] = []
        self._in_li = 0
        self._url = ""
        self._title_parts: list[str] = []
        self._text_parts: list[str] = []
        self._in_target_link = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "li":
            self._in_li += 1
            if self._in_li == 1:
                self._url = ""
                self._title_parts = []
                self._text_parts = []
        if self._in_li and tag == "a" and not self._url:
            href = (dict(attrs).get("href") or "").strip()
            if "/cggg/" in href:
                self._url = href
                self._in_target_link = True

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag == "a":
            self._in_target_link = False
        if tag == "li" and self._in_li:
            self._in_li -= 1
            if self._in_li == 0 and self._url:
                title = re.sub(r"\s+", " ", " ".join(self._title_parts)).strip()
                text = re.sub(r"\s+", " ", " ".join(self._text_parts)).strip()
                self.records.append(_SearchRecord(self._url, title, text))

    def handle_data(self, data: str) -> None:
        if not self._in_li:
            return
        value = re.sub(r"\s+", " ", data).strip()
        if not value:
            return
        self._text_parts.append(value)
        if self._in_target_link:
            self._title_parts.append(value)


def _parse_records(data: bytes, content_type: str, base_url: str) -> list[_SearchRecord]:
    parser = _SearchResultParser()
    parser.feed(decode_html(data, content_type))
    for record in parser.records:
        record.url = urljoin(base_url, html.unescape(record.url))
        parsed = urlsplit(record.url)
        if (parsed.scheme == "http" and parsed.hostname in {"www.ccgp.gov.cn", "ccgp.gov.cn"}
                and parsed.username is None and parsed.password is None and parsed.port in (None, 80)):
            record.url = urlunsplit(("https", parsed.hostname, parsed.path, parsed.query, parsed.fragment))
    return parser.records


def _record_date(text: str) -> date | None:
    # 摘要正文可能先出现投标截止日；发布时间位于结果元数据尾部并紧邻“| 采购人”。
    matches = list(re.finditer(
        r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})"
        r"(?:日)?(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?\s*(?=[|｜]\s*采购人)",
        text,
    ))
    if not matches:
        return None
    match = matches[-1]
    return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))


def _buyer(text: str) -> str:
    match = re.search(
        r"采购人\s*[：:]\s*(.+?)(?=\s*[|｜]\s*|\s+代理机构\s*[：:]|$)",
        text,
    )
    return match.group(1).strip() if match else ""


def _pagination(document: str) -> tuple[int | None, int | None]:
    pages_match = re.search(
        r"Pager\s*\(\s*\{.*?\bsize\s*:\s*['\"]?(\d+)",
        document,
        flags=re.I | re.S,
    )
    total_match = re.search(
        r"共找到\s*<span[^>]*>\s*(\d+)\s*</span>\s*条内容",
        document,
        flags=re.I | re.S,
    )
    pages = int(pages_match.group(1)) if pages_match else None
    total = int(total_match.group(1)) if total_match else None
    return pages, total


class CCGPSearchSource(SourceAdapter):
    source_type = "ccgp_search"
    display_name = "中国政府采购网检索（吉林）"
    endpoint = "https://search.ccgp.gov.cn/bxsearch"

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        from ..geography import source_region
        selected_region = source_region(self.config)
        keywords = [str(value).strip() for value in self.config.get(
            "_query_terms", self.config.get("keywords", DEFAULT_KEYWORDS))]
        keywords = keywords if "_query_terms" in self.config else [value for value in keywords if value]
        if not keywords:
            coverage.status = "failed"
            coverage.message = "ccgp_search 至少需要一个检索关键词"
            return
        window_days = max(1, int(self.config.get("window_days", 31)))
        max_pages = max(1, int(self.config.get("max_pages_per_query", 100)))
        seen: set[str] = set()
        earliest = ""
        latest = ""
        all_queries_complete = True

        cursor = start
        while cursor <= end:
            window_end = min(end, cursor + timedelta(days=window_days - 1))
            for keyword in keywords:
                query_complete = False
                for page in range(1, max_pages + 1):
                    params = {
                        "searchtype": "1",
                        "page_index": str(page),
                        "bidSort": "0",
                        "buyerName": "",
                        "projectId": "",
                        "pinMu": "0",
                        "bidType": "0",
                        "dbselect": "bidx",
                        "kw": keyword,
                        "start_time": cursor.strftime("%Y:%m:%d"),
                        "end_time": window_end.strftime("%Y:%m:%d"),
                        "timeType": "6",
                        "pppStatus": "0",
                        "agentName": "",
                    }
                    if selected_region["province"]:
                        params.update(displayZone=selected_region["province"],
                                      zoneId=selected_region["province_code"][:2])
                    url = f"{self.endpoint}?{urlencode(params)}"
                    result = self.client.request(
                        url,
                        headers={"Referer": "https://search.ccgp.gov.cn/bxsearch"},
                    )
                    coverage.pages += 1
                    decoded_page = decode_html(
                        result.body, result.headers.get("content-type", "")
                    )
                    valid_result_page = any(marker in decoded_page for marker in (
                        "vT-srch-result-list-bid",
                        "没有搜索到",
                        "暂无相关",
                        "没有检索到",
                    ))
                    if not valid_result_page:
                        raise FetchError(
                            "中国政府采购网检索页结构变化或返回了非结果页面"
                        )
                    total_pages, total_records = _pagination(decoded_page)
                    if total_records is None:
                        raise FetchError("中国政府采购网检索页缺少总记录数")
                    if total_records > 0 and total_pages is None:
                        raise FetchError("中国政府采购网检索页缺少总页数/总记录数")
                    records = _parse_records(
                        result.body,
                        result.headers.get("content-type", ""),
                        result.url,
                    )
                    if total_records == 0:
                        if records:
                            raise FetchError("中国政府采购网总记录数为0但列表非空")
                        query_complete = True
                        break
                    if total_pages < 1 or page > total_pages:
                        raise FetchError("中国政府采购网分页元数据不一致")
                    if not records:
                        raise FetchError("中国政府采购网非空分页未解析出公告记录")
                    for record in records:
                        published = _record_date(record.text)
                        # 官方搜索请求已按日期窗口过滤；摘要中的其他业务日期不能用于二次排除。
                        if record.url in seen:
                            continue
                        seen.add(record.url)
                        published_text = published.isoformat() if published else ""
                        if published_text:
                            earliest = min(
                                filter(None, (earliest, published_text)),
                                default=published_text,
                            )
                            latest = max(latest, published_text)
                        match = re.search(r"t\d+_(\d+)\.htm", record.url)
                        external_id = match.group(1) if match else stable_id(record.url)
                        coverage.notices += 1
                        yield Notice(
                            source=self.name,
                            authority_rank=self.authority_rank,
                            external_id=external_id,
                            title=record.title,
                            published_at=published_text,
                            url=record.url,
                            region=selected_region["province"],
                            notice_type="政府采购/检索结果",
                            buyer=_buyer(record.text),
                            metadata={
                                "query_keyword": keyword,
                                "window_start": cursor.isoformat(),
                                "window_end": window_end.isoformat(),
                            },
                        )
                    if page >= total_pages:
                        query_complete = True
                        break
                if not query_complete:
                    all_queries_complete = False
            cursor = window_end + timedelta(days=1)

        coverage.first_date = earliest
        coverage.last_date = latest
        coverage.truncated = not all_queries_complete
        coverage.reached_start = all_queries_complete
        coverage.reached_end = all_queries_complete
        if coverage.truncated:
            coverage.message = "至少一个日期窗口/关键词达到分页上限，检索结果被截断"
