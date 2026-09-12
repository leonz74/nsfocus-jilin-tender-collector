from __future__ import annotations

import os
import shutil
import stat
from dataclasses import replace
from pathlib import Path

from .http_client import HttpClient
from .models import Classification, Notice, StoredArtifact
from .utils import safe_component, sha256_bytes, sha256_file, stable_id


def _readonly(path: Path) -> None:
    try:
        path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass


class ImmutableStore:
    def __init__(self, output_dir: Path) -> None:
        self.output_dir = output_dir
        self.raw_dir = output_dir / "raw"
        self.evidence_dir = output_dir / "lead_evidence"
        self.snapshot_dir = output_dir / "render_snapshots"
        self.quarantine_dir = output_dir / "quarantine"
        self.staging_dir = output_dir / ".staging"
        self.delivery_dir = output_dir / "delivery_verified"
        self.requested_dir = output_dir / "requested_downloads"
        self.review_dir = output_dir / "review"
        for path in (
            self.raw_dir,
            self.evidence_dir,
            self.snapshot_dir,
            self.quarantine_dir,
            self.staging_dir,
            self.delivery_dir,
            self.requested_dir,
            self.review_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _vault_path(self, digest: str, original_name: str) -> Path:
        return self.raw_dir / digest[:2] / digest / safe_component(original_name, max_length=80)

    def _evidence_path(self, digest: str, original_name: str) -> Path:
        return (
            self.evidence_dir
            / digest[:2]
            / digest
            / safe_component(original_name, max_length=80)
        )

    def _snapshot_path(self, digest: str, original_name: str) -> Path:
        return (
            self.snapshot_dir
            / digest[:2]
            / digest
            / safe_component(original_name, max_length=80)
        )

    def _quarantine_path(self, digest: str, original_name: str) -> Path:
        return (
            self.quarantine_dir
            / digest[:2]
            / digest
            / safe_component(original_name, max_length=80)
        )

    @staticmethod
    def _write_immutable(destination: Path, data: bytes, digest: str) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.with_suffix(destination.suffix + ".part")
            with temporary.open("wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            _readonly(destination)
        if sha256_file(destination) != digest:
            raise RuntimeError(f"不可变文件哈希校验失败: {destination}")

    def ingest_bytes(
        self,
        data: bytes,
        *,
        original_name: str,
        source_url: str,
        content_type: str = "",
    ) -> StoredArtifact:
        digest = sha256_bytes(data)
        destination = self._vault_path(digest, original_name)
        self._write_immutable(destination, data, digest)
        return StoredArtifact(
            sha256=digest,
            size=len(data),
            original_name=destination.name,
            vault_path=destination,
            source_url=source_url,
            content_type=content_type,
            original_name_raw=original_name,
            final_url=source_url,
        )

    def ingest_evidence_bytes(
        self,
        data: bytes,
        *,
        original_name: str,
        source_url: str,
        content_type: str = "",
    ) -> StoredArtifact:
        """Preserve a discovery/lead response outside the verified raw vault."""

        digest = sha256_bytes(data)
        destination = self._evidence_path(digest, original_name)
        self._write_immutable(destination, data, digest)
        return StoredArtifact(
            sha256=digest,
            size=len(data),
            original_name=destination.name,
            vault_path=destination,
            source_url=source_url,
            content_type=content_type,
            original_name_raw=original_name,
            final_url=source_url,
            document_kind="discovery_lead",
            source_role="commercial_aggregator",
            gate_status="not_delivery_eligible",
            gate_code="NON_OFFICIAL_SOURCE",
            delivery_eligible=False,
            discovery_url=source_url,
        )

    def ingest_render_snapshot(
        self,
        data: bytes,
        *,
        original_name: str,
        source_url: str,
        content_type: str = "text/html",
    ) -> StoredArtifact:
        """Preserve browser-rendered analysis bytes, never as an original."""

        digest = sha256_bytes(data)
        destination = self._snapshot_path(digest, original_name)
        self._write_immutable(destination, data, digest)
        return StoredArtifact(
            sha256=digest,
            size=len(data),
            original_name=destination.name,
            vault_path=destination,
            source_url=source_url,
            content_type=content_type,
            original_name_raw=original_name,
            final_url=source_url,
            document_kind="rendered_snapshot",
            source_role="analysis_only",
            gate_status="analysis_only",
            gate_code="RENDERED_SNAPSHOT",
            delivery_eligible=False,
            discovery_url=source_url,
        )

    def download(
        self,
        client: HttpClient,
        *,
        url: str,
        suggested_name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> StoredArtifact:
        part = self.staging_dir / f"{stable_id(url, 32)}.part"
        result = client.download(url, part, suggested_name=suggested_name, headers=headers)
        destination = self._vault_path(result.sha256, result.original_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != result.sha256:
                raise RuntimeError(f"同路径原件哈希冲突: {destination}")
            part.unlink(missing_ok=True)
        else:
            os.replace(part, destination)
            _readonly(destination)
        if sha256_file(destination) != result.sha256:
            raise RuntimeError(f"下载文件进入原件库后哈希改变: {destination}")
        return StoredArtifact(
            sha256=result.sha256,
            size=result.size,
            original_name=result.original_name,
            vault_path=destination,
            source_url=url,
            content_type=result.content_type,
            original_name_raw=result.original_name_raw,
            final_url=result.final_url,
        )

    def download_candidate(
        self,
        client: HttpClient,
        *,
        url: str,
        suggested_name: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> StoredArtifact:
        """先隔离下载；通过门禁后再原字节提升到官方原件库。"""

        part = self.staging_dir / f"{stable_id(url, 32)}.candidate.part"
        result = client.download(
            url, part, suggested_name=suggested_name, headers=headers
        )
        destination = self._quarantine_path(result.sha256, result.original_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if sha256_file(destination) != result.sha256:
                raise RuntimeError(f"隔离区同路径哈希冲突: {destination}")
            part.unlink(missing_ok=True)
        else:
            os.replace(part, destination)
            _readonly(destination)
        if sha256_file(destination) != result.sha256:
            raise RuntimeError(f"候选文件进入隔离区后哈希改变: {destination}")
        return StoredArtifact(
            sha256=result.sha256,
            size=result.size,
            original_name=result.original_name,
            vault_path=destination,
            source_url=url,
            content_type=result.content_type,
            original_name_raw=result.original_name_raw,
            final_url=result.final_url,
            document_kind="official_attachment_candidate",
            source_role="official",
            origin_verified=False,
            gate_status="pending",
            delivery_eligible=False,
        )

    def promote_candidate(self, artifact: StoredArtifact) -> StoredArtifact:
        """把已验证候选按原字节复制进不可变原件库。"""

        if sha256_file(artifact.vault_path) != artifact.sha256:
            raise RuntimeError(f"隔离候选哈希错误: {artifact.vault_path}")
        destination = self._vault_path(artifact.sha256, artifact.original_name)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(artifact.vault_path, destination)
            _readonly(destination)
        if sha256_file(destination) != artifact.sha256:
            raise RuntimeError(f"候选提升后哈希改变: {destination}")
        return replace(artifact, vault_path=destination)

    def deliver(
        self,
        notice: Notice,
        artifact: StoredArtifact,
        classification: Classification,
        *,
        review: bool = False,
    ) -> Path:
        if not artifact.delivery_eligible:
            raise RuntimeError(
                f"文件未通过官方来源和内容门禁，禁止复制到交付或复核目录: {artifact.original_name}"
            )
        root = self.review_dir if review else self.delivery_dir
        industry = safe_component(classification.industry or "其他行业")
        category = safe_component(
            classification.security_categories[0] if classification.security_categories else "其他网络安全"
        )
        project_label = safe_component(notice.external_id, max_length=24)
        project = f"{project_label}_{stable_id(notice.identity, 12)}"
        directory = root / industry / category / project
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / artifact.original_name
        if destination.exists() and sha256_file(destination) != artifact.sha256:
            directory = directory / artifact.sha256[:12]
            directory.mkdir(parents=True, exist_ok=True)
            destination = directory / artifact.original_name
        if not destination.exists():
            shutil.copyfile(artifact.vault_path, destination)
            _readonly(destination)
        if sha256_file(destination) != artifact.sha256:
            raise RuntimeError(f"交付副本哈希与原件不一致: {destination}")
        return destination

    def deliver_requested(self, notice: Notice, artifact: StoredArtifact) -> Path:
        """Copy a user-requested verified original without changing one byte."""

        if (
            not artifact.delivery_eligible
            or not artifact.origin_verified
            or artifact.gate_status != "allowed"
            or artifact.source_role != "official"
        ):
            raise RuntimeError(
                f"文件未通过官方来源和内容门禁，禁止按需交付: {artifact.original_name}"
            )
        if sha256_file(artifact.vault_path) != artifact.sha256:
            raise RuntimeError(f"按需交付前原件哈希错误: {artifact.vault_path}")
        project = safe_component(
            f"{notice.external_id}_{stable_id(notice.identity, 12)}",
            max_length=64,
        )
        directory = self.requested_dir / project
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / artifact.original_name
        if destination.exists() and sha256_file(destination) != artifact.sha256:
            destination = directory / artifact.sha256[:12] / artifact.original_name
            destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            shutil.copyfile(artifact.vault_path, destination)
            _readonly(destination)
        if sha256_file(destination) != artifact.sha256:
            raise RuntimeError(f"按需交付副本哈希与原件不一致: {destination}")
        return destination
