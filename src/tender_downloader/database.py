from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime
from functools import wraps
from pathlib import Path
from typing import Any

from .field_extract import ExtractedFields, clean_project_code
from .geography import infer_location
from .htmlparse import attachment_url_error
from .models import AttachmentRef, Classification, Coverage, Notice, StoredArtifact


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS notices (
    identity TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    authority_rank INTEGER NOT NULL,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT,
    url TEXT NOT NULL,
    region TEXT,
    notice_type TEXT,
    buyer TEXT,
    source_role TEXT NOT NULL DEFAULT 'unknown',
    discovery_url TEXT,
    publication_url TEXT,
    origin_url TEXT,
    origin_verified INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'discovered',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
    notice_identity TEXT NOT NULL,
    source_url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    original_name_raw TEXT,
    final_url TEXT,
    vault_path TEXT NOT NULL,
    content_type TEXT,
    document_kind TEXT NOT NULL DEFAULT 'legacy_unverified',
    source_role TEXT NOT NULL DEFAULT 'unknown',
    origin_verified INTEGER NOT NULL DEFAULT 0,
    gate_status TEXT NOT NULL DEFAULT 'legacy_unverified',
    gate_code TEXT,
    delivery_eligible INTEGER NOT NULL DEFAULT 0,
    discovery_url TEXT,
    official_notice_url TEXT,
    delivery_path TEXT,
    status TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notice_identity, source_url),
    FOREIGN KEY (notice_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS artifact_refs (
    notice_identity TEXT NOT NULL,
    source_url TEXT NOT NULL,
    original_name TEXT,
    label TEXT,
    access TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notice_identity, source_url),
    FOREIGN KEY (notice_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS artifact_versions (
    notice_identity TEXT NOT NULL,
    source_url TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    final_url TEXT,
    size INTEGER NOT NULL,
    original_name TEXT NOT NULL,
    original_name_raw TEXT,
    vault_path TEXT NOT NULL,
    content_type TEXT,
    document_kind TEXT NOT NULL DEFAULT 'legacy_unverified',
    source_role TEXT NOT NULL DEFAULT 'unknown',
    origin_verified INTEGER NOT NULL DEFAULT 0,
    gate_status TEXT NOT NULL DEFAULT 'legacy_unverified',
    gate_code TEXT,
    delivery_eligible INTEGER NOT NULL DEFAULT 0,
    discovery_url TEXT,
    official_notice_url TEXT,
    delivery_path TEXT,
    status TEXT NOT NULL,
    error TEXT,
    classification_relevant INTEGER,
    classification_confidence REAL,
    classification_industry TEXT,
    classification_categories_json TEXT,
    classification_evidence_json TEXT,
    classification_reason TEXT,
    classification_method TEXT,
    classification_ai_confirmed INTEGER,
    classification_needs_review INTEGER,
    classification_updated_at TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (notice_identity, source_url, sha256),
    FOREIGN KEY (notice_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS notice_aliases (
    alias_identity TEXT PRIMARY KEY,
    canonical_identity TEXT NOT NULL,
    reason TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (alias_identity) REFERENCES notices(identity),
    FOREIGN KEY (canonical_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS classifications (
    notice_identity TEXT PRIMARY KEY,
    relevant INTEGER,
    confidence REAL NOT NULL,
    industry TEXT NOT NULL,
    categories_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    reason TEXT,
    method TEXT NOT NULL,
    ai_confirmed INTEGER NOT NULL,
    needs_review INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (notice_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS structured_fields (
    notice_identity TEXT PRIMARY KEY,
    event_type TEXT,
    project_code TEXT,
    package_no TEXT,
    intention_amount_minor INTEGER,
    budget_amount_minor INTEGER,
    max_price_minor INTEGER,
    award_amount_minor INTEGER,
    contract_amount_minor INTEGER,
    winning_vendor TEXT,
    city TEXT,
    buyer_contact TEXT,
    buyer_phone TEXT,
    winning_vendor_contact TEXT,
    winning_vendor_phone TEXT,
    project_summary TEXT,
    published_at TEXT,
    bid_deadline TEXT,
    opening_at TEXT,
    award_at TEXT,
    expected_purchase_date TEXT,
    evidence_json TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (notice_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS provenance_edges (
    lead_identity TEXT NOT NULL,
    discovery_url TEXT NOT NULL,
    official_url TEXT NOT NULL,
    relation TEXT NOT NULL,
    method TEXT NOT NULL,
    verified INTEGER NOT NULL DEFAULT 0,
    evidence TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (lead_identity, official_url, relation),
    FOREIGN KEY (lead_identity) REFERENCES notices(identity)
);

CREATE TABLE IF NOT EXISTS coverage (
    run_id TEXT NOT NULL,
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    target_start TEXT,
    target_end TEXT,
    first_date TEXT,
    last_date TEXT,
    reached_start INTEGER NOT NULL DEFAULT 0,
    reached_end INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    pages INTEGER NOT NULL,
    notices INTEGER NOT NULL,
    details_ok INTEGER NOT NULL,
    attachments_found INTEGER NOT NULL,
    restricted_files INTEGER NOT NULL DEFAULT 0,
    downloads_ok INTEGER NOT NULL,
    downloads_failed INTEGER NOT NULL,
    blocked INTEGER NOT NULL,
    message TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        # Sources are collected concurrently (one thread per source adapter);
        # a single connection is shared, so cross-thread use plus a reentrant
        # lock around every public method keeps SQLite access serialized.
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(SCHEMA)
        self._migrate_coverage()
        self._migrate_notices()
        self._migrate_structured_fields()
        self._migrate_artifacts()
        self._migrate_artifact_versions()
        self._backfill_artifact_versions()
        self._revoke_insecure_transport_artifacts()
        self._normalize_legacy_delivery_statuses()

    def _migrate_notices(self) -> None:
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(notices)")
        }
        additions = {
            "source_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "discovery_url": "TEXT",
            "publication_url": "TEXT",
            "origin_url": "TEXT",
            "origin_verified": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE notices ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _migrate_coverage(self) -> None:
        """允许早期 MVP 数据库原地增加覆盖证明字段。"""
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(coverage)")
        }
        additions = {
            "target_start": "TEXT",
            "target_end": "TEXT",
            "reached_start": "INTEGER NOT NULL DEFAULT 0",
            "reached_end": "INTEGER NOT NULL DEFAULT 0",
            "truncated": "INTEGER NOT NULL DEFAULT 0",
            "restricted_files": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE coverage ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _migrate_structured_fields(self) -> None:
        """Add catalogue columns without altering existing extracted values."""

        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(structured_fields)")
        }
        additions = {
            "city": "TEXT",
            "buyer_contact": "TEXT",
            "buyer_phone": "TEXT",
            "winning_vendor_contact": "TEXT",
            "winning_vendor_phone": "TEXT",
            "project_summary": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE structured_fields ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _migrate_artifacts(self) -> None:
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(artifacts)")
        }
        if "original_name_raw" not in existing:
            self.connection.execute(
                "ALTER TABLE artifacts ADD COLUMN original_name_raw TEXT"
            )
            self.connection.execute(
                "UPDATE artifacts SET original_name_raw=original_name "
                "WHERE original_name_raw IS NULL"
            )
        if "final_url" not in existing:
            self.connection.execute("ALTER TABLE artifacts ADD COLUMN final_url TEXT")
            self.connection.execute(
                "UPDATE artifacts SET final_url=source_url WHERE final_url IS NULL"
            )
        additions = {
            "document_kind": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
            "source_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "origin_verified": "INTEGER NOT NULL DEFAULT 0",
            "gate_status": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
            "gate_code": "TEXT",
            "delivery_eligible": "INTEGER NOT NULL DEFAULT 0",
            "discovery_url": "TEXT",
            "official_notice_url": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE artifacts ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _migrate_artifact_versions(self) -> None:
        """为早期版本增加逐文件分类快照，不猜测旧记录的分类归属。"""
        existing = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(artifact_versions)")
        }
        additions = {
            "classification_relevant": "INTEGER",
            "classification_confidence": "REAL",
            "classification_industry": "TEXT",
            "classification_categories_json": "TEXT",
            "classification_evidence_json": "TEXT",
            "classification_reason": "TEXT",
            "classification_method": "TEXT",
            "classification_ai_confirmed": "INTEGER",
            "classification_needs_review": "INTEGER",
            "classification_updated_at": "TEXT",
            "document_kind": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
            "source_role": "TEXT NOT NULL DEFAULT 'unknown'",
            "origin_verified": "INTEGER NOT NULL DEFAULT 0",
            "gate_status": "TEXT NOT NULL DEFAULT 'legacy_unverified'",
            "gate_code": "TEXT",
            "delivery_eligible": "INTEGER NOT NULL DEFAULT 0",
            "discovery_url": "TEXT",
            "official_notice_url": "TEXT",
        }
        for name, declaration in additions.items():
            if name not in existing:
                self.connection.execute(
                    f"ALTER TABLE artifact_versions ADD COLUMN {name} {declaration}"
                )
        self.connection.commit()

    def _backfill_artifact_versions(self) -> None:
        now = self._now()
        self.connection.execute(
            """
            INSERT OR IGNORE INTO artifact_versions (
                notice_identity, source_url, sha256, final_url, size,
                original_name, original_name_raw, vault_path, content_type,
                document_kind, source_role, origin_verified, gate_status,
                gate_code, delivery_eligible, discovery_url, official_notice_url,
                delivery_path, status, error, first_seen_at, updated_at
            )
            SELECT notice_identity, source_url, sha256,
                   COALESCE(final_url, source_url), size, original_name,
                   COALESCE(original_name_raw, original_name), vault_path,
                   content_type, document_kind, source_role, origin_verified,
                   gate_status, gate_code, delivery_eligible, discovery_url,
                   official_notice_url, delivery_path, status, error, ?, updated_at
            FROM artifacts
            """,
            (now,),
        )
        self.connection.commit()

    def _revoke_insecure_transport_artifacts(self) -> None:
        """旧版本曾允许 HTTP 官方页；升级时必须撤销其交付资格。"""

        now = self._now()
        message = "官方原件使用非 HTTPS 传输，已撤销验证与交付资格"
        for table in ("artifacts", "artifact_versions"):
            self.connection.execute(
                f"""
                UPDATE {table}
                SET origin_verified=0,
                    delivery_eligible=0,
                    gate_status='revoked_insecure_transport',
                    gate_code='INSECURE_ORIGIN_TRANSPORT',
                    status='revoked_insecure_transport',
                    error=CASE
                        WHEN COALESCE(error, '')='' THEN ?
                        WHEN instr(error, ?)>0 THEN error
                        ELSE error || '；' || ?
                    END,
                    updated_at=?
                WHERE delivery_eligible=1
                  AND (
                      lower(COALESCE(source_url, '')) LIKE 'http://%'
                      OR lower(COALESCE(final_url, '')) LIKE 'http://%'
                      OR lower(COALESCE(official_notice_url, '')) LIKE 'http://%'
                  )
                """,
                (message, message, message, now),
            )
        self.connection.execute(
            """
            UPDATE notices
            SET origin_verified=0,
                status='unresolved_official_source',
                updated_at=?
            WHERE origin_verified=1
              AND (
                  lower(COALESCE(publication_url, '')) LIKE 'http://%'
                  OR lower(COALESCE(origin_url, '')) LIKE 'http://%'
              )
              AND NOT EXISTS (
                  SELECT 1 FROM artifacts a
                  WHERE a.notice_identity=notices.identity
                    AND a.delivery_eligible=1
                    AND a.origin_verified=1
                    AND a.gate_status='allowed'
              )
            """,
            (now,),
        )
        self.connection.commit()

    def _normalize_legacy_delivery_statuses(self) -> None:
        """保留旧路径供审计，但禁止继续显示为已验证交付。"""

        now = self._now()
        for table in ("artifacts", "artifact_versions"):
            self.connection.execute(
                f"""
                UPDATE {table}
                SET status='legacy_delivered_unverified', updated_at=?
                WHERE delivery_eligible=0
                  AND gate_status='legacy_unverified'
                  AND status IN ('delivered', 'review')
                """,
                (now,),
            )
        self.connection.execute(
            """
            UPDATE notices
            SET status='legacy_unverified', updated_at=?
            WHERE status IN ('delivered', 'review')
              AND NOT (
                  status='review' AND origin_verified=1
                  AND json_extract(metadata_json, '$.scan_sha256') IS NOT NULL
                  AND NOT EXISTS (
                      SELECT 1 FROM artifacts a WHERE a.notice_identity=notices.identity
                  )
              )
              AND NOT EXISTS (
                  SELECT 1 FROM artifacts a
                  WHERE a.notice_identity=notices.identity
                    AND a.delivery_eligible=1
                    AND a.origin_verified=1
                    AND a.gate_status='allowed'
              )
            """,
            (now,),
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    @staticmethod
    def _now() -> str:
        return datetime.now().astimezone().isoformat()

    def upsert_notice(self, notice: Notice, status: str = "discovered") -> None:
        self.connection.execute(
            """
            INSERT INTO notices (
                identity, source, authority_rank, external_id, title, published_at, url,
                region, notice_type, buyer, source_role, discovery_url,
                publication_url, origin_url, origin_verified,
                metadata_json, status, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(identity) DO UPDATE SET
                title=excluded.title, published_at=excluded.published_at, url=excluded.url,
                region=excluded.region, notice_type=excluded.notice_type, buyer=excluded.buyer,
                source_role=excluded.source_role,
                discovery_url=excluded.discovery_url,
                publication_url=excluded.publication_url,
                origin_url=excluded.origin_url,
                origin_verified=excluded.origin_verified,
                metadata_json=excluded.metadata_json, status=excluded.status, updated_at=excluded.updated_at
            """,
            (
                notice.identity, notice.source, notice.authority_rank, notice.external_id,
                notice.title, notice.published_at, notice.url, notice.region, notice.notice_type,
                notice.buyer, notice.source_role,
                notice.discovery_url or notice.url,
                notice.publication_url or notice.url,
                notice.origin_url or notice.publication_url or notice.url,
                int(notice.origin_verified),
                json.dumps(notice.metadata, ensure_ascii=False), status, self._now(),
            ),
        )
        self.connection.commit()

    def set_notice_status(self, identity: str, status: str) -> None:
        self.connection.execute(
            "UPDATE notices SET status=?, updated_at=? WHERE identity=?",
            (status, self._now(), identity),
        )
        self.connection.commit()

    def notice_status(self, identity: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM notices WHERE identity=?", (identity,)
        ).fetchone()
        return str(row["status"]) if row else None

    def artifact_sha(self, notice_identity: str, source_url: str) -> str | None:
        row = self.connection.execute(
            "SELECT sha256 FROM artifacts WHERE notice_identity=? AND source_url=?",
            (notice_identity, source_url),
        ).fetchone()
        return str(row["sha256"]) if row else None

    def record_artifact(
        self,
        notice: Notice,
        artifact: StoredArtifact,
        *,
        status: str,
        delivery_path: Path | None = None,
        error: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO artifacts (
                notice_identity, source_url, sha256, size, original_name, vault_path,
                original_name_raw, final_url, content_type, document_kind, source_role,
                origin_verified, gate_status, gate_code, delivery_eligible,
                discovery_url, official_notice_url, delivery_path, status, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_identity, source_url) DO UPDATE SET
                sha256=excluded.sha256, size=excluded.size, original_name=excluded.original_name,
                original_name_raw=excluded.original_name_raw, vault_path=excluded.vault_path,
                final_url=excluded.final_url, content_type=excluded.content_type,
                document_kind=excluded.document_kind,
                source_role=excluded.source_role,
                origin_verified=excluded.origin_verified,
                gate_status=excluded.gate_status,
                gate_code=excluded.gate_code,
                delivery_eligible=excluded.delivery_eligible,
                discovery_url=excluded.discovery_url,
                official_notice_url=excluded.official_notice_url,
                delivery_path=CASE
                    WHEN artifacts.sha256=excluded.sha256
                        THEN COALESCE(excluded.delivery_path, artifacts.delivery_path)
                    ELSE excluded.delivery_path
                END,
                status=CASE
                    WHEN artifacts.sha256=excluded.sha256
                         AND artifacts.status IN ('delivered', 'review')
                         AND excluded.status IN ('downloaded', 'verified') THEN artifacts.status
                    ELSE excluded.status
                END,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            (
                notice.identity, artifact.source_url, artifact.sha256, artifact.size,
                artifact.original_name, str(artifact.vault_path),
                artifact.original_name_raw or artifact.original_name,
                artifact.final_url or artifact.source_url, artifact.content_type,
                artifact.document_kind, artifact.source_role,
                int(artifact.origin_verified), artifact.gate_status,
                artifact.gate_code, int(artifact.delivery_eligible),
                artifact.discovery_url or notice.discovery_url or notice.url,
                artifact.official_notice_url or notice.origin_url or notice.publication_url,
                str(delivery_path) if delivery_path else None, status, error, self._now(),
            ),
        )
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO artifact_versions (
                notice_identity, source_url, sha256, final_url, size,
                original_name, original_name_raw, vault_path, content_type,
                document_kind, source_role, origin_verified, gate_status, gate_code,
                delivery_eligible, discovery_url, official_notice_url,
                delivery_path, status, error, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_identity, source_url, sha256) DO UPDATE SET
                final_url=excluded.final_url, size=excluded.size,
                original_name=excluded.original_name,
                original_name_raw=excluded.original_name_raw,
                vault_path=excluded.vault_path, content_type=excluded.content_type,
                document_kind=excluded.document_kind,
                source_role=excluded.source_role,
                origin_verified=excluded.origin_verified,
                gate_status=excluded.gate_status,
                gate_code=excluded.gate_code,
                delivery_eligible=excluded.delivery_eligible,
                discovery_url=excluded.discovery_url,
                official_notice_url=excluded.official_notice_url,
                delivery_path=COALESCE(excluded.delivery_path, artifact_versions.delivery_path),
                status=CASE
                    WHEN artifact_versions.status IN ('delivered', 'review')
                         AND excluded.status IN ('downloaded', 'verified') THEN artifact_versions.status
                    ELSE excluded.status
                END,
                error=excluded.error, updated_at=excluded.updated_at
            """,
            (
                notice.identity, artifact.source_url, artifact.sha256,
                artifact.final_url or artifact.source_url, artifact.size,
                artifact.original_name, artifact.original_name_raw or artifact.original_name,
                str(artifact.vault_path), artifact.content_type,
                artifact.document_kind, artifact.source_role,
                int(artifact.origin_verified), artifact.gate_status,
                artifact.gate_code, int(artifact.delivery_eligible),
                artifact.discovery_url or notice.discovery_url or notice.url,
                artifact.official_notice_url or notice.origin_url or notice.publication_url,
                str(delivery_path) if delivery_path else None, status, error, now, now,
            ),
        )
        self.connection.commit()

    def record_structured_fields(
        self, notice: Notice, fields: ExtractedFields
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO structured_fields (
                notice_identity, event_type, project_code, package_no,
                intention_amount_minor, budget_amount_minor, max_price_minor,
                award_amount_minor, contract_amount_minor, winning_vendor,
                city, buyer_contact, buyer_phone, winning_vendor_contact,
                winning_vendor_phone, project_summary,
                published_at, bid_deadline, opening_at, award_at,
                expected_purchase_date, evidence_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_identity) DO UPDATE SET
                event_type=excluded.event_type,
                project_code=excluded.project_code,
                package_no=excluded.package_no,
                intention_amount_minor=excluded.intention_amount_minor,
                budget_amount_minor=excluded.budget_amount_minor,
                max_price_minor=excluded.max_price_minor,
                award_amount_minor=excluded.award_amount_minor,
                contract_amount_minor=excluded.contract_amount_minor,
                winning_vendor=excluded.winning_vendor,
                city=excluded.city,
                buyer_contact=excluded.buyer_contact,
                buyer_phone=excluded.buyer_phone,
                winning_vendor_contact=excluded.winning_vendor_contact,
                winning_vendor_phone=excluded.winning_vendor_phone,
                project_summary=excluded.project_summary,
                published_at=excluded.published_at,
                bid_deadline=excluded.bid_deadline,
                opening_at=excluded.opening_at,
                award_at=excluded.award_at,
                expected_purchase_date=excluded.expected_purchase_date,
                evidence_json=excluded.evidence_json,
                updated_at=excluded.updated_at
            """,
            (
                notice.identity, fields.event_type, fields.project_code,
                fields.package_no, fields.intention_amount_minor,
                fields.budget_amount_minor, fields.max_price_minor,
                fields.award_amount_minor, fields.contract_amount_minor,
                fields.winning_vendor, fields.city, fields.buyer_contact,
                fields.buyer_phone, fields.winning_vendor_contact,
                fields.winning_vendor_phone, fields.project_summary,
                fields.published_at,
                fields.bid_deadline, fields.opening_at, fields.award_at,
                fields.expected_purchase_date,
                json.dumps(fields.evidence, ensure_ascii=False), self._now(),
            ),
        )
        self.connection.commit()

    def record_provenance_edge(
        self,
        notice: Notice,
        *,
        discovery_url: str,
        official_url: str,
        relation: str,
        method: str,
        verified: bool,
        evidence: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO provenance_edges (
                lead_identity, discovery_url, official_url, relation,
                method, verified, evidence, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lead_identity, official_url, relation) DO UPDATE SET
                discovery_url=excluded.discovery_url,
                method=excluded.method,
                verified=excluded.verified,
                evidence=excluded.evidence,
                updated_at=excluded.updated_at
            """,
            (
                notice.identity, discovery_url, official_url, relation,
                method, int(verified), evidence[:1000], self._now(),
            ),
        )
        self.connection.commit()

    def record_artifact_ref(
        self,
        notice: Notice,
        reference: AttachmentRef,
        *,
        status: str,
        error: str = "",
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO artifact_refs (
                notice_identity, source_url, original_name, label, access,
                status, error, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_identity, source_url) DO UPDATE SET
                original_name=excluded.original_name, label=excluded.label,
                access=excluded.access,
                status=CASE WHEN excluded.status IN ('available','discovered')
                                  AND artifact_refs.status IN ('downloaded','failed','blocked')
                            THEN artifact_refs.status ELSE excluded.status END,
                error=CASE WHEN excluded.status IN ('available','discovered')
                                 AND artifact_refs.status IN ('downloaded','failed','blocked')
                           THEN artifact_refs.error ELSE excluded.error END,
                updated_at=excluded.updated_at
            """,
            (
                notice.identity, reference.url, reference.original_name,
                reference.label, reference.access, status, error, self._now(),
            ),
        )
        self.connection.commit()

    def record_notice_alias(
        self, alias: Notice, canonical_identity: str, reason: str
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO notice_aliases (
                alias_identity, canonical_identity, reason, updated_at
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(alias_identity) DO UPDATE SET
                canonical_identity=excluded.canonical_identity,
                reason=excluded.reason, updated_at=excluded.updated_at
            """,
            (alias.identity, canonical_identity, reason, self._now()),
        )
        self.connection.commit()

    def record_classification(
        self,
        notice: Notice,
        result: Classification,
        *,
        artifacts: Iterable[StoredArtifact] = (),
    ) -> None:
        """保存公告分类，并把它快照到本次实际参与分类的文件版本。"""
        relevant = None if result.relevant is None else int(result.relevant)
        categories_json = json.dumps(result.security_categories, ensure_ascii=False)
        evidence_json = json.dumps(result.evidence, ensure_ascii=False)
        now = self._now()
        self.connection.execute(
            """
            INSERT INTO classifications (
                notice_identity, relevant, confidence, industry, categories_json, evidence_json,
                reason, method, ai_confirmed, needs_review, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(notice_identity) DO UPDATE SET
                relevant=excluded.relevant, confidence=excluded.confidence, industry=excluded.industry,
                categories_json=excluded.categories_json, evidence_json=excluded.evidence_json,
                reason=excluded.reason, method=excluded.method, ai_confirmed=excluded.ai_confirmed,
                needs_review=excluded.needs_review, updated_at=excluded.updated_at
            """,
            (
                notice.identity, relevant, result.confidence, result.industry,
                categories_json, evidence_json, result.reason, result.method,
                int(result.ai_confirmed), int(result.needs_review), now,
            ),
        )
        for artifact in artifacts:
            self.connection.execute(
                """
                UPDATE artifact_versions SET
                    classification_relevant=?, classification_confidence=?,
                    classification_industry=?, classification_categories_json=?,
                    classification_evidence_json=?, classification_reason=?,
                    classification_method=?, classification_ai_confirmed=?,
                    classification_needs_review=?, classification_updated_at=?
                WHERE notice_identity=? AND source_url=? AND sha256=?
                """,
                (
                    relevant, result.confidence, result.industry,
                    categories_json, evidence_json, result.reason, result.method,
                    int(result.ai_confirmed), int(result.needs_review), now,
                    notice.identity, artifact.source_url, artifact.sha256,
                ),
            )
        self.connection.commit()

    def record_coverage(self, run_id: str, coverage: Coverage) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO coverage (
                run_id, source, status, target_start, target_end, first_date, last_date,
                reached_start, reached_end, truncated, pages, notices, details_ok,
                attachments_found, restricted_files, downloads_ok, downloads_failed,
                blocked, message, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, coverage.source, coverage.status, coverage.target_start,
                coverage.target_end, coverage.first_date, coverage.last_date,
                int(coverage.reached_start), int(coverage.reached_end),
                int(coverage.truncated), coverage.pages, coverage.notices,
                coverage.details_ok, coverage.attachments_found,
                coverage.restricted_files, coverage.downloads_ok,
                coverage.downloads_failed, coverage.blocked, coverage.message,
                self._now(),
            ),
        )
        self.connection.commit()

    def get_notice(self, identity: str) -> Notice | None:
        row = self.connection.execute(
            "SELECT * FROM notices WHERE identity=?", (identity,)
        ).fetchone()
        if row is None:
            return None
        try:
            metadata = json.loads(str(row["metadata_json"] or "{}"))
        except json.JSONDecodeError:
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        return Notice(
            source=str(row["source"]),
            authority_rank=int(row["authority_rank"]),
            external_id=str(row["external_id"]),
            title=str(row["title"]),
            published_at=str(row["published_at"] or ""),
            url=str(row["url"]),
            region=str(row["region"] or ""),
            notice_type=str(row["notice_type"] or ""),
            buyer=str(row["buyer"] or ""),
            metadata=metadata,
            discovery_url=str(row["discovery_url"] or ""),
            publication_url=str(row["publication_url"] or ""),
            origin_url=str(row["origin_url"] or ""),
            source_role=str(row["source_role"] or "unknown"),
            origin_verified=bool(row["origin_verified"]),
        )

    def attachment_refs_for_notice(self, identity: str) -> list[AttachmentRef]:
        rows = self.connection.execute(
            """
            SELECT source_url, original_name, label, access
            FROM artifact_refs WHERE notice_identity=? ORDER BY source_url
            """,
            (identity,),
        )
        return [
            AttachmentRef(
                url=str(row["source_url"]),
                original_name=str(row["original_name"] or "") or None,
                label=str(row["label"] or ""),
                access=str(row["access"] or "public_direct"),  # type: ignore[arg-type]
            )
            for row in rows if not attachment_url_error(str(row["source_url"]))
        ]

    @staticmethod
    def _metadata_text(metadata: dict[str, Any], *keys: str) -> str:
        for key in keys:
            value = metadata.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool):
                text = str(value).strip()
                if text:
                    return text
        return ""

    def list_notices(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        query: str = "",
        notice_type: str = "",
        industry: str = "",
        category: str = "",
        city: str = "",
        status: str = "",
        download_status: str = "",
        source: str = "",
        date_from: str = "",
        date_to: str = "",
        relevance: str = "all",
        query_id: str = "",
    ) -> dict[str, Any]:
        """Return a safe, filterable catalogue; no file bytes are read here."""

        if isinstance(page, bool) or page < 1:
            raise ValueError("page 必须是大于等于 1 的整数")
        if isinstance(page_size, bool) or not 1 <= page_size <= 200:
            raise ValueError("page_size 必须在 1 到 200 之间")
        if relevance not in {"all", "relevant", "review", "excluded"}:
            raise ValueError("relevance 必须是 all、relevant、review 或 excluded")
        if download_status not in {"", "downloaded", "not_downloaded", "partial", "failed"}:
            raise ValueError(
                "download_status 必须是 downloaded、not_downloaded、partial 或 failed"
            )

        base = """
            FROM notices n
            LEFT JOIN structured_fields sf ON sf.notice_identity=n.identity
            LEFT JOIN classifications c ON c.notice_identity=n.identity
            LEFT JOIN (
                SELECT notice_identity,
                       COUNT(*) AS ref_count,
                       SUM(CASE WHEN status IN ('failed','blocked','restricted','unverified_origin')
                                THEN 1 ELSE 0 END) AS ref_failed_count
                FROM artifact_refs WHERE status <> 'invalid_link' GROUP BY notice_identity
            ) refs ON refs.notice_identity=n.identity
            LEFT JOIN (
                SELECT notice_identity, COUNT(*) AS verified_file_count,
                       SUM(CASE WHEN source_url<>COALESCE(official_notice_url,'')
                                THEN 1 ELSE 0 END) AS downloaded_attachment_count,
                       GROUP_CONCAT(delivery_path, ' | ') AS delivery_paths
                FROM artifacts
                WHERE delivery_eligible=1 AND origin_verified=1
                  AND gate_status='allowed' AND source_role='official'
                  AND lower(source_url) LIKE 'https://%'
                  AND status IN ('delivered','review','verified','downloaded')
                  AND delivery_path IS NOT NULL
                GROUP BY notice_identity
            ) files ON files.notice_identity=n.identity
        """
        clauses: list[str] = []
        values: list[object] = []
        if query_id:
            clauses.append("json_extract(n.metadata_json,'$.query_id')=?")
            clauses.append("n.status<>'alias'")
            values.append(query_id)

        def like_clause(expression: str, value: str) -> None:
            cleaned = value.strip()
            if cleaned == "__unknown__":
                clauses.append(f"COALESCE({expression},'')=''")
                return
            if cleaned:
                clauses.append(f"{expression} LIKE ? ESCAPE '\\'")
                escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                values.append(f"%{escaped}%")

        cleaned_query = query.strip()[:200]
        if cleaned_query:
            escaped = cleaned_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{escaped}%"
            clauses.append(
                "(n.title LIKE ? ESCAPE '\\' OR n.buyer LIKE ? ESCAPE '\\' "
                "OR sf.winning_vendor LIKE ? ESCAPE '\\' OR sf.project_code LIKE ? ESCAPE '\\')"
            )
            values.extend((pattern, pattern, pattern, pattern))
        like_clause("COALESCE(sf.event_type,n.notice_type,'')", notice_type)
        like_clause("COALESCE(c.industry,'')", industry)
        like_clause("COALESCE(c.categories_json,'')", category)
        like_clause("COALESCE(sf.city,n.region,'')", city)
        if status.strip():
            clauses.append("n.status=?")
            values.append(status.strip())
        if source.strip():
            clauses.append("n.source=?")
            values.append(source.strip())
        if date_from.strip():
            clauses.append("substr(COALESCE(sf.published_at,n.published_at,''),1,10)>=?")
            values.append(date_from.strip())
        if date_to.strip():
            clauses.append("substr(COALESCE(sf.published_at,n.published_at,''),1,10)<=?")
            values.append(date_to.strip())
        if relevance == "relevant":
            clauses.append("c.relevant=1")
        elif relevance == "review":
            clauses.append(
                "((c.needs_review=1 OR c.relevant IS NULL) "
                "AND n.status NOT IN ('filtered','excluded','alias'))"
            )
        elif relevance == "excluded":
            clauses.append("(c.relevant=0 OR n.status IN ('filtered','excluded'))")
        if download_status == "downloaded":
            clauses.append("COALESCE(files.verified_file_count,0)>0")
        elif download_status == "not_downloaded":
            clauses.append("COALESCE(files.verified_file_count,0)=0")
        elif download_status == "partial":
            clauses.append(
                "COALESCE(files.verified_file_count,0)>0 AND "
                "COALESCE(refs.ref_count,0)>COALESCE(files.downloaded_attachment_count,0)"
            )
        elif download_status == "failed":
            clauses.append("COALESCE(refs.ref_failed_count,0)>0")

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        total_row = self.connection.execute(
            "SELECT COUNT(*) AS total " + base + where, values
        ).fetchone()
        total = int(total_row["total"] if total_row else 0)
        select = """
            SELECT n.identity, n.source, n.external_id, n.title,
                   COALESCE(sf.event_type,n.notice_type,'') AS notice_type,
                   COALESCE(sf.published_at,n.published_at,'') AS published_at,
                   n.buyer, n.region, n.metadata_json,
                   sf.city, sf.buyer_contact, sf.buyer_phone,
                   sf.winning_vendor, sf.winning_vendor_contact,
                   sf.winning_vendor_phone, sf.project_summary,
                   sf.project_code, sf.package_no,
                   sf.intention_amount_minor, sf.budget_amount_minor,
                   sf.max_price_minor, sf.award_amount_minor,
                   sf.contract_amount_minor, sf.bid_deadline, sf.opening_at,
                   sf.award_at, sf.expected_purchase_date,
                   c.relevant, c.confidence, c.industry, c.categories_json,
                   c.reason, c.method, c.ai_confirmed, c.needs_review,
                   n.discovery_url, n.publication_url, n.origin_url,
                   n.origin_verified, n.status,
                   COALESCE(refs.ref_count,0) AS attachment_count,
                   COALESCE(refs.ref_failed_count,0) AS failed_file_count,
                   COALESCE(files.verified_file_count,0) AS downloaded_file_count,
                   COALESCE(files.downloaded_attachment_count,0) AS downloaded_attachment_count,
                   files.delivery_paths
        """
        rows = self.connection.execute(
            select + base + where
            + " ORDER BY COALESCE(sf.published_at,n.published_at) DESC, n.source, n.external_id"
            + " LIMIT ? OFFSET ?",
            (*values, page_size, (page - 1) * page_size),
        )
        items: list[dict[str, Any]] = []
        for row in rows:
            try:
                metadata = json.loads(str(row["metadata_json"] or "{}"))
            except json.JSONDecodeError:
                metadata = {}
            if not isinstance(metadata, dict):
                metadata = {}
            try:
                categories = json.loads(str(row["categories_json"] or "[]"))
            except json.JSONDecodeError:
                categories = []
            if not isinstance(categories, list):
                categories = []
            contact = str(row["buyer_contact"] or "") or self._metadata_text(
                metadata, "buyer_contact", "purchaser_contact", "customer_contact", "contact"
            )
            phone = str(row["buyer_phone"] or "") or self._metadata_text(
                metadata, "buyer_phone", "purchaser_phone", "customer_phone", "contact_phone", "phone"
            )
            vendor_contact = str(row["winning_vendor_contact"] or "") or self._metadata_text(
                metadata, "winning_vendor_contact", "supplier_contact", "vendor_contact"
            )
            vendor_phone = str(row["winning_vendor_phone"] or "") or self._metadata_text(
                metadata, "winning_vendor_phone", "supplier_phone", "vendor_phone"
            )
            city_value = str(row["city"] or "") or self._metadata_text(
                metadata, "city", "area", "district"
            ) or str(row["region"] or "")
            industry_value = str(row["industry"] or "") or self._metadata_text(
                metadata, "industry", "industry_name"
            )
            downloaded = int(row["downloaded_file_count"] or 0)
            downloaded_attachments = int(row["downloaded_attachment_count"] or 0)
            attachments = int(row["attachment_count"] or 0)
            failed = int(row["failed_file_count"] or 0)
            if failed:
                file_state = "failed" if not downloaded else "partial"
            elif downloaded:
                file_state = (
                    "partial" if downloaded_attachments < attachments else "downloaded"
                )
            else:
                file_state = "not_downloaded"
            if row["needs_review"]:
                ai_state = "review"
            elif row["relevant"] is True or row["relevant"] == 1:
                ai_state = "confirmed" if row["ai_confirmed"] else "review"
            elif row["relevant"] is False or row["relevant"] == 0:
                ai_state = "excluded"
            else:
                ai_state = "pending"
            amount_fields = {
                name: (int(row[name]) if row[name] is not None else None)
                for name in (
                    "intention_amount_minor", "budget_amount_minor", "max_price_minor",
                    "award_amount_minor", "contract_amount_minor",
                )
            }
            primary_field = next(
                (name for name in (
                    "contract_amount_minor", "award_amount_minor", "budget_amount_minor",
                    "max_price_minor", "intention_amount_minor",
                ) if amount_fields[name] is not None),
                "",
            )
            primary_amount = amount_fields.get(primary_field) if primary_field else None
            location = infer_location(str(row["region"] or ""), city_value,
                                      str(row["buyer"] or ""), str(row["title"]),
                                      str(metadata.get("district") or ""))
            items.append({
                "notice_id": str(row["identity"]),
                "identity": str(row["identity"]),
                "query_review": (metadata.get("query_review", {})
                                 if isinstance(metadata.get("query_review"), dict) else {}),
                "source": str(row["source"]),
                "external_id": str(row["external_id"]),
                "published_at": str(row["published_at"] or ""),
                "title": str(row["title"]),
                "notice_type": str(row["notice_type"] or ""),
                "buyer": str(row["buyer"] or ""),
                "industry": industry_value,
                "city": city_value,
                "location": location,
                "buyer_contact": contact,
                "buyer_phone": phone,
                "winning_vendor": str(row["winning_vendor"] or ""),
                "winning_vendor_contact": vendor_contact,
                "winning_vendor_phone": vendor_phone,
                "vendor_contact": vendor_contact,
                "vendor_phone": vendor_phone,
                "project_tags": [str(value) for value in categories if str(value).strip()],
                "project_summary": str(row["project_summary"] or "") or self._metadata_text(
                    metadata, "project_summary", "summary", "description", "project_info"
                ),
                "project_code": clean_project_code(str(row["project_code"] or "")),
                "package_no": str(row["package_no"] or ""),
                **amount_fields,
                "amount_minor": primary_amount,
                "amount_type": {
                    "contract_amount_minor": "contract",
                    "award_amount_minor": "award",
                    "budget_amount_minor": "budget",
                    "max_price_minor": "max_price",
                    "intention_amount_minor": "intention",
                }.get(primary_field, ""),
                "bid_deadline": str(row["bid_deadline"] or ""),
                "opening_at": str(row["opening_at"] or ""),
                "award_at": str(row["award_at"] or ""),
                "expected_purchase_date": str(row["expected_purchase_date"] or ""),
                "original_notice_url": str(row["publication_url"] or row["origin_url"] or ""),
                "discovery_url": str(row["discovery_url"] or ""),
                "origin_verified": bool(row["origin_verified"]),
                "status": str(row["status"] or ""),
                "relevant": None if row["relevant"] is None else bool(row["relevant"]),
                "confidence": float(row["confidence"] or 0),
                "ai_status": ai_state,
                "ai_method": str(row["method"] or ""),
                "ai_reason": str(row["reason"] or ""),
                "attachment_count": attachments,
                "downloaded_file_count": downloaded,
                "downloaded_attachment_count": downloaded_attachments,
                "download_status": file_state,
                "delivery_paths": str(row["delivery_paths"] or ""),
            })
        return {
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "pages": (total + page_size - 1) // page_size,
        }

    def artifact_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT n.source, n.external_id, n.title, n.published_at, n.url AS notice_url,
                   n.buyer, n.source_role AS notice_source_role,
                   n.discovery_url, n.publication_url, n.origin_url,
                   n.origin_verified AS notice_origin_verified,
                   a.source_url, a.final_url, a.original_name, a.original_name_raw,
                   a.sha256, a.size, a.vault_path, a.content_type, a.document_kind,
                   a.source_role, a.origin_verified, a.gate_status, a.gate_code,
                   a.delivery_eligible, a.official_notice_url,
                   a.delivery_path, a.status, a.error,
                   a.classification_relevant AS relevant,
                   a.classification_confidence AS confidence,
                   a.classification_industry AS industry,
                   a.classification_categories_json AS categories_json,
                   a.classification_evidence_json AS evidence_json,
                   a.classification_reason AS reason,
                   a.classification_method AS method,
                   a.classification_ai_confirmed AS ai_confirmed,
                   a.classification_needs_review AS needs_review
            FROM artifact_versions a
            JOIN notices n ON n.identity=a.notice_identity
            ORDER BY n.published_at DESC, n.source, n.external_id
            """
        ))

    def verified_artifact_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT n.source, n.external_id, n.title, n.published_at,
                   n.buyer, n.url AS notice_url, n.discovery_url,
                   n.publication_url, n.origin_url,
                   n.source_role AS notice_source_role,
                   n.origin_verified AS notice_origin_verified,
                   a.source_url, a.final_url, a.original_name,
                   a.original_name_raw, a.sha256, a.size, a.vault_path,
                   a.content_type, a.document_kind, a.source_role,
                   a.origin_verified, a.gate_status, a.gate_code,
                   a.delivery_eligible, a.official_notice_url,
                   a.delivery_path, a.status, a.error,
                   a.classification_relevant AS relevant,
                   a.classification_confidence AS confidence,
                   a.classification_industry AS industry,
                   a.classification_categories_json AS categories_json,
                   a.classification_evidence_json AS evidence_json,
                   a.classification_reason AS reason,
                   a.classification_method AS method,
                   a.classification_ai_confirmed AS ai_confirmed,
                   a.classification_needs_review AS needs_review
            FROM artifact_versions a
            JOIN artifacts current
              ON current.notice_identity=a.notice_identity
             AND current.source_url=a.source_url
             AND current.sha256=a.sha256
            JOIN notices n ON n.identity=a.notice_identity
            WHERE current.delivery_eligible=1
              AND current.origin_verified=1
              AND current.gate_status='allowed'
              AND current.source_role='official'
              AND lower(current.source_url) LIKE 'https://%'
              AND lower(COALESCE(current.final_url, '')) LIKE 'https://%'
              AND lower(COALESCE(current.official_notice_url, '')) LIKE 'https://%'
              AND a.delivery_eligible=1
              AND a.origin_verified=1
              AND a.gate_status='allowed'
              AND a.source_role='official'
              AND n.origin_verified=1
            ORDER BY n.published_at DESC, n.source, n.external_id
            """
        ))

    def current_result_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT n.source, n.external_id, n.title,
                   COALESCE(sf.event_type, n.notice_type) AS event_type,
                   COALESCE(sf.published_at, n.published_at) AS published_at,
                   n.buyer, n.region, sf.project_code, sf.package_no,
                   sf.intention_amount_minor, sf.budget_amount_minor,
                   sf.max_price_minor, sf.award_amount_minor,
                   sf.contract_amount_minor, sf.winning_vendor,
                   sf.city, sf.buyer_contact, sf.buyer_phone,
                   sf.winning_vendor_contact, sf.winning_vendor_phone,
                   sf.project_summary,
                   sf.bid_deadline, sf.opening_at, sf.award_at,
                   sf.expected_purchase_date, sf.evidence_json AS field_evidence_json,
                   c.relevant, c.confidence, c.industry,
                   c.categories_json, c.evidence_json, c.reason,
                   c.method, c.ai_confirmed, c.needs_review,
                   n.discovery_url, n.publication_url, n.origin_url,
                   n.origin_verified, n.status,
                   COALESCE(v.verified_file_count, 0) AS verified_file_count,
                   v.delivery_paths, v.official_source_urls
            FROM notices n
            LEFT JOIN structured_fields sf ON sf.notice_identity=n.identity
            LEFT JOIN classifications c ON c.notice_identity=n.identity
            LEFT JOIN (
                SELECT notice_identity,
                       COUNT(*) AS verified_file_count,
                       GROUP_CONCAT(delivery_path, ' | ') AS delivery_paths,
                       GROUP_CONCAT(source_url, ' | ') AS official_source_urls
                FROM artifacts
                WHERE delivery_eligible=1
                  AND origin_verified=1
                  AND gate_status='allowed'
                  AND source_role='official'
                  AND lower(source_url) LIKE 'https://%'
                  AND lower(COALESCE(final_url, '')) LIKE 'https://%'
                  AND lower(COALESCE(official_notice_url, '')) LIKE 'https://%'
                  AND status IN ('delivered', 'review', 'verified', 'downloaded')
                GROUP BY notice_identity
            ) v ON v.notice_identity=n.identity
            ORDER BY COALESCE(sf.published_at, n.published_at) DESC,
                     n.source, n.external_id
            """
        ))

    def provenance_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT n.source, n.external_id, n.title, p.discovery_url,
                   p.official_url, p.relation, p.method, p.verified,
                   p.evidence, p.updated_at
            FROM provenance_edges p
            JOIN notices n ON n.identity=p.lead_identity
            ORDER BY p.updated_at DESC, n.source, n.external_id
            """
        ))

    def coverage_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            "SELECT * FROM coverage ORDER BY updated_at DESC, source"
        ))

    def artifact_ref_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT n.source, n.external_id, n.title, n.published_at,
                   n.url AS notice_url, r.source_url, r.original_name,
                   r.label, r.access, r.status, r.error, r.updated_at
            FROM artifact_refs r
            JOIN notices n ON n.identity=r.notice_identity
            ORDER BY n.published_at DESC, n.source, n.external_id, r.source_url
            """
        ))

    def download_link_rows(self, notice_ids: list[str] | None = None) -> list[sqlite3.Row]:
        """One row per discovered attachment, plus an entry for notices without one.

        This query never requests a website or reads a downloaded file.
        """
        rows = self.connection.execute(
            """
            SELECT n.identity, n.source, n.title, n.published_at, n.buyer,
                   n.url AS discovered_url, n.discovery_url,
                   COALESCE(NULLIF(n.publication_url,''), NULLIF(n.origin_url,''), '') AS official_url,
                   n.source_role, n.origin_verified, n.status AS notice_status,
                   n.metadata_json,
                   r.source_url AS attachment_url, r.original_name, r.label,
                   r.access, r.status AS reference_status, r.error,
                   a.delivery_path, a.sha256,
                   a.status AS artifact_status, a.gate_status, a.delivery_eligible
            FROM notices n
            LEFT JOIN artifact_refs r ON r.notice_identity=n.identity
            LEFT JOIN artifacts a ON a.notice_identity=n.identity
                AND a.source_url=r.source_url
            ORDER BY n.published_at DESC, n.source, n.external_id, r.source_url
            """
        )
        selected = None if notice_ids is None else set(notice_ids)
        return [row for row in rows if selected is None or row["identity"] in selected]

    def notice_alias_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT a.alias_identity, n.source AS alias_source, n.url AS alias_url,
                   a.canonical_identity, c.source AS canonical_source,
                   c.url AS canonical_url, a.reason, a.updated_at
            FROM notice_aliases a
            JOIN notices n ON n.identity=a.alias_identity
            JOIN notices c ON c.identity=a.canonical_identity
            ORDER BY a.updated_at DESC, a.alias_identity
            """
        ))

    def latest_coverage_rows(self) -> list[sqlite3.Row]:
        return list(self.connection.execute(
            """
            SELECT c.* FROM coverage c
            JOIN (
                SELECT source, MAX(updated_at) AS max_updated
                FROM coverage GROUP BY source
            ) latest ON latest.source=c.source AND latest.max_updated=c.updated_at
            ORDER BY c.source
            """
        ))


def _synchronized(method):
    @wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


for _name, _member in list(vars(Database).items()):
    # Plain instance methods only: staticmethods keep their descriptor type
    # and are already stateless, and private helpers run under a caller's lock.
    if (callable(_member) and not _name.startswith("_")
            and getattr(_member, "__self__", None) is None):
        setattr(Database, _name, _synchronized(_member))
