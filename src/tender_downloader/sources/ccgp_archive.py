from __future__ import annotations

import html
import re
from datetime import date
from typing import Iterator
from urllib.parse import urljoin

from ..geography import source_region, province_alias
from ..htmlparse import decode_html
from ..http_client import FetchError, SourceBlocked
from ..models import Coverage, Notice
from ..utils import stable_id
from .base import SourceAdapter


ENTRY_RE = re.compile(
    r"<li[^>]*>\s*<a(?P<attrs>[^>]*)>(?P<link_text>(?:(?!</?a\b|</?li\b).)*?)</a>\s*"
    r"发布时间：\s*<em>(?P<date>[^<]+)</em>\s*"
    r"地域：\s*<em>(?P<region>[^<]+)</em>\s*"
    r"采购人：\s*<em>(?P<buyer>[^<]*)</em>",
    flags=re.S | re.I,
)

HREF_RE = re.compile(r"\bhref=[\"'](?P<value>[^\"']+)[\"']", flags=re.I)
TITLE_RE = re.compile(r"\btitle=[\"'](?P<value>[^\"']*)[\"']", flags=re.I)


class CCGPArchiveSource(SourceAdapter):
    source_type = "ccgp_archive"
    display_name = "中国政府采购网（吉林）"

    def _page_url(self, scope: str, category: str, page: int) -> str:
        name = "index.htm" if page == 0 else f"index_{page}.htm"
        return f"https://www.ccgp.gov.cn/cggg/{scope}/{category}/{name}"

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        selected_province = source_region(self.config)["province"]
        scopes = [str(value) for value in self.config.get("scopes", ["zygg", "dfgg"])]
        categories = [str(value) for value in self.config.get(
            "categories", ["gkzb", "zbgg", "cjgg", "gzgg", "fbgg"]
        )]
        max_pages = int(self.config.get("max_pages_per_category", 500))
        seen: set[str] = set()
        oldest_seen = ""
        newest_seen = ""
        errors: list[str] = []
        all_reached_start = True
        all_reached_end = True
        readable_channels = set()
        parse_errors = []
        for scope in scopes:
            for category in categories:
                category_reached_start = False
                category_reached_end = False
                category_exhausted = False
                for page in range(max_pages):
                    page_url = self._page_url(scope, category, page)
                    try:
                        result = self.client.request(page_url)
                    except SourceBlocked:
                        raise
                    except FetchError as exc:
                        if page > 0 and "HTTP 404" in str(exc):
                            # Each archive category retains only a couple dozen
                            # pages; a 404 while paging forward is the retention
                            # boundary, not an access failure.
                            errors.append(
                                f"{scope}/{category}: 归档仅保留最近约 {page} 页，更早内容需用检索来源")
                            coverage.truncated = True
                            break
                        errors.append(f"{scope}/{category}/{page}: {exc}")
                        break
                    coverage.pages += 1
                    content = decode_html(result.body, result.headers.get("content-type", ""))
                    entries = list(ENTRY_RE.finditer(content))
                    if not entries and 'c_list_bid' not in content:
                        # A throttled in-between page can look like structure
                        # loss; give it one delayed retry before declaring the
                        # archive unreadable.
                        sleeper = getattr(self.client, "sleeper", None)
                        if callable(sleeper):
                            sleeper(5)
                        try:
                            retry = self.client.request(page_url)
                        except SourceBlocked:
                            raise
                        except FetchError:
                            retry = None
                        if retry is not None:
                            content = decode_html(retry.body, retry.headers.get("content-type", ""))
                            entries = list(ENTRY_RE.finditer(content))
                    if not entries:
                        listing = re.search(r'<ul\b[^>]*class=["\'][^"\']*\bc_list_bid\b[^"\']*["\'][^>]*>(.*?)</ul>', content, re.S | re.I)
                        if not listing or re.search(r'<li\b', listing.group(1), re.I):
                            message = f"{scope}/{category}/{page}: 归档列表结构异常或访问受阻，不能判定为零条公告；其余栏目继续采集"
                            errors.append(message)
                            parse_errors.append(message)
                            coverage.truncated = True
                            break
                        readable_channels.add((scope, category))
                        category_exhausted = True
                        category_reached_start = True
                        category_reached_end = True
                        break
                    readable_channels.add((scope, category))
                    page_older_than_start = True
                    for match in entries:
                        href_match = HREF_RE.search(match.group("attrs"))
                        if not href_match:
                            continue
                        published_text = html.unescape(match.group("date")).strip()
                        try:
                            published = date.fromisoformat(published_text[:10])
                        except ValueError:
                            page_older_than_start = False
                            continue
                        if published <= end:
                            category_reached_end = True
                        if published < start:
                            category_reached_start = True
                        if published >= start:
                            page_older_than_start = False
                        if not (start <= published <= end):
                            continue
                        region = html.unescape(match.group("region")).strip()
                        if selected_province and province_alias(selected_province) not in region:
                            continue
                        url = urljoin(page_url, html.unescape(href_match.group("value")))
                        if url in seen:
                            continue
                        seen.add(url)
                        title_match = TITLE_RE.search(match.group("attrs"))
                        raw_title = title_match.group("value") if title_match else match.group("link_text")
                        title = re.sub(r"<[^>]+>", "", html.unescape(raw_title or "")).strip()
                        buyer = html.unescape(match.group("buyer") or "").strip()
                        terms = [str(t).casefold() for t in self.config.get("_query_terms", []) if t]
                        if terms and not any(t in (title + " " + buyer).casefold() for t in terms):
                            continue
                        external_match = re.search(r"t\d+_(\d+)\.htm", url)
                        external_id = external_match.group(1) if external_match else stable_id(url)
                        iso_date = published.isoformat()
                        oldest_seen = min(filter(None, (oldest_seen, iso_date)), default=iso_date)
                        newest_seen = max(newest_seen, iso_date)
                        coverage.notices += 1
                        yield Notice(
                            source=self.name,
                            authority_rank=self.authority_rank,
                            external_id=external_id,
                            title=title,
                            published_at=iso_date,
                            url=url,
                            region=region,
                            notice_type=f"{scope}/{category}",
                            buyer=buyer,
                            metadata={"scope": scope, "category": category},
                        )
                    if page_older_than_start:
                        category_reached_start = True
                        break
                if not category_exhausted and not category_reached_start:
                    coverage.truncated = True
                all_reached_start = all_reached_start and category_reached_start
                all_reached_end = all_reached_end and category_reached_end
        coverage.first_date = oldest_seen
        coverage.last_date = newest_seen
        coverage.reached_start = all_reached_start
        coverage.reached_end = all_reached_end
        if errors:
            coverage.message = "；".join(errors[:5])
        if coverage.truncated:
            suffix = "静态归档达到分页上限，未证明覆盖完整日期范围"
            coverage.message = "；".join(filter(None, (coverage.message, suffix)))
        if parse_errors and not readable_channels:
            raise FetchError(parse_errors[0])
