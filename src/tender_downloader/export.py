from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

from .database import Database
from .htmlparse import attachment_url_error
from .utils import sha256_file


def _atomic_export(function):
    """Publish a complete CSV when collection and manual downloads overlap."""
    @wraps(function)
    def wrapped(db: Database, output_dir: Path) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".export-", dir=output_dir) as temporary:
            staged = function(db, Path(temporary))
            target = output_dir / staged.name
            os.replace(staged, target)
        return target
    return wrapped


MANIFEST_FIELDS = (
    "source", "external_id", "title", "published_at", "buyer", "notice_url",
    "notice_source_role", "discovery_url", "publication_url", "origin_url",
    "notice_origin_verified",
    "source_url", "final_url", "original_name", "original_name_raw", "sha256", "size", "vault_path", "delivery_path",
    "content_type", "document_kind", "source_role", "origin_verified",
    "gate_status", "gate_code", "delivery_eligible", "official_notice_url",
    "status", "error", "relevant", "confidence", "industry", "categories_json",
    "evidence_json", "reason", "method", "ai_confirmed", "needs_review",
)

AMOUNT_FIELDS = (
    "intention_amount_minor",
    "budget_amount_minor",
    "max_price_minor",
    "award_amount_minor",
    "contract_amount_minor",
)

DOWNLOAD_LINK_FIELDS = (
    "公告时间", "项目名称", "采购人", "来源", "文件名称", "链接类型",
    "下载URL", "公告URL", "发现URL", "来源验证", "访问条件", "下载状态",
    "失败或待办原因", "已下载路径", "SHA256", "标讯ID",
)


def download_links_csv(db: Database, notice_ids: list[str] | None = None) -> bytes:
    """Export observed links without claiming they are usable or downloading them."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=DOWNLOAD_LINK_FIELDS)
    writer.writeheader()
    for record in db.download_link_rows(notice_ids):
        item = dict(record)
        attachment = item.get("attachment_url") or ""
        invalid_link = attachment_url_error(attachment) if attachment else ""
        if invalid_link:
            attachment = ""
        official = item.get("official_url") or ""
        verified = bool(item.get("origin_verified")) and item.get("source_role") == "official"
        downloaded = not invalid_link and (
            bool(item.get("delivery_path")) and item.get("gate_status") == "allowed"
            and bool(item.get("delivery_eligible"))
        )
        access = item.get("access") or ""
        reason = invalid_link or item.get("error") or ""
        try:
            metadata = json.loads(item.get("metadata_json") or "{}")
        except (ValueError, TypeError):
            metadata = {}
        if downloaded:
            reason = ""
        if not downloaded and not reason and isinstance(metadata, dict):
            reason = metadata.get("download_error") or metadata.get("collection_error") or ""
        if not reason and not attachment:
            reason = "未发现附件直链；请打开公告查看文件获取方式"
        elif not reason and not verified:
            reason = "尚未验证官方来源；链接仅供人工核对"
        elif not reason and not downloaded:
            reason = "链接已发现，尚未验证下载响应；可能需要登录或报名"
        state = "已下载" if downloaded else {
            "failed": "下载失败", "blocked": "访问受阻", "restricted": "需要人工处理",
            "unverified_origin": "来源待验证",
        }.get(str(item.get("reference_status") or ""), "未下载" if attachment else "无附件直链")
        row = {
            "公告时间": item["published_at"] or "", "项目名称": item["title"],
            "采购人": item["buyer"] or "", "来源": item["source"],
            "文件名称": item.get("original_name") or item.get("label") or "",
            "链接类型": "附件候选" if attachment else "公告入口",
            "下载URL": attachment,
            "公告URL": official or item["discovered_url"],
            "发现URL": item.get("discovery_url") or item["discovered_url"],
            "来源验证": "公告来源已验证" if verified else "未验证",
            "访问条件": {"public_direct": "公开链接（下载时验证）", "login_required": "需要登录",
                         "registration_required": "需要报名", "restricted": "访问受限"}.get(access, access),
            "下载状态": state, "失败或待办原因": reason,
            "已下载路径": item.get("delivery_path") if downloaded else "",
            "SHA256": item.get("sha256") if downloaded else "", "标讯ID": item["identity"],
        }
        writer.writerow(_csv_safe_row(row))
    return b"\xef\xbb\xbf" + stream.getvalue().encode("utf-8")


@_atomic_export
def export_download_links(db: Database, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "download_links.csv"
    path.write_bytes(download_links_csv(db))
    return path


def _csv_safe_row(row) -> dict:
    result: dict = {}
    for key, value in dict(row).items():
        if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
            value = "'" + value
        result[key] = value
    return result


@_atomic_export
def export_manifest(db: Database, output_dir: Path) -> Path:
    path = output_dir / "manifest.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for row in db.artifact_rows():
            writer.writerow(_csv_safe_row(row))
    return path


@_atomic_export
def export_verified_manifest(db: Database, output_dir: Path) -> Path:
    """只导出通过官方来源与内容门禁的当前版本。"""
    path = output_dir / "verified_manifest.csv"
    rows = db.verified_artifact_rows()
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=MANIFEST_FIELDS, extrasaction="ignore"
        )
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row))
    return path


def _with_amount_yuan(row) -> dict:
    result = _csv_safe_row(row)
    for field in AMOUNT_FIELDS:
        value = result.get(field)
        yuan_field = field.replace("_minor", "_yuan")
        if value is None:
            result[yuan_field] = ""
        else:
            minor = int(value)
            sign = "-" if minor < 0 else ""
            yuan, cents = divmod(abs(minor), 100)
            result[yuan_field] = f"{sign}{yuan}.{cents:02d}"
    return result


@_atomic_export
def export_current_results(db: Database, output_dir: Path) -> Path:
    """导出当前公告级结果；金额同时保留分和元，避免浮点歧义。"""
    path = output_dir / "current_results.csv"
    rows = db.current_result_rows()
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields = list(rows[0].keys())
    for field in AMOUNT_FIELDS:
        fields.insert(fields.index(field) + 1, field.replace("_minor", "_yuan"))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_with_amount_yuan(row))
    return path


@_atomic_export
def export_provenance(db: Database, output_dir: Path) -> Path:
    path = output_dir / "provenance.csv"
    rows = db.provenance_rows()
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields = tuple(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row))
    return path


@_atomic_export
def export_coverage(db: Database, output_dir: Path) -> Path:
    path = output_dir / "coverage.csv"
    rows = db.coverage_rows()
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields = tuple(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row))
    return path


@_atomic_export
def export_artifact_refs(db: Database, output_dir: Path) -> Path:
    path = output_dir / "artifact_refs.csv"
    rows = db.artifact_ref_rows()
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields = tuple(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row))
    return path


@_atomic_export
def export_notice_aliases(db: Database, output_dir: Path) -> Path:
    path = output_dir / "source_aliases.csv"
    rows = db.notice_alias_rows()
    if not rows:
        path.write_text("", encoding="utf-8-sig")
        return path
    fields = tuple(rows[0].keys())
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(_csv_safe_row(row))
    return path


def verify_files(db: Database) -> tuple[int, list[str]]:
    """Verify immutable bytes and the non-AI delivery/provenance invariants."""

    checked = 0
    errors: list[str] = []
    rows = db.artifact_rows()
    verified_rows = db.verified_artifact_rows()
    if not verified_rows:
        errors.append("没有当前有效的 HTTPS 官方原件")

    def is_https(value: object) -> bool:
        try:
            parsed = urlsplit(str(value or ""))
            return (
                parsed.scheme.lower() == "https"
                and bool(parsed.hostname)
                and parsed.username is None
                and parsed.password is None
                and parsed.port in {None, 443}
            )
        except ValueError:
            return False

    for row in verified_rows:
        identity = f"{row['source']}:{row['external_id']}"
        if row["source_role"] != "official":
            errors.append(f"当前验证文件来源角色不是 official: {identity}")
        if not row["notice_origin_verified"]:
            errors.append(f"当前公告没有官方来源验证证明: {identity}")
        for field, label in (
            ("source_url", "原件 URL"),
            ("final_url", "最终 URL"),
            ("official_notice_url", "官方公告 URL"),
        ):
            if not is_https(row[field]):
                errors.append(f"当前验证文件的{label}不是安全 HTTPS: {identity}")

    for row in rows:
        expected = str(row["sha256"])
        vault = Path(str(row["vault_path"]))
        if not vault.exists():
            errors.append(f"原件不存在: {vault}")
            continue
        if sha256_file(vault) != expected:
            errors.append(f"原件哈希错误: {vault}")
        checked += 1
        delivery_value = row["delivery_path"]
        if delivery_value:
            identity = f"{row['source']}:{row['external_id']}:{expected[:12]}"
            eligible = (
                bool(row["delivery_eligible"])
                and bool(row["origin_verified"])
                and row["gate_status"] == "allowed"
                and row["source_role"] == "official"
            )
            if not eligible:
                errors.append(f"未通过官方来源门禁的历史文件仍有交付路径: {identity}")
            for field, label in (
                ("source_url", "原件 URL"),
                ("final_url", "最终 URL"),
                ("official_notice_url", "官方公告 URL"),
            ):
                if not is_https(row[field]):
                    errors.append(f"交付文件的{label}不是安全 HTTPS: {identity}")
            delivery = Path(str(delivery_value))
            if not delivery.exists():
                errors.append(f"交付文件不存在: {delivery}")
            elif sha256_file(delivery) != expected:
                errors.append(f"交付文件哈希错误: {delivery}")
            else:
                checked += 1
    return checked, errors
