from __future__ import annotations

import csv
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tender_downloader.database import Database
from tender_downloader.export import (
    export_current_results,
    export_verified_manifest,
    verify_files,
)
from tender_downloader.field_extract import ExtractedFields, extract_structured_fields
from tender_downloader.models import Classification, Notice
from tender_downloader.storage import ImmutableStore


class VerifiedExportTests(unittest.TestCase):
    @staticmethod
    def _notice(*, url: str = "https://www.ccgp.gov.cn/notice/1") -> Notice:
        return Notice(
            source="fixture-official",
            authority_rank=100,
            external_id="JL-SEC-1",
            title="网络安全设备采购项目",
            published_at="2026-08-21",
            url=url,
            buyer="吉林省测试单位",
            source_role="official",
            discovery_url=url,
            publication_url=url,
            origin_url=url,
            origin_verified=True,
        )

    @staticmethod
    def _classification() -> Classification:
        return Classification(
            relevant=True,
            confidence=0.98,
            industry="政务",
            security_categories=("网络安全设备",),
            evidence=("网络安全设备采购",),
            reason="fixture",
            method="fixture-ai",
            ai_confirmed=True,
            needs_review=False,
        )

    @staticmethod
    def _official(artifact, notice: Notice):
        return replace(
            artifact,
            final_url=artifact.source_url,
            document_kind="official_pdf",
            source_role="official",
            origin_verified=True,
            gate_status="allowed",
            gate_code="",
            delivery_eligible=True,
            discovery_url=notice.discovery_url,
            official_notice_url=notice.origin_url,
        )

    def test_verified_exports_and_verify_only_accept_current_https_official(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            store = ImmutableStore(root / "files")
            notice = self._notice()
            db.upsert_notice(notice, status="delivered")

            raw = store.ingest_bytes(
                b"%PDF-1.4\nfixture\n%%EOF\n",
                original_name="security.pdf",
                source_url="https://www.ccgp.gov.cn/files/security.pdf",
                content_type="application/pdf",
            )
            official = self._official(raw, notice)
            delivery = root / "delivery_verified" / "security.pdf"
            delivery.parent.mkdir(parents=True)
            shutil.copyfile(official.vault_path, delivery)
            db.record_artifact(
                notice, official, status="delivered", delivery_path=delivery
            )
            db.record_classification(
                notice, self._classification(), artifacts=(official,)
            )
            db.record_structured_fields(
                notice,
                ExtractedFields(
                    event_type="招标公告",
                    budget_amount_minor=99_999_999_999_999_993,
                    published_at="2026-08-21",
                    evidence={"budget_amount_minor": "预算金额：999999999999999.93元"},
                ),
            )

            lead_raw = store.ingest_evidence_bytes(
                b"commercial copy",
                original_name="copy.html",
                source_url="https://www.okcis.cn/copy/1",
                content_type="text/html",
            )
            lead = replace(
                lead_raw,
                final_url=lead_raw.source_url,
                document_kind="lead_evidence",
                source_role="commercial_lead",
                official_notice_url=notice.origin_url,
            )
            db.record_artifact(notice, lead, status="lead_evidence")

            verified_path = export_verified_manifest(db, root)
            current_path = export_current_results(db, root)
            checked, errors = verify_files(db)
            with verified_path.open(encoding="utf-8-sig", newline="") as handle:
                verified = list(csv.DictReader(handle))
            with current_path.open(encoding="utf-8-sig", newline="") as handle:
                current = list(csv.DictReader(handle))
            db.close()

        self.assertFalse(errors)
        self.assertEqual(3, checked)  # 两个原始文件路径 + 一个交付副本
        self.assertEqual(1, len(verified))
        self.assertEqual("official", verified[0]["source_role"])
        self.assertEqual("official", verified[0]["notice_source_role"])
        self.assertEqual("1", verified[0]["notice_origin_verified"])
        self.assertEqual(
            "999999999999999.93", current[0]["budget_amount_yuan"]
        )

    def test_new_rejected_hash_hides_historical_allowed_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            store = ImmutableStore(root / "files")
            notice = self._notice()
            db.upsert_notice(notice)
            url = "https://www.ccgp.gov.cn/files/revised.pdf"
            old = self._official(
                store.ingest_bytes(b"old", original_name="a.pdf", source_url=url),
                notice,
            )
            new = replace(
                store.ingest_bytes(b"new", original_name="a.pdf", source_url=url),
                final_url=url,
                document_kind="gate_rejected",
                source_role="official",
                origin_verified=False,
                gate_status="rejected",
                gate_code="captcha_page",
                delivery_eligible=False,
                discovery_url=notice.discovery_url,
                official_notice_url=notice.origin_url,
            )
            db.record_artifact(notice, old, status="verified")
            db.record_artifact(notice, new, status="gate_rejected")
            rows = db.verified_artifact_rows()
            db.close()

        self.assertEqual([], rows)

    def test_reopen_revokes_legacy_http_delivery_without_deleting_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db_path = root / "state.sqlite3"
            db = Database(db_path)
            store = ImmutableStore(root / "files")
            notice = self._notice(url="http://www.ccgp.gov.cn/notice/legacy")
            db.upsert_notice(notice, status="delivered")
            url = "http://www.ccgp.gov.cn/files/legacy.pdf"
            artifact = self._official(
                store.ingest_bytes(b"legacy", original_name="a.pdf", source_url=url),
                notice,
            )
            artifact = replace(
                artifact,
                final_url=url,
                official_notice_url=notice.origin_url,
            )
            delivery = root / "delivery" / "legacy.pdf"
            delivery.parent.mkdir()
            shutil.copyfile(artifact.vault_path, delivery)
            db.record_artifact(
                notice, artifact, status="delivered", delivery_path=delivery
            )
            db.close()

            reopened = Database(db_path)
            current = reopened.connection.execute(
                "SELECT * FROM artifacts"
            ).fetchone()
            version = reopened.connection.execute(
                "SELECT * FROM artifact_versions"
            ).fetchone()
            notice_row = reopened.connection.execute(
                "SELECT * FROM notices"
            ).fetchone()
            _, errors = verify_files(reopened)
            reopened.close()

            self.assertTrue(delivery.exists())

        self.assertEqual(0, current["delivery_eligible"])
        self.assertEqual(0, version["delivery_eligible"])
        self.assertEqual("revoked_insecure_transport", current["gate_status"])
        self.assertEqual("revoked_insecure_transport", version["gate_status"])
        self.assertEqual("unresolved_official_source", notice_row["status"])
        self.assertTrue(any("没有当前有效" in error for error in errors))
        self.assertTrue(any("仍有交付路径" in error for error in errors))


class FieldExtractionRegressionTests(unittest.TestCase):
    def test_parenthesized_amount_unit_is_respected(self) -> None:
        notice = VerifiedExportTests._notice()
        fields = extract_structured_fields(notice, "预算金额（万元）：14")
        self.assertEqual(14_000_000, fields.budget_amount_minor)

    def test_tender_amount_label_maps_to_budget_without_touching_award(self) -> None:
        notice = VerifiedExportTests._notice()
        fields = extract_structured_fields(
            notice, "招标金额：125.50万元\n中标金额：120万元"
        )
        self.assertEqual(125_500_000, fields.budget_amount_minor)
        self.assertEqual(120_000_000, fields.award_amount_minor)

    def test_invalid_expected_purchase_month_is_not_emitted(self) -> None:
        notice = replace(
            VerifiedExportTests._notice(), notice_type="采购意向"
        )
        labeled = extract_structured_fields(notice, "预计采购时间：2026年13月")
        standalone = extract_structured_fields(
            notice, "采购意向\n2026年13月"
        )
        self.assertEqual("", labeled.expected_purchase_date)
        self.assertEqual("", standalone.expected_purchase_date)


if __name__ == "__main__":
    unittest.main()
