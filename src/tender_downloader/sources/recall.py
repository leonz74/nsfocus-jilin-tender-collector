"""Keep the ordinary search, then add a bounded date-only recall pass."""
from __future__ import annotations

from ..models import Coverage


class RecallSource:
    def __init__(self, source, limit: int):
        self.source = source
        self.limit = limit

    def __getattr__(self, name):
        return getattr(self.source, name)

    def iter_notices(self, start, end, coverage):
        baseline = self.config["_baseline_terms"]
        phases = ([("baseline", baseline)] if any(baseline) else []) + [("broad", [""])]
        seen = set()
        complete_start = complete_end = True
        original_terms = self.config.get("_query_terms")
        try:
            for origin, terms in phases:
                self.config["_query_terms"] = terms
                child = Coverage(source=self.name, target_start=start.isoformat(), target_end=end.isoformat())
                pages = 0
                added = 0
                iterator = self.source.iter_notices(start, end, child)
                try:
                    for notice in iterator:
                        coverage.pages += child.pages - pages
                        pages = child.pages
                        if notice.identity in seen:
                            continue
                        if origin == "broad" and added >= self.limit:
                            coverage.truncated = True
                            coverage.status = "partial"
                            coverage.message += f" 额外广搜达到 {self.limit} 条处理上限；请缩短日期分段检测。"
                            break
                        seen.add(notice.identity)
                        added += 1
                        coverage.notices += 1
                        notice.metadata["recall_origin"] = origin
                        yield notice
                finally:
                    iterator.close()
                    coverage.pages += child.pages - pages
                    coverage.truncated = coverage.truncated or child.truncated
                    complete_start = complete_start and child.reached_start
                    complete_end = complete_end and child.reached_end
                    coverage.reached_start = complete_start
                    coverage.reached_end = complete_end
                    dates = [value for value in (coverage.first_date, child.first_date) if value]
                    coverage.first_date = min(dates) if dates else ""
                    coverage.last_date = max(coverage.last_date, child.last_date)
                    if child.message and child.message not in coverage.message:
                        coverage.message = (coverage.message + " " + child.message).strip()
        finally:
            self.config["_query_terms"] = original_terms
