from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Iterator

from ..models import Coverage, Notice
from ..utils import stable_id
from .base import SourceAdapter


class URLSeedSource(SourceAdapter):
    source_type = "url_seed"
    display_name = "官方 URL 补录"

    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        path = Path(str(self.config["path"]))
        if not path.is_absolute():
            path = Path(str(self.config.get("_base_dir", "."))) / path
        path = path.resolve()
        if not path.exists():
            coverage.status = "failed"
            coverage.message = f"补录文件不存在: {path}"
            return
        missing_dates = 0
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                published_text = (row.get("published_at") or "").strip()[:10]
                if not published_text:
                    missing_dates += 1
                    continue
                try:
                    published = date.fromisoformat(published_text)
                except ValueError:
                    missing_dates += 1
                    continue
                if not (start <= published <= end):
                    continue
                url = (row.get("url") or "").strip()
                if not url:
                    continue
                coverage.notices += 1
                yield Notice(
                    source=(row.get("source") or self.name).strip(),
                    authority_rank=self.authority_rank,
                    external_id=(row.get("external_id") or stable_id(url)).strip(),
                    title=(row.get("title") or url).strip(),
                    published_at=published_text,
                    url=url,
                    region=(row.get("region") or "吉林省").strip(),
                    notice_type=(row.get("notice_type") or "官方补录").strip(),
                    buyer=(row.get("buyer") or "").strip(),
                )
        coverage.first_date = start.isoformat()
        coverage.last_date = end.isoformat()
        coverage.reached_start = True
        coverage.reached_end = True
        if missing_dates:
            coverage.status = "partial"
            coverage.message = f"补录中有 {missing_dates} 条缺少有效 published_at，已跳过"
