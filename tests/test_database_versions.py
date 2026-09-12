from __future__ import annotations

import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from tender_downloader.database import Database
from tender_downloader.export import export_manifest
from tender_downloader.models import Classification, Notice
from tender_downloader.storage import ImmutableStore


class ArtifactVersionTests(unittest.TestCase):
    @staticmethod
    def _classification(
        *, industry: str, category: str, relevant: bool, method: str
    ) -> Classification:
        return Classification(
            relevant=relevant,
            confidence=0.96,
            industry=industry,
            security_categories=(category,),
            evidence=("网络安全设备维保",),
            reason="fixture",
            method=method,
            ai_confirmed=True,
            needs_review=False,
        )

    def test_new_hash_clears_old_delivery_pointer_and_keeps_both_versions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            store = ImmutableStore(root / "files")
            notice = Notice(
                source="fixture", authority_rank=100, external_id="1",
                title="项目", published_at="2026-01-01",
                url="https://example.gov.cn/notice",
            )
            db.upsert_notice(notice)
            old = store.ingest_bytes(
                b"old", original_name="a.pdf",
                source_url="https://example.gov.cn/a.pdf",
            )
            new = store.ingest_bytes(
                b"new", original_name="a.pdf",
                source_url="https://example.gov.cn/a.pdf",
            )
            db.record_artifact(notice, old, status="delivered", delivery_path=root / "old.pdf")
            db.record_artifact(notice, new, status="downloaded")
            current = db.connection.execute(
                "SELECT sha256, status, delivery_path FROM artifacts"
            ).fetchone()
            versions = db.connection.execute(
                "SELECT COUNT(*) AS count FROM artifact_versions"
            ).fetchone()["count"]
            db.close()
        self.assertEqual(new.sha256, current["sha256"])
        self.assertEqual("downloaded", current["status"])
        self.assertIsNone(current["delivery_path"])
        self.assertEqual(2, versions)

    def test_each_hash_exports_its_own_classification_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            store = ImmutableStore(root / "files")
            notice = Notice(
                source="fixture", authority_rank=100, external_id="snapshot",
                title="项目", published_at="2026-01-01",
                url="https://example.gov.cn/notice",
            )
            db.upsert_notice(notice)
            old = store.ingest_bytes(
                b"old", original_name="a.pdf",
                source_url="https://example.gov.cn/a.pdf",
            )
            new = store.ingest_bytes(
                b"new", original_name="a.pdf",
                source_url="https://example.gov.cn/a.pdf",
            )
            old_result = self._classification(
                industry="教育", category="等保测评", relevant=True, method="old-ai"
            )
            new_result = self._classification(
                industry="金融", category="安全运营与运维", relevant=True,
                method="new-ai",
            )

            db.record_artifact(notice, old, status="downloaded")
            db.record_classification(notice, old_result, artifacts=(old,))
            db.record_artifact(
                notice, old, status="delivered", delivery_path=root / "old.pdf"
            )
            db.record_artifact(notice, new, status="downloaded")
            db.record_classification(notice, new_result, artifacts=(new,))
            db.record_artifact(
                notice, new, status="delivered", delivery_path=root / "new.pdf"
            )
            manifest = export_manifest(db, root)
            with manifest.open(encoding="utf-8-sig", newline="") as handle:
                rows = {row["sha256"]: row for row in csv.DictReader(handle)}
            db.close()

        self.assertEqual("教育", rows[old.sha256]["industry"])
        self.assertEqual('["等保测评"]', rows[old.sha256]["categories_json"])
        self.assertEqual("old-ai", rows[old.sha256]["method"])
        self.assertEqual(str(root / "old.pdf"), rows[old.sha256]["delivery_path"])
        self.assertEqual("金融", rows[new.sha256]["industry"])
        self.assertEqual(
            '["安全运营与运维"]', rows[new.sha256]["categories_json"]
        )
        self.assertEqual("new-ai", rows[new.sha256]["method"])
        self.assertEqual(str(root / "new.pdf"), rows[new.sha256]["delivery_path"])

    def test_legacy_version_does_not_borrow_latest_notice_classification(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = Database(root / "state.sqlite3")
            store = ImmutableStore(root / "files")
            notice = Notice(
                source="fixture", authority_rank=100, external_id="legacy",
                title="项目", published_at="2026-01-01",
                url="https://example.gov.cn/notice",
            )
            db.upsert_notice(notice)
            artifact = store.ingest_bytes(
                b"legacy", original_name="a.pdf",
                source_url="https://example.gov.cn/a.pdf",
            )
            db.record_artifact(notice, artifact, status="delivered")
            db.record_classification(
                notice,
                self._classification(
                    industry="金融", category="安全运营与运维",
                    relevant=True, method="latest-ai",
                ),
            )
            row = db.artifact_rows()[0]
            db.close()

        self.assertIsNone(row["industry"])
        self.assertIsNone(row["categories_json"])
        self.assertIsNone(row["method"])

    def test_migrates_legacy_artifact_versions_without_fabricating_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE artifact_versions (
                    notice_identity TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    sha256 TEXT NOT NULL,
                    final_url TEXT,
                    size INTEGER NOT NULL,
                    original_name TEXT NOT NULL,
                    original_name_raw TEXT,
                    vault_path TEXT NOT NULL,
                    content_type TEXT,
                    delivery_path TEXT,
                    status TEXT NOT NULL,
                    error TEXT,
                    first_seen_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (notice_identity, source_url, sha256)
                )
                """
            )
            connection.commit()
            connection.close()

            db = Database(path)
            columns = {
                str(row["name"])
                for row in db.connection.execute("PRAGMA table_info(artifact_versions)")
            }
            db.close()

        self.assertIn("classification_industry", columns)
        self.assertIn("classification_categories_json", columns)
        self.assertIn("classification_updated_at", columns)


if __name__ == "__main__":
    unittest.main()
