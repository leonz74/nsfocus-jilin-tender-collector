from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tender_downloader.config import AppConfig
from tender_downloader.database import Database
from tender_downloader.http_client import HttpResult
from tender_downloader.models import Notice, RawDocument
from tender_downloader.pipeline import Pipeline
from tender_downloader.storage import ImmutableStore


GROUP_URL = (
    "https://cgyx.ccgp.gov.cn/cgyx/pub/details?"
    "groupId=c477f86f-22b3-422a-b711-ddc7bce5cbd1"
)
PROJECT_URL = (
    "https://cgyx.ccgp.gov.cn/cgyx/pub/proJ/details?"
    "projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc"
)


class _UnusedClassifier:
    def classify(self, notice, text, *, stage="final"):
        raise AssertionError("refinement test must not call AI")


class _FixtureClient:
    def __init__(self, group_body: bytes, project_body: bytes) -> None:
        self.group_body = group_body
        self.project_body = project_body
        self.requests: list[str] = []

    def request(self, url: str):
        self.requests.append(url)
        if url == GROUP_URL:
            return HttpResult(
                url=GROUP_URL,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=self.group_body,
            )
        if url == PROJECT_URL:
            return HttpResult(
                url=PROJECT_URL,
                status=200,
                headers={"content-type": "text/html; charset=utf-8"},
                body=self.project_body,
            )
        raise AssertionError(f"unexpected URL: {url}")


class OfficialGroupPipelineTests(unittest.TestCase):
    def test_group_page_is_only_an_intermediate_and_project_page_is_verified(self) -> None:
        title = (
            "中国人民银行吉林省分行2026年8至12月政府采购意向-"
            "网络安全设备维保服务项目"
        )
        buyer = "中国人民银行吉林省分行"
        group_body = f"""
        <html><body><table><tr>
          <td>{buyer}</td>
          <td><a href="/cgyx/pub/proJ/details?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc">网络安全设备维保服务项目</a></td>
          <td>14.000000</td>
          <td><a href="/cgyx/pub/proJ/details?projId=fe4ba317-1bfd-46d6-8d56-fa8e2f0232bc">详见项目详情</a></td>
        </tr></table></body></html>
        """.encode("utf-8")
        project_body = f"""
        <html><head><title>{title}</title></head><body>
          <h1>{title}</h1><p>采购人：{buyer}</p>
          <p>本项目采购网络安全设备维保服务，包括防火墙、入侵防御系统和日志审计系统维护。
          预算金额（万元）：14，预计采购时间：2026年10月。以上为项目官方详情内容。</p>
        </body></html>
        """.encode("utf-8")
        lead = RawDocument(
            body=(
                f'<html><body><a href="{GROUP_URL.replace("https://", "http://")}">'
                "中国政府采购网公告原文</a></body></html>"
            ).encode("utf-8"),
            url="https://www.okcis.cn/lead/1.html",
            headers={"content-type": "text/html; charset=utf-8"},
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(root / "config.json", {
                "start_date": "2026-01-01",
                "end_date": "2026-08-21",
                "output_dir": str(root / "output"),
                "database": str(root / "output" / "state.sqlite3"),
                "sources": [],
                "ai": {},
                "delivery": {"mode": "automatic"},
            })
            db = Database(config.database_path)
            notice = Notice(
                source="OKCIS fixture",
                authority_rank=40,
                external_id="okcis-1",
                title=title,
                published_at="2026-08-12",
                url=lead.url,
                buyer=buyer,
                source_role="commercial_lead",
                discovery_url=lead.url,
            )
            db.upsert_notice(notice)
            client = _FixtureClient(group_body, project_body)
            pipeline = Pipeline(
                config=config,
                client=client,  # type: ignore[arg-type]
                db=db,
                store=ImmutableStore(config.output_dir),
                classifier=_UnusedClassifier(),
                sources=[],
            )
            verified = pipeline._resolve_from_lead(notice, lead)
            artifacts = db.artifact_rows()
            provenance = db.provenance_rows()
            db.close()

        self.assertIsNotNone(verified)
        assert verified is not None
        self.assertEqual(PROJECT_URL, verified.requested_url)
        self.assertEqual([GROUP_URL, PROJECT_URL], client.requests)
        self.assertEqual(1, len(artifacts))
        self.assertEqual(PROJECT_URL, artifacts[0]["source_url"])
        self.assertNotEqual(GROUP_URL, artifacts[0]["source_url"])
        self.assertEqual(1, artifacts[0]["delivery_eligible"])
        by_relation = {row["relation"]: row for row in provenance}
        self.assertEqual(0, by_relation["official_group_intermediate"]["verified"])
        self.assertEqual(GROUP_URL, by_relation["official_group_intermediate"]["official_url"])
        self.assertEqual(1, by_relation["official_original"]["verified"])
        self.assertEqual(PROJECT_URL, by_relation["official_original"]["official_url"])


if __name__ == "__main__":
    unittest.main()
