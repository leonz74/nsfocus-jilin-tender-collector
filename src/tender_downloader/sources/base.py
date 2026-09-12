from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Iterator

from ..http_client import HttpClient
from ..htmlparse import parse_html_document
from ..models import AttachmentRef, Coverage, Notice, RawDocument


class SourceAdapter(ABC):
    source_type = "base"
    display_name = "未命名来源"

    def __init__(self, client: HttpClient, config: dict) -> None:
        self.client = client
        self.config = config
        self.authority_rank = int(config.get("authority_rank", 50))
        self.name = str(config.get("name", self.display_name))

    def prepare(self) -> None:
        """建立浏览会话；只可使用用户明确提供的合法凭证正常登录。

        实现不得破解验证码、绕过双因素认证/CA 或规避其他访问控制。
        """

    def close(self) -> None:
        """Release a source-specific session, if the adapter owns one."""

    def fetch_detail(self, notice: Notice) -> RawDocument:
        result = self.client.request(notice.url)
        return RawDocument(
            body=result.body,
            url=result.url,
            headers=result.headers,
            analysis_body=result.analysis_body,
        )

    def discover_artifacts(
        self, notice: Notice, detail: RawDocument
    ) -> list[AttachmentRef]:
        content_type = detail.headers.get("content-type", "")
        analysis_body = (
            detail.analysis_body
            if detail.analysis_body is not None
            else detail.body
        )
        return list(
            parse_html_document(analysis_body, detail.url, content_type).attachments
        )

    @staticmethod
    def artifact_headers(artifact: AttachmentRef) -> dict[str, str]:
        return {"Referer": artifact.referer} if artifact.referer else {}

    @abstractmethod
    def iter_notices(self, start: date, end: date, coverage: Coverage) -> Iterator[Notice]:
        raise NotImplementedError
