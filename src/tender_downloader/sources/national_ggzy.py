from __future__ import annotations

import json
import html
import re
from datetime import date, timedelta
from typing import Iterator
from urllib.parse import urljoin, urlsplit

from ..http_client import FetchError, SourceBlocked
from ..models import Coverage, Notice, RawDocument
from ..htmlparse import decode_html
from ..utils import stable_id
from .base import SourceAdapter


class NationalGGZYSource(SourceAdapter):
    source_type = "national_ggzy"
    display_name = "全国公共资源交易平台（吉林）"
    endpoint = "https://www.ggzy.gov.cn/information/pubTradingInfo/getTradList"

    def fetch_detail(self, notice: Notice) -> RawDocument:
        document = super().fetch_detail(notice)
        if "/information/deal/html/a/" not in urlsplit(document.url).path:
            return document
        text = decode_html(document.body, document.headers.get("content-type", ""))
        match = re.search(r"\bfirstLastUrl\s*=\s*['\"]([^'\"]+)['\"]", text)
        if not match:
            raise FetchError("全国平台目录页缺少公告正文链接")
        url = urljoin(document.url, html.unescape(match.group(1)))
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "www.ggzy.gov.cn"
                or parsed.username or parsed.password or parsed.port not in (None, 443)
                or not re.fullmatch(r"/information/deal/html/b/\d{6}/\d{4}/\d{8}/[A-Za-z0-9]+\.html", parsed.path)
                or parsed.path.rsplit("/", 1)[-1] != urlsplit(document.url).path.rsplit("/", 1)[-1]):
            raise FetchError("全国平台正文链接与当前公告不匹配")
        response = self.client.request(url, headers={"Referer": document.url})
        notice.discovery_url = notice.discovery_url or notice.url
        notice.metadata["directory_url"] = document.url
        notice.url = url
        return RawDocument(body=response.body, url=response.url, headers=response.headers,
                           analysis_body=response.analysis_body)

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        keywords = self.config.get("_query_terms")
        if not keywords:
            yield from self._iter_notices(start, end, coverage)
            return
        seen: set[str] = set()
        coverage.reached_start = True
        coverage.reached_end = True
        for keyword in keywords:
            child = Coverage(source=self.name, target_start=start.isoformat(), target_end=end.isoformat())
            self.config["_query_keyword"] = keyword
            try:
                for notice in self._iter_notices(start, end, child):
                    if notice.url not in seen:
                        seen.add(notice.url)
                        coverage.notices += 1
                        yield notice
            finally:
                coverage.pages += child.pages
                coverage.truncated = coverage.truncated or child.truncated
                coverage.reached_start = coverage.reached_start and child.reached_start
                coverage.reached_end = coverage.reached_end and child.reached_end
                dates = [x for x in (coverage.first_date, child.first_date) if x]
                coverage.first_date = min(dates) if dates else ""
                coverage.last_date = max(coverage.last_date, child.last_date)
                if child.message:
                    coverage.message = child.message

    def _iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        from ..geography import source_region, province_alias
        selected_region = source_region(self.config)
        source_types = [str(value) for value in self.config.get("source_types", ["1", "3"])]
        deal_types = [str(value) for value in self.config.get("deal_types", ["01", "02"])]
        window_days = max(1, int(self.config.get("window_days", 14)))
        max_pages = int(self.config.get("max_pages_per_window", 50))
        seen: set[str] = set()
        truncated = False
        cursor = start
        while cursor <= end:
            window_end = min(end, cursor + timedelta(days=window_days - 1))
            for source_type in source_types:
                for deal_type in deal_types:
                    if source_type == "3" and deal_type != "02":
                        continue
                    for page in range(1, max_pages + 1):
                        fields = {
                            "DEAL_CLASSIFY": deal_type,
                            "SOURCE_TYPE": source_type,
                            "DEAL_TIME": "06",
                            "TIMEBEGIN": cursor.isoformat(),
                            "TIMEEND": window_end.isoformat(),
                            "PAGENUMBER": str(page),
                        }
                        if selected_region["province_code"]:
                            fields["DEAL_PROVINCE"] = selected_region["province_code"]
                        # County-level direct administrations are not ordinary
                        # DEAL_CITY codes. Keep the province scan for those;
                        # the catalogue filters their explicit location later.
                        if (selected_region["city_code"].endswith("00")
                                and selected_region["city_code"] != selected_region["province_code"]):
                            fields["DEAL_CITY"] = selected_region["city_code"]
                        if self.config.get("_query_keyword"):
                            fields["FINDTXT"] = self.config["_query_keyword"]
                        result = self.client.post_form(
                            self.endpoint,
                            fields,
                            headers={"Referer": "https://www.ggzy.gov.cn/deal/dealList.html"},
                        )
                        coverage.pages += 1
                        payload = json.loads(result.body.decode("utf-8", errors="replace"))
                        code = int(payload.get("code", 0))
                        if code in (800, 829):
                            raise SourceBlocked(str(payload.get("message") or "全国平台要求验证码/休息"))
                        if code != 200:
                            raise FetchError(f"全国平台返回 code={code}: {payload.get('message', '')}")
                        data = payload.get("data")
                        if not isinstance(data, dict) or "records" not in data or "pages" not in data:
                            raise FetchError("全国平台成功响应缺少 records/pages，接口结构可能变化")
                        records = data.get("records")
                        if not isinstance(records, list):
                            raise FetchError("全国平台 records 不是列表")
                        reported_pages = int(data.get("pages") or 0)
                        if records and reported_pages < page:
                            raise FetchError("全国平台分页总数小于当前非空页")
                        for record in records:
                            raw_url = str(record.get("url") or "").strip()
                            if not raw_url:
                                raise FetchError("全国平台记录缺少详情 URL")
                            url = urljoin("https://www.ggzy.gov.cn/", raw_url)
                            if url in seen:
                                continue
                            published = str(record.get("publishTime") or "")[:10]
                            try:
                                published_date = date.fromisoformat(published)
                            except ValueError as exc:
                                raise FetchError("全国平台记录发布时间无效") from exc
                            if not (cursor <= published_date <= window_end):
                                raise FetchError("全国平台记录发布时间超出请求窗口")
                            region = str(record.get("provinceText") or "")
                            if selected_region["province"] and province_alias(selected_region["province"]) not in region:
                                raise FetchError(f"全国平台返回所选省份以外的记录: {region}")
                            if record.get("cityText"):
                                region += "-" + str(record["cityText"])
                            seen.add(url)
                            external_id = str(record.get("id") or stable_id(url))
                            coverage.notices += 1
                            yield Notice(
                                source=self.name,
                                authority_rank=self.authority_rank,
                                external_id=external_id,
                                title=str(record.get("title") or "").strip(),
                                published_at=published,
                                url=url,
                                region=region,
                                notice_type=(
                                    f"{record.get('businessTypeText', '')}/{record.get('informationTypeText', '')}"
                                ).strip("/"),
                                metadata={
                                    "source_type": source_type,
                                    "source_platform": record.get("transactionSourcesPlatformText", ""),
                                    "industry": record.get("industryTypeText", ""),
                                },
                            )
                        if reported_pages > max_pages:
                            truncated = True
                        pages = min(reported_pages, max_pages)
                        if not records or page >= pages:
                            break
            cursor = window_end + timedelta(days=1)
        coverage.first_date = start.isoformat()
        coverage.last_date = end.isoformat()
        coverage.truncated = truncated
        coverage.reached_start = not truncated
        coverage.reached_end = not truncated
        if truncated:
            coverage.message = "至少一个日期窗口达到分页上限，结果被截断"
