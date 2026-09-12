from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from .artifact_check import inspect_artifact
from .classify import (
    Classifier,
    RuleClassifier,
    force_review,
    has_strong_evidence,
    is_candidate,
)
from .config import AppConfig
from .content_gate import ContentGateInput, ContentGatePolicy, evaluate_content
from .database import Database
from .export import (
    export_artifact_refs,
    export_coverage,
    export_current_results,
    export_download_links,
    export_manifest,
    export_notice_aliases,
    export_provenance,
    export_verified_manifest,
)
from .extract import extract_text
from .field_extract import enrich_purchaser, extract_structured_fields, scope_text_for_notice
from .htmlparse import parse_html_document
from .http_client import ArtifactRestricted, FetchError, HttpClient, SourceBlocked
from .models import AttachmentRef, Classification, Coverage, Notice, RawDocument, StoredArtifact
from .official_registry import (
    DEFAULT_SOURCE_REGISTRY,
    SourceRegistration,
    SourceRegistry,
    SourceRole,
    UrlPolicyViolation,
    trusted_configured_official_host,
)
from .provenance import (
    OfficialCandidate,
    OfficialCandidateKind,
    extract_matching_official_detail_candidate,
    extract_official_candidates,
)
from .sources.base import SourceAdapter
from .storage import ImmutableStore
from .query_review import QueryReviewer
from .sources.recall import RecallSource
from .utils import canonical_url_key, safe_component, sha256_bytes


LOGGER = logging.getLogger(__name__)


_ACCESS_GATE_CODES = frozenset({"captcha_page", "waf_block_page", "login_page"})


@dataclass(frozen=True, slots=True)
class _VerifiedOfficial:
    requested_url: str
    detail: RawDocument
    text: str
    attachments: tuple
    artifact: StoredArtifact | None
    client: HttpClient
    unchanged: bool


class Pipeline:
    def __init__(
        self,
        *,
        config: AppConfig,
        client: HttpClient,
        db: Database,
        store: ImmutableStore,
        classifier: Classifier,
        sources: list[SourceAdapter],
        query_reviewer: QueryReviewer | None = None,
    ) -> None:
        self.config = config
        self.client = client
        self.db = db
        self.store = store
        self.classifier = classifier
        self.sources = sources
        criteria = config.data.get("_query", {}).get("criteria", {})
        if criteria.get("mode") == "ai_recall":
            self.sources = [
                RecallSource(source, criteria["max_candidates"])
                if source.config.get("_baseline_terms") is not None and not source.config.get("_query_unsupported")
                else source for source in sources
            ]
        self.query_reviewer = query_reviewer
        self.download_source_errors: dict[str, str] = {}
        ai = config.data.get("ai", {})
        self.accept_threshold = float(ai.get("auto_accept_threshold", 0.85))
        self.review_threshold = float(ai.get("second_review_threshold", 0.60))
        self.reclassify = bool(ai.get("reclassify", False))
        delivery = config.data.get("delivery", {})
        self.delivery_mode = str(delivery.get("mode", "on_demand"))
        self.copy_uncertain = bool(delivery.get("copy_uncertain_to_review", True))
        self.include_html_when_no_attachment = bool(
            delivery.get("include_notice_html_when_no_attachment", True)
        )
        self.include_official_notice_with_attachments = bool(
            delivery.get("include_official_notice_with_attachments", True)
        )
        recall = config.data.get("recall", {})
        self.recall_mode = str(recall.get("mode", "p0_complete"))
        provenance = config.data.get("provenance", {})
        self.registry = self._build_source_registry(config.data.get("sources", []))
        self.max_gate_bytes = int(
            float(provenance.get("max_gate_file_mb", 256)) * 1024 * 1024
        )

    @staticmethod
    def _build_source_registry(source_configs: object) -> SourceRegistry:
        """仅为受代码规则认可的机构主机扩展官方来源注册表。"""

        registrations = list(DEFAULT_SOURCE_REGISTRY.registrations)
        claimed = {
            domain
            for registration in registrations
            for domain in registration.domains
        }
        if not isinstance(source_configs, list):
            return DEFAULT_SOURCE_REGISTRY
        for source_index, source in enumerate(source_configs):
            if not isinstance(source, dict) or source.get("source_role") != "official":
                continue
            start_urls = source.get("start_urls", [])
            if not isinstance(start_urls, list):
                continue
            for url_index, value in enumerate(start_urls):
                try:
                    host = trusted_configured_official_host(
                        str(value), DEFAULT_SOURCE_REGISTRY
                    )
                except (UrlPolicyViolation, ValueError):
                    # AppConfig validation reports the actionable error to users.
                    # Keeping this fail-closed check also protects programmatic
                    # callers that construct AppConfig without validation.
                    continue
                existing = DEFAULT_SOURCE_REGISTRY.registration_for_host(host) if host else None
                if (
                    not host
                    or host in claimed
                    or existing is not None
                ):
                    continue
                registrations.append(SourceRegistration(
                    key=f"configured_official_{source_index}_{url_index}",
                    display_name=str(source.get("name") or host),
                    role=SourceRole.OFFICIAL,
                    domains=(host,),
                    https_only=True,
                    exact_hosts_only=True,
                ))
                claimed.add(host)
        return SourceRegistry(tuple(registrations))

    def _role_for_url(self, url: str) -> SourceRole:
        try:
            registration = self.registry.registration_for_url(
                url, enforce_transport=False
            )
        except UrlPolicyViolation:
            return SourceRole.UNKNOWN
        if registration is None:
            return SourceRole.UNKNOWN
        # Exact infrastructure registered only for contextual attachments must
        # never become a standalone official notice or a commercial lead's
        # apparent "official original" merely because its host is trusted for
        # files.
        if (
            registration.role is SourceRole.OFFICIAL
            and not registration.official_notice_eligible
        ):
            return SourceRole.UNKNOWN
        return registration.role

    def _official_attachment_source_allowed(
        self,
        attachment_url: str,
        official_notice_url: str,
    ) -> bool:
        return self.registry.official_attachment_registration(
            attachment_url,
            official_notice_url,
        ) is not None

    def _official_notice_source_allowed(self, url: str) -> bool:
        try:
            return self.registry.official_notice_registration(
                url, enforce_transport=True
            ) is not None
        except UrlPolicyViolation:
            return False

    def _gate_policy(self, origin: str, final_url: str) -> ContentGatePolicy:
        base = ContentGatePolicy()
        official_hosts = set(base.official_hosts)
        allowed_pairs = set(base.allowed_redirect_pairs)
        origin_registration = None
        final_registration = None
        for url, is_origin in ((origin, True), (final_url, False)):
            try:
                registration = self.registry.registration_for_url(
                    url, enforce_transport=False
                )
            except UrlPolicyViolation:
                registration = None
            if registration is None or registration.role is not SourceRole.OFFICIAL:
                continue
            host = (urlparse(url).hostname or "").lower().rstrip(".")
            if host:
                official_hosts.add(host)
            if is_origin:
                origin_registration = registration
            else:
                final_registration = registration
        origin_host = (urlparse(origin).hostname or "").lower().rstrip(".")
        final_host = (urlparse(final_url).hostname or "").lower().rstrip(".")
        if (
            origin_host
            and final_host
            and origin_host != final_host
            and origin_registration is not None
            and final_registration is not None
            and origin_registration.key == final_registration.key
        ):
            allowed_pairs.add((origin_host, final_host))
        return ContentGatePolicy(
            official_hosts=frozenset(official_hosts),
            allowed_redirect_pairs=frozenset(allowed_pairs),
            minimum_html_text_chars=base.minimum_html_text_chars,
            insecure_test_hosts=base.insecure_test_hosts,
        )

    def run(self) -> dict[str, int]:
        query = self.config.data.get("_query", {})
        run_id = query.get("id") or datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
        summary = {
            "sources": 0,
            "notices": 0,
            "delivered": 0,
            "review": 0,
            "unresolved": 0,
            "failed": 0,
            "partial_sources": 0,
            "source_failures": 0,
            "aliases": 0,
            "cataloged": 0,
        }
        canonical_urls: dict[str, str] = {}
        state_lock = threading.Lock()

        def collect(source):
            self._collect_source(source, run_id, query, summary, canonical_urls, state_lock)

        # Each source adapter talks to a different site with its own client
        # session, so sources can be collected concurrently; shared counters
        # go through state_lock and every database call is serialized by the
        # Database's own lock.
        if len(self.sources) > 1:
            with ThreadPoolExecutor(max_workers=len(self.sources), thread_name_prefix="source") as pool:
                list(pool.map(collect, self.sources))
        else:
            for source in self.sources:
                collect(source)
        export_manifest(self.db, self.config.output_dir)
        export_manifest(self.db, self.config.output_dir)
        export_verified_manifest(self.db, self.config.output_dir)
        export_current_results(self.db, self.config.output_dir)
        export_download_links(self.db, self.config.output_dir)
        export_provenance(self.db, self.config.output_dir)
        export_artifact_refs(self.db, self.config.output_dir)
        export_notice_aliases(self.db, self.config.output_dir)
        export_coverage(self.db, self.config.output_dir)
        return summary

    def _collect_source(self, source, run_id, query, summary, canonical_urls, state_lock):
        coverage = Coverage(
            source=source.name,
            target_start=self.config.start_date.isoformat(),
            target_end=self.config.end_date.isoformat(),
        )
        with state_lock:
            summary["sources"] += 1
        LOGGER.info("开始来源：%s", source.name)
        consecutive_detail_blocks = 0
        try:
            self.db.record_coverage(run_id, coverage)
            if source.config.get("_query_unsupported"):
                coverage.status = "unsupported"
                coverage.message = source.config["_query_unsupported"]
                with state_lock:
                    summary["partial_sources"] += 1
                return
            progress_setter = getattr(source, "set_progress_callback", None)
            if progress_setter:
                def on_source_progress(state, message):
                    coverage.status = state
                    coverage.message = message
                    self.db.record_coverage(run_id, coverage)
                    LOGGER.info("%s：%s", source.name, message)
                progress_setter(on_source_progress)
            source.prepare()
            processed = 0
            reviewed = 0
            limit = (query.get("criteria", {}).get("max_candidates", 100)
                     if query.get("criteria", {}).get("mode") == "ai_recall" else 0)
            for notice in source.iter_notices(self.config.start_date, self.config.end_date, coverage):
                if limit and processed >= limit and not isinstance(source, RecallSource):
                    coverage.truncated = True
                    coverage.status = "partial"
                    coverage.message = (coverage.message + f" 已达到本来源 {limit} 条处理上限；"
                                        "仍有候选未解析，请缩短日期分段检测。").strip()
                    break
                processed += 1
                with state_lock:
                    summary["notices"] += 1
                    url_key = canonical_url_key(notice.url)
                    canonical_identity = canonical_urls.get(url_key)
                if canonical_identity:
                    self.db.upsert_notice(notice, status="alias")
                    self.db.record_notice_alias(
                        notice, canonical_identity, "同一规范化官方详情 URL"
                    )
                    with state_lock:
                        summary["aliases"] += 1
                    continue
                try:
                    outcome = self._process_notice(source, notice, coverage)
                    consecutive_detail_blocks = 0
                    with state_lock:
                        canonical_urls[url_key] = notice.identity
                        if notice.origin_url:
                            canonical_urls[
                                canonical_url_key(notice.origin_url)
                            ] = notice.identity
                        if outcome in summary:
                            summary[outcome] += 1
                    if query and outcome == "unresolved":
                        coverage.status = "partial"
                        coverage.message = "部分公告未取得可验证的官方正文，字段或下载链接可能缺失。"
                except SourceBlocked as exc:
                    notice.metadata["collection_error"] = str(exc)
                    self.db.upsert_notice(notice, status="blocked")
                    self.db.set_notice_status(notice.identity, "blocked")
                    coverage.blocked += 1
                    consecutive_detail_blocks += 1
                    hard_block = any(token in str(exc).lower() for token in (
                        "429", "waf", "验证码", "限频", "频繁访问"
                    ))
                    LOGGER.warning("详情访问受阻：%s：%s", notice.url, exc)
                    if hard_block or consecutive_detail_blocks >= 3:
                        if query.get("criteria", {}).get("mode") == "ai_recall":
                            notice.metadata["query_id"] = run_id
                            notice.metadata["query_review"] = {
                                "status": "unread", "reason": "正文访问受阻，保留待复核", "evidence": []}
                            self.db.upsert_notice(notice, status="blocked")
                        raise
                except Exception as exc:
                    coverage.downloads_failed += 1
                    with state_lock:
                        summary["failed"] += 1
                    notice.metadata["collection_error"] = str(exc)
                    self.db.upsert_notice(notice, status="failed")
                    LOGGER.exception("公告处理失败：%s：%s", notice.url, exc)
                if query:
                    if query.get("criteria", {}).get("mode") == "ai_recall":
                        self.db.record_coverage(run_id, coverage)
                        if reviewed >= limit:
                            review = {"status": "not_run",
                                      "reason": "本来源 AI 复核额度已用完，公告保留待人工复核", "evidence": []}
                        else:
                            review = (self.query_reviewer.review(notice) if self.query_reviewer else
                                      {"status": "not_run", "reason": "AI 未执行，保留待复核", "evidence": []})
                            if review["status"] not in {"unread", "not_run"}:
                                reviewed += 1
                        review["origin"] = notice.metadata.get("recall_origin", "broad")
                        notice.metadata["query_review"] = review
                        if notice.metadata["query_review"]["status"] in {"error", "not_run", "unread"}:
                            coverage.status = "partial"
                            message = "部分公告未完成 AI 复核，已保留待人工复核。"
                            if message not in coverage.message:
                                coverage.message = (coverage.message + " " + message).strip()
                    # Publish this query's row only after processing the fresh
                    # detail, not while joins still contain an older scan.
                    notice.metadata["query_id"] = run_id
                    self.db.upsert_notice(notice, status=self.db.notice_status(notice.identity) or "discovered")
                    self.db.record_coverage(run_id, coverage)
            if coverage.status == "running":
                incomplete = (
                    coverage.truncated
                    or not coverage.reached_start
                    or not coverage.reached_end
                    or coverage.blocked > 0
                    or coverage.downloads_failed > 0
                )
                if incomplete:
                    coverage.status = "partial"
                else:
                    coverage.status = "ok" if coverage.notices else "empty"
        except SourceBlocked as exc:
            coverage.status = "blocked"
            coverage.blocked += 1
            coverage.message = str(exc)
            LOGGER.warning("来源熔断：%s：%s", source.name, exc)
        except Exception as exc:
            coverage.status = "failed"
            coverage.message = str(exc)
            LOGGER.exception("来源失败：%s：%s", source.name, exc)
        finally:
            source.close()
            with state_lock:
                if coverage.status == "partial":
                    summary["partial_sources"] += 1
                elif coverage.status in ("blocked", "failed"):
                    summary["source_failures"] += 1
            self.db.record_coverage(run_id, coverage)
            LOGGER.info(
                "来源结束：%s status=%s pages=%d notices=%d downloads=%d",
                source.name, coverage.status, coverage.pages, coverage.notices, coverage.downloads_ok,
            )

    @staticmethod
    def _candidate_priority(candidate: OfficialCandidate) -> tuple[int, str]:
        order = {
            OfficialCandidateKind.CCGP_INTENTION_PROJECT: 0,
            OfficialCandidateKind.CCGP_INTENTION_GROUP: 1,
            OfficialCandidateKind.OFFICIAL_NOTICE: 2,
            OfficialCandidateKind.OFFICIAL_ATTACHMENT: 3,
        }
        return order.get(candidate.kind, 9), candidate.url

    def download_notices(
        self,
        notice_ids: list[str],
        *,
        include_notice: bool = True,
        include_attachments: bool = True,
    ) -> list[dict[str, object]]:
        """Download only originals explicitly selected by the user.

        Every response is revalidated as an HTTPS official source and every
        delivered copy is hash-checked.  Failures stay scoped to one notice so
        a batch can return useful partial results.
        """

        results: list[dict[str, object]] = []
        source_by_name = {source.name: source for source in self.sources}
        prepared: set[str] = set()
        source_errors = dict(self.download_source_errors)
        for identity in notice_ids:
            try:
                notice = self.db.get_notice(identity)
                source = source_by_name.get(notice.source) if notice is not None else None
                if source is not None:
                    if source.name in source_errors:
                        raise SourceBlocked(source_errors[source.name])
                    if source.name not in prepared:
                        try:
                            source.prepare()
                        except Exception as exc:
                            source_errors[source.name] = str(exc)
                            raise
                        prepared.add(source.name)
                results.append(self._download_notice(
                    identity,
                    include_notice=include_notice,
                    include_attachments=include_attachments,
                    source=source,
                ))
            except Exception as exc:
                LOGGER.exception("按需下载失败：%s：%s", identity, exc)
                notice = self.db.get_notice(identity)
                if notice is not None:
                    notice.metadata["download_error"] = str(exc)
                    self.db.upsert_notice(notice, status="download_failed")
                results.append({
                    "notice_id": identity,
                    "status": "failed",
                    "files": [],
                    "errors": [str(exc)],
                    "source_changed_since_scan": False,
                })
        export_current_results(self.db, self.config.output_dir)
        export_download_links(self.db, self.config.output_dir)
        export_manifest(self.db, self.config.output_dir)
        export_verified_manifest(self.db, self.config.output_dir)
        export_artifact_refs(self.db, self.config.output_dir)
        return results

    def _download_notice(
        self,
        identity: str,
        *,
        include_notice: bool,
        include_attachments: bool,
        source: SourceAdapter | None = None,
    ) -> dict[str, object]:
        notice = self.db.get_notice(identity)
        if notice is None:
            raise ValueError(f"未找到标讯: {identity}")
        requested_url = notice.publication_url or notice.origin_url or notice.url
        if (
            not requested_url
            or not self._official_notice_source_allowed(requested_url)
        ):
            raise ValueError("该标讯尚未追溯到可验证的 HTTPS 官方原公告")

        previous_scan_sha = str(notice.metadata.get("scan_sha256", ""))
        client = source.client if source is not None else self.client
        if source is not None:
            request_notice = replace(notice, url=requested_url)
            detail = source.fetch_detail(request_notice)
            requested_url = request_notice.url
        else:
            response = client.request(requested_url)
            detail = RawDocument(
                body=response.body, url=response.url, headers=response.headers,
                analysis_body=response.analysis_body,
            )
        official = self._verify_official_response(
            notice,
            requested_url=requested_url,
            detail=detail,
            discovery_url=notice.discovery_url or notice.url,
            client=client,
            persist_original=True,
        )
        if official is None or official.artifact is None:
            raise ArtifactRestricted("官方公告未通过来源和内容门禁")
        notice.metadata.pop("collection_error", None)
        current_sha = official.artifact.sha256
        changed = bool(previous_scan_sha and previous_scan_sha != current_sha)
        files: list[dict[str, object]] = []
        errors: list[str] = []

        if include_notice:
            destination = self.store.deliver_requested(notice, official.artifact)
            self.db.record_artifact(
                notice,
                official.artifact,
                status="downloaded",
                delivery_path=destination,
            )
            files.append(self._download_file_result(
                official.artifact,
                destination,
                kind="notice",
                source_changed_since_scan=changed,
            ))

        if include_attachments:
            references = (source.discover_artifacts(notice, official.detail)
                          if source is not None else list(official.attachments))
            if not references:
                references = self.db.attachment_refs_for_notice(identity)
            seen_urls: set[str] = set()
            for reference in references:
                if reference.url in seen_urls:
                    continue
                seen_urls.add(reference.url)
                try:
                    artifact = self._download_requested_attachment(
                        notice,
                        reference,
                        official_notice_url=requested_url,
                        referer=reference.referer or official.detail.url,
                        client=client,
                        headers=source.artifact_headers(reference) if source is not None else None,
                    )
                    destination = self.store.deliver_requested(notice, artifact)
                    self.db.record_artifact(
                        notice,
                        artifact,
                        status="downloaded",
                        delivery_path=destination,
                    )
                    self.db.record_artifact_ref(notice, reference, status="downloaded")
                    files.append(self._download_file_result(
                        artifact,
                        destination,
                        kind="attachment",
                        source_changed_since_scan=False,
                    ))
                except (ArtifactRestricted, SourceBlocked, FetchError, OSError, RuntimeError) as exc:
                    self.db.record_artifact_ref(
                        notice, reference, status="failed", error=str(exc)
                    )
                    errors.append(f"{reference.original_name or reference.label or reference.url}: {exc}")

        status = "downloaded" if files and not errors else "partial" if files else "failed"
        if not files and not errors:
            errors.append("未发现可下载的附件；请查看下载链接表中的公告入口")
        notice.metadata.pop("download_error", None)
        if errors:
            notice.metadata["download_error"] = "；".join(errors)
        self.db.upsert_notice(notice, status="cataloged" if status == "downloaded" else "download_failed")
        return {
            "notice_id": identity,
            "status": status,
            "files": files,
            "errors": errors,
            "source_changed_since_scan": changed,
        }

    @staticmethod
    def _download_file_result(
        artifact: StoredArtifact,
        destination: Path,
        *,
        kind: str,
        source_changed_since_scan: bool,
    ) -> dict[str, object]:
        return {
            "kind": kind,
            "name": artifact.original_name,
            "source_url": artifact.source_url,
            "url": artifact.source_url,
            "path": str(destination),
            "sha256": artifact.sha256,
            "size": artifact.size,
            "source_changed_since_scan": source_changed_since_scan,
        }

    def _download_requested_attachment(
        self,
        notice: Notice,
        reference: AttachmentRef,
        *,
        official_notice_url: str,
        referer: str,
        client: HttpClient | None = None,
        headers: dict[str, str] | None = None,
    ) -> StoredArtifact:
        if reference.access != "public_direct":
            raise ArtifactRestricted(f"附件需要额外访问条件: {reference.access}")
        if not self._official_attachment_source_allowed(
            reference.url, official_notice_url
        ):
            raise ArtifactRestricted("附件不属于该公告允许的 HTTPS 官方来源")
        candidate = self.store.download_candidate(
            client or self.client,
            url=reference.url,
            suggested_name=reference.original_name,
            headers={**(headers or {}), "Referer": referer},
        )
        if candidate.size > self.max_gate_bytes:
            raise ArtifactRestricted("附件超过当前门禁内存校验上限")
        body = candidate.vault_path.read_bytes()
        verdict = evaluate_content(
            ContentGateInput(
                source_role=SourceRole.OFFICIAL.value,
                origin=reference.url,
                final_url=candidate.final_url or reference.url,
                content_type=candidate.content_type,
                body=body,
                document_kind="official_attachment",
            ),
            self._gate_policy(reference.url, candidate.final_url or reference.url),
        )
        if not verdict.allowed:
            raise ArtifactRestricted("附件未通过内容门禁：" + "；".join(verdict.reasons))
        inspection = inspect_artifact(
            candidate.vault_path,
            candidate.original_name,
            candidate.content_type,
        )
        if inspection.blocked:
            raise ArtifactRestricted(inspection.reason)
        candidate = self.store.promote_candidate(candidate)
        return replace(
            candidate,
            document_kind=f"official_{verdict.detected_kind}",
            source_role=SourceRole.OFFICIAL.value,
            origin_verified=True,
            gate_status="allowed",
            gate_code="",
            delivery_eligible=True,
            discovery_url=notice.discovery_url,
            official_notice_url=official_notice_url,
        )

    def _record_lead_response(
        self,
        notice: Notice,
        detail: RawDocument,
        role: SourceRole,
    ) -> None:
        if self.delivery_mode == "on_demand":
            # The response was read only to find and verify the official source.
            # Catalogue mode must not create a local source-file copy.
            return
        content_type = detail.headers.get("content-type", "")
        lead = self.store.ingest_evidence_bytes(
            detail.body,
            original_name=f"线索页_{safe_component(notice.external_id)}.html",
            source_url=notice.url,
            content_type=content_type.split(";", 1)[0],
        )
        lead = replace(
            lead,
            final_url=detail.url,
            source_role=role.value,
            discovery_url=notice.url,
        )
        self.db.record_artifact(notice, lead, status="lead_evidence")
        if detail.analysis_body is not None and detail.analysis_body != detail.body:
            snapshot = self.store.ingest_render_snapshot(
                detail.analysis_body,
                original_name=f"线索渲染快照_{safe_component(notice.external_id)}.html",
                source_url=f"{notice.url}#rendered-analysis-snapshot",
                content_type="text/html",
            )
            snapshot = replace(snapshot, final_url=detail.url, discovery_url=notice.url)
            self.db.record_artifact(notice, snapshot, status="analysis_snapshot")

    def _lead_access_gate(
        self, notice: Notice, detail: RawDocument, role: SourceRole
    ) -> None:
        verdict = evaluate_content(ContentGateInput(
            source_role=role.value,
            origin=notice.url,
            final_url=detail.url,
            content_type=detail.headers.get("content-type", ""),
            body=detail.body,
            analysis_body=detail.analysis_body,
            document_kind="",
        ))
        access_errors = _ACCESS_GATE_CODES.intersection(verdict.error_codes)
        if access_errors:
            raise SourceBlocked(
                "线索站仍处于登录/验证码/WAF 页面，已暂停且不会保存为原文："
                + ",".join(sorted(access_errors))
            )

    def _verify_official_response(
        self,
        notice: Notice,
        *,
        requested_url: str,
        detail: RawDocument,
        discovery_url: str,
        client: HttpClient,
        persist_original: bool | None = None,
    ) -> _VerifiedOfficial | None:
        if not self._official_notice_source_allowed(requested_url):
            return None
        content_type = detail.headers.get("content-type", "")
        verdict = evaluate_content(
            ContentGateInput(
                source_role=SourceRole.OFFICIAL.value,
                origin=requested_url,
                final_url=detail.url,
                content_type=content_type,
                body=detail.body,
                analysis_body=detail.analysis_body,
                expected_title=notice.title,
                expected_buyer=notice.buyer,
                document_kind="",
            ),
            self._gate_policy(requested_url, detail.url),
        )
        if not verdict.allowed:
            if self.delivery_mode == "automatic":
                rejected = self.store.ingest_evidence_bytes(
                    detail.body,
                    original_name=f"官方响应未通过门禁_{safe_component(notice.external_id)}.bin",
                    source_url=requested_url,
                    content_type=content_type.split(";", 1)[0],
                )
                rejected = replace(
                    rejected,
                    final_url=detail.url,
                    document_kind=f"rejected_{verdict.detected_kind}",
                    source_role=SourceRole.OFFICIAL.value,
                    origin_verified=False,
                    gate_status="rejected",
                    gate_code=",".join(verdict.error_codes),
                    discovery_url=discovery_url,
                    official_notice_url=requested_url,
                )
                self.db.record_artifact(
                    notice,
                    rejected,
                    status="gate_rejected",
                    error="；".join(verdict.reasons),
                )
            return None

        extension = {
            "html": ".html",
            "json": ".json",
            "pdf": ".pdf",
            "doc": ".doc",
            "docx": ".docx",
            "xls": ".xls",
            "xlsx": ".xlsx",
            "zip": ".zip",
            "rar": ".rar",
            "7z": ".7z",
        }.get(verdict.detected_kind, ".bin")
        artifact: StoredArtifact | None = None
        unchanged = False
        should_persist = (
            self.delivery_mode == "automatic"
            if persist_original is None
            else persist_original
        )
        if should_persist:
            artifact = self.store.ingest_bytes(
                detail.body,
                original_name=(
                    f"官方公告原文_{safe_component(notice.external_id)}{extension}"
                ),
                source_url=requested_url,
                content_type=content_type.split(";", 1)[0],
            )
            unchanged = self.db.artifact_sha(notice.identity, requested_url) == artifact.sha256
            artifact = replace(
                artifact,
                final_url=detail.url,
                document_kind=f"official_{verdict.detected_kind}",
                source_role=SourceRole.OFFICIAL.value,
                origin_verified=True,
                gate_status="allowed",
                gate_code="",
                delivery_eligible=True,
                discovery_url=discovery_url,
                official_notice_url=requested_url,
            )

        parsed = parse_html_document(detail.body, detail.url, content_type)
        text = parsed.text
        if (
            artifact is not None
            and not text
            and verdict.detected_kind not in {"html", "json"}
        ):
            extracted = extract_text(artifact.vault_path)
            text = extracted.text
        notice.body_text = text
        enrich_purchaser(notice, text)
        notice.attachments = list(parsed.attachments)
        notice.metadata["scan_sha256"] = sha256_bytes(detail.body)
        notice.metadata["scan_final_url"] = detail.url
        notice.metadata["scan_fetched_at"] = datetime.now().astimezone().isoformat()
        notice.discovery_url = discovery_url
        notice.publication_url = requested_url
        notice.origin_url = detail.url
        notice.source_role = SourceRole.OFFICIAL.value
        notice.origin_verified = True
        self.db.upsert_notice(
            notice, status=self.db.notice_status(notice.identity) or "official_verified"
        )
        if artifact is not None:
            self.db.record_artifact(notice, artifact, status="verified")
        return _VerifiedOfficial(
            requested_url=requested_url,
            detail=detail,
            text=text,
            attachments=parsed.attachments,
            artifact=artifact,
            client=client,
            unchanged=unchanged,
        )

    def _resolve_from_lead(
        self,
        notice: Notice,
        detail: RawDocument,
    ) -> _VerifiedOfficial | None:
        candidate_body = detail.analysis_body or detail.body
        try:
            candidates = extract_official_candidates(
                candidate_body,
                detail.url,
                detail.headers.get("content-type", ""),
                registry=self.registry,
                require_https_lead=False,
                cross_domain_only=True,
                upgrade_registered_http=True,
            )
        except (ValueError, UrlPolicyViolation) as exc:
            self.db.record_provenance_edge(
                notice,
                discovery_url=notice.url,
                official_url="",
                relation="unresolved",
                method="official_link_extraction",
                verified=False,
                evidence=str(exc),
            )
            return None

        primary = [
            candidate
            for candidate in sorted(candidates, key=self._candidate_priority)
            if (
                candidate.kind is not OfficialCandidateKind.OFFICIAL_ATTACHMENT
                and self._role_for_url(candidate.url) is SourceRole.OFFICIAL
            )
        ][:20]
        if not primary:
            self.db.record_provenance_edge(
                notice,
                discovery_url=notice.url,
                official_url="",
                relation="unresolved",
                method="official_link_extraction",
                verified=False,
                evidence="线索页没有可验证的官方公告链接",
            )
            return None

        for candidate in primary:
            requested_url = candidate.url
            selected_candidate = candidate
            try:
                result = self.client.request(requested_url)
                if candidate.kind is OfficialCandidateKind.CCGP_INTENTION_GROUP:
                    project_candidate = extract_matching_official_detail_candidate(
                        result.body,
                        result.url,
                        notice.title,
                        notice.buyer,
                        str(notice.metadata.get("project_code", "")),
                        result.headers.get("content-type", ""),
                        registry=self.registry,
                    )
                    if project_candidate is None:
                        self.db.record_provenance_edge(
                            notice,
                            discovery_url=notice.url,
                            official_url=requested_url,
                            relation="official_group_unresolved",
                            method=candidate.discovery_method.value,
                            verified=False,
                            evidence=(
                                "官方整批页无法唯一匹配当前项目详情；"
                                "整批页不会作为项目原文交付"
                            ),
                        )
                        continue
                    self.db.record_provenance_edge(
                        notice,
                        discovery_url=notice.url,
                        official_url=requested_url,
                        relation="official_group_intermediate",
                        method=candidate.discovery_method.value,
                        verified=False,
                        evidence=(
                            "仅作为中间追溯页，不可交付；"
                            f"project_url={project_candidate.url}"
                        ),
                    )
                    selected_candidate = project_candidate
                    requested_url = project_candidate.url
                    if not self._official_notice_source_allowed(requested_url):
                        self.db.record_provenance_edge(
                            notice,
                            discovery_url=notice.url,
                            official_url=requested_url,
                            relation="official_group_unresolved",
                            method=project_candidate.discovery_method.value,
                            verified=False,
                            evidence="匹配结果不是可作为公告页的官方来源",
                        )
                        continue
                    result = self.client.request(requested_url)
                official_detail = RawDocument(
                    body=result.body,
                    url=result.url,
                    headers=result.headers,
                    analysis_body=None,
                )
                verified = self._verify_official_response(
                    notice,
                    requested_url=requested_url,
                    detail=official_detail,
                    discovery_url=notice.url,
                    client=self.client,
                )
            except (FetchError, OSError, RuntimeError) as exc:
                self.db.record_provenance_edge(
                    notice,
                    discovery_url=notice.url,
                    official_url=requested_url,
                    relation="discovered_official_candidate",
                    method=candidate.discovery_method.value,
                    verified=False,
                    evidence=str(exc),
                )
                continue
            if verified is None:
                self.db.record_provenance_edge(
                    notice,
                    discovery_url=notice.url,
                    official_url=requested_url,
                    relation="discovered_official_candidate",
                    method=candidate.discovery_method.value,
                    verified=False,
                    evidence="官方响应未通过内容与语义门禁",
                )
                continue
            self.db.record_provenance_edge(
                notice,
                discovery_url=notice.url,
                official_url=requested_url,
                relation="official_original",
                method=selected_candidate.discovery_method.value,
                verified=True,
                evidence=(
                    f"source={selected_candidate.source_name};"
                    f"kind={selected_candidate.kind.value};transport=https"
                ),
            )
            return verified
        return None

    def _process_notice(
        self, source: SourceAdapter, notice: Notice, coverage: Coverage
    ) -> str:
        previous = self.db.notice_status(notice.identity)
        notice.discovery_url = notice.discovery_url or notice.url
        initial_role = self._role_for_url(notice.url)
        declared_role = str(source.config.get("source_role", "auto"))
        if declared_role == SourceRole.COMMERCIAL_LEAD.value:
            initial_role = SourceRole.COMMERCIAL_LEAD
        notice.source_role = initial_role.value
        self.db.upsert_notice(notice, status=previous or "discovered")
        detail = source.fetch_detail(notice)
        coverage.details_ok += 1

        if initial_role is SourceRole.OFFICIAL:
            official = self._verify_official_response(
                notice,
                requested_url=notice.url,
                detail=detail,
                discovery_url=notice.discovery_url,
                client=source.client,
            )
        else:
            self._lead_access_gate(notice, detail, initial_role)
            self._record_lead_response(notice, detail, initial_role)
            official = self._resolve_from_lead(notice, detail)

        if official is None:
            notice.origin_verified = False
            lead_text = parse_html_document(
                detail.analysis_body if detail.analysis_body is not None else detail.body,
                detail.url, detail.headers.get("content-type", "")).text
            notice.metadata["field_source"] = "商业平台线索，官方原文未验证"
            self.db.record_structured_fields(notice, extract_structured_fields(notice, lead_text))
            self.db.upsert_notice(notice, status="unresolved_official_source")
            references = source.discover_artifacts(notice, detail)
            coverage.attachments_found += len(references)
            for reference in references:
                self.db.record_artifact_ref(
                    notice, reference, status="unverified_origin",
                    error="已发现链接，尚未验证官方来源；仅供人工核对",
                )
            return "unresolved"

        if initial_role is SourceRole.OFFICIAL:
            official = replace(official, attachments=tuple(source.discover_artifacts(notice, official.detail)))

        scoped_official_text = scope_text_for_notice(notice, official.text)
        attachment_labels = "\n".join(
            filter(
                None,
                (
                    attachment.label or attachment.original_name or ""
                    for attachment in official.attachments
                ),
            )
        )
        recall_text = (
            f"{notice.title}\n{notice.buyer}\n{scoped_official_text}\n{attachment_labels}"
        )
        complete_attachment_recall = self.recall_mode in {"complete", "p0_complete"}
        if not self.config.data.get("_query") and not is_candidate(recall_text) and not (
            complete_attachment_recall and official.attachments
        ):
            self.db.record_structured_fields(
                notice, extract_structured_fields(notice, scoped_official_text)
            )
            self.db.set_notice_status(notice.identity, "filtered")
            return "filtered"

        notice.attachments = list(official.attachments)
        coverage.attachments_found += len(notice.attachments)
        for attachment in notice.attachments:
            self.db.record_artifact_ref(
                notice,
                attachment,
                status=("available" if self.delivery_mode == "on_demand" else "discovered"),
            )

        if self.delivery_mode == "on_demand":
            self.db.record_structured_fields(
                notice, extract_structured_fields(notice, recall_text)
            )
            result = self._classify_with_review(notice, recall_text)
            self.db.record_classification(notice, result)
            accepted = (
                result.relevant is True
                and result.ai_confirmed
                and not result.needs_review
                and result.confidence >= self.accept_threshold
                and has_strong_evidence("\n".join(result.evidence))
            )
            if accepted:
                self.db.set_notice_status(notice.identity, "metadata_ready")
                return "cataloged"
            if result.relevant is False and not result.needs_review:
                self.db.set_notice_status(notice.identity, "excluded")
                return "excluded"
            self.db.set_notice_status(notice.identity, "review")
            return "review"
        previous_attachment_hashes = {
            attachment.url: self.db.artifact_sha(notice.identity, attachment.url)
            for attachment in notice.attachments
        }

        verified_attachments: list[StoredArtifact] = []
        derived_text: list[str] = []
        unreadable = False
        integrity_review = False
        incomplete_artifacts = False
        for attachment in notice.attachments:
            if attachment.access != "public_direct":
                incomplete_artifacts = True
                coverage.restricted_files += 1
                self.db.record_artifact_ref(notice, attachment, status="restricted")
                continue
            if not self._official_attachment_source_allowed(
                attachment.url,
                official.requested_url,
            ):
                incomplete_artifacts = True
                coverage.restricted_files += 1
                self.db.record_artifact_ref(
                    notice,
                    attachment,
                    status="unverified_origin",
                    error="附件地址不属于该公告允许的 HTTPS 官方来源，未下载",
                )
                continue
            try:
                candidate = self.store.download_candidate(
                    official.client,
                    url=attachment.url,
                    suggested_name=attachment.original_name,
                    headers={"Referer": attachment.referer or official.detail.url},
                )
                if candidate.size > self.max_gate_bytes:
                    candidate = replace(
                        candidate,
                        gate_status="rejected",
                        gate_code="CONTENT_GATE_SIZE_LIMIT",
                        discovery_url=notice.discovery_url,
                        official_notice_url=official.requested_url,
                    )
                    self.db.record_artifact(
                        notice,
                        candidate,
                        status="gate_rejected",
                        error="附件超过当前门禁内存校验上限",
                    )
                    raise ArtifactRestricted("附件超过当前门禁校验上限")
                body = candidate.vault_path.read_bytes()
                verdict = evaluate_content(
                    ContentGateInput(
                        source_role=SourceRole.OFFICIAL.value,
                        origin=attachment.url,
                        final_url=candidate.final_url or attachment.url,
                        content_type=candidate.content_type,
                        body=body,
                        document_kind="official_attachment",
                    ),
                    self._gate_policy(
                        attachment.url, candidate.final_url or attachment.url
                    ),
                )
                if not verdict.allowed:
                    candidate = replace(
                        candidate,
                        gate_status="rejected",
                        gate_code=",".join(verdict.error_codes),
                        discovery_url=notice.discovery_url,
                        official_notice_url=official.requested_url,
                    )
                    self.db.record_artifact(
                        notice,
                        candidate,
                        status="gate_rejected",
                        error="；".join(verdict.reasons),
                    )
                    raise ArtifactRestricted("附件未通过内容门禁")
                inspection = inspect_artifact(
                    candidate.vault_path,
                    candidate.original_name,
                    candidate.content_type,
                )
                if inspection.blocked:
                    candidate = replace(
                        candidate,
                        gate_status="rejected",
                        gate_code="ARTIFACT_CHECK_REJECTED",
                        discovery_url=notice.discovery_url,
                        official_notice_url=official.requested_url,
                    )
                    self.db.record_artifact(
                        notice, candidate, status="invalid", error=inspection.reason
                    )
                    raise ArtifactRestricted(inspection.reason)
                candidate = self.store.promote_candidate(candidate)
                artifact = replace(
                    candidate,
                    document_kind=f"official_{verdict.detected_kind}",
                    source_role=SourceRole.OFFICIAL.value,
                    origin_verified=True,
                    gate_status="allowed",
                    gate_code="",
                    delivery_eligible=True,
                    discovery_url=notice.discovery_url,
                    official_notice_url=official.requested_url,
                )
                integrity_review = integrity_review or inspection.needs_review
                coverage.downloads_ok += 1
                self.db.record_artifact(notice, artifact, status="verified")
                self.db.record_artifact_ref(notice, attachment, status="verified")
                verified_attachments.append(artifact)
                extracted = extract_text(artifact.vault_path)
                if extracted.text:
                    derived_text.append(extracted.text)
                unreadable = unreadable or extracted.needs_review
            except ArtifactRestricted as exc:
                incomplete_artifacts = True
                coverage.downloads_failed += 1
                coverage.restricted_files += 1
                self.db.record_artifact_ref(
                    notice, attachment, status="restricted", error=str(exc)
                )
            except SourceBlocked as exc:
                coverage.downloads_failed += 1
                self.db.record_artifact_ref(
                    notice, attachment, status="blocked", error=str(exc)
                )
                raise
            except (FetchError, OSError, RuntimeError) as exc:
                incomplete_artifacts = True
                coverage.downloads_failed += 1
                self.db.record_artifact_ref(
                    notice, attachment, status="failed", error=str(exc)
                )
                LOGGER.warning("官方附件失败：%s：%s", attachment.url, exc)

        downloaded_hashes = {
            artifact.source_url: artifact.sha256 for artifact in verified_attachments
        }
        attachment_set_unchanged = all(
            attachment.access == "public_direct"
            and previous_attachment_hashes.get(attachment.url)
            == downloaded_hashes.get(attachment.url)
            for attachment in notice.attachments
        )
        combined = "\n".join((recall_text, *derived_text))
        self.db.record_structured_fields(
            notice, extract_structured_fields(notice, combined)
        )
        if (
            previous in ("delivered", "excluded")
            and official.unchanged
            and attachment_set_unchanged
            and not incomplete_artifacts
            and not self.reclassify
        ):
            self.db.set_notice_status(notice.identity, previous)
            return "skipped"

        deliverables = list(verified_attachments)
        if (
            self.include_official_notice_with_attachments
            or (not deliverables and self.include_html_when_no_attachment)
        ):
            if official.artifact is not None:
                deliverables.insert(0, official.artifact)
        result = self._classify_with_review(notice, combined)
        if incomplete_artifacts:
            result = force_review(
                result,
                "存在未通过官方门禁或未成功取得的附件，不能标为完整交付",
            )
        if integrity_review:
            result = force_review(result, "附件内部格式需要人工复核")
        if unreadable and result.relevant is not True:
            result = force_review(result, "存在无法提取文本的附件，不能自动排除")
        if not deliverables:
            result = force_review(result, "没有可交付的已验证官方原件")
        self.db.record_classification(
            notice,
            result,
            artifacts=(
                *((official.artifact,) if official.artifact is not None else ()),
                *verified_attachments,
            ),
        )

        accepted = (
            bool(deliverables)
            and result.relevant is True
            and result.ai_confirmed
            and not result.needs_review
            and result.confidence >= self.accept_threshold
            and has_strong_evidence("\n".join(result.evidence))
        )
        if accepted:
            for artifact in deliverables:
                destination = self.store.deliver(notice, artifact, result, review=False)
                self.db.record_artifact(
                    notice, artifact, status="delivered", delivery_path=destination
                )
            self.db.set_notice_status(notice.identity, "delivered")
            return "delivered"

        should_review = (
            result.relevant is not False
            or result.needs_review
            or not result.ai_confirmed
        )
        if self.copy_uncertain and should_review and deliverables:
            review_result = result
            if review_result.industry == "":
                review_result = replace(review_result, industry="其他行业")
            for artifact in deliverables:
                destination = self.store.deliver(
                    notice, artifact, review_result, review=True
                )
                self.db.record_artifact(
                    notice, artifact, status="review", delivery_path=destination
                )
            self.db.set_notice_status(notice.identity, "review")
            return "review"

        self.db.set_notice_status(notice.identity, "excluded")
        return "excluded"

    def _classify_with_review(self, notice: Notice, text: str) -> Classification:
        def guard(candidate: Classification) -> Classification:
            if candidate.relevant is True and not has_strong_evidence(
                "\n".join(candidate.evidence)
            ):
                return force_review(candidate, "AI 正例证据不含明确网络安全术语")
            if candidate.relevant is False and has_strong_evidence(text):
                return force_review(candidate, "正文存在强网络安全证据，禁止自动排除")
            if candidate.relevant is False and candidate.confidence < self.accept_threshold:
                return force_review(candidate, "排除置信度不足，进入人工复核")
            return candidate

        result = guard(self.classifier.classify(notice, text, stage="final"))
        is_real_ai = not isinstance(self.classifier, RuleClassifier)
        in_review_band = self.review_threshold <= result.confidence < self.accept_threshold
        needs_second = (
            result.relevant is True
            or result.relevant is None
            or result.needs_review
            or in_review_band
        )
        if is_real_ai and needs_second:
            second = guard(
                self.classifier.classify(notice, text, stage="second-review")
            )
            if (
                result.relevant is True
                and second.relevant is True
                and result.ai_confirmed
                and second.ai_confirmed
                and has_strong_evidence("\n".join(result.evidence))
                and has_strong_evidence("\n".join(second.evidence))
            ):
                return guard(
                    replace(second, confidence=min(result.confidence, second.confidence))
                )
            if result.relevant != second.relevant:
                return guard(force_review(second, "两次 AI 判断不一致"))
            if result.relevant is True and second.relevant is True:
                return guard(force_review(second, "两次 AI 正例证据未同时通过校验"))
            return guard(second)
        return guard(result)
