from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from tender_downloader.models import Classification, Notice
from tender_downloader.storage import ImmutableStore
from tender_downloader.utils import safe_component, sha256_bytes, sha256_file


class ImmutableStoreTests(unittest.TestCase):
    def test_long_fake_extension_cannot_bypass_path_limit(self) -> None:
        value = "a." + ("x" * 500)
        self.assertLessEqual(len(safe_component(value, max_length=80)), 80)

    def test_source_vault_and_delivery_hashes_are_identical(self) -> None:
        payload = b"%PDF-1.7\nimmutable-test-payload\x00\xff"
        expected = sha256_bytes(payload)
        with tempfile.TemporaryDirectory() as directory:
            store = ImmutableStore(Path(directory))
            artifact = store.ingest_bytes(
                payload,
                original_name="原始 标书.pdf",
                source_url="https://example.gov.cn/a.pdf",
                content_type="application/pdf",
            )
            artifact = replace(
                artifact,
                document_kind="official_pdf",
                source_role="official",
                origin_verified=True,
                gate_status="allowed",
                delivery_eligible=True,
                official_notice_url="https://example.gov.cn/notice",
            )
            notice = Notice(
                source="fixture",
                authority_rank=100,
                external_id="JL-001",
                title="网络安全项目",
                published_at="2026-08-18",
                url="https://example.gov.cn/notice",
            )
            classification = Classification(
                relevant=True,
                confidence=0.99,
                industry="教育",
                security_categories=("测评咨询",),
                evidence=("网络安全",),
                reason="fixture",
                method="test-ai",
                ai_confirmed=True,
            )
            delivered = store.deliver(notice, artifact, classification)

            self.assertEqual(expected, artifact.sha256)
            self.assertEqual(expected, sha256_file(artifact.vault_path))
            self.assertEqual(expected, sha256_file(delivered))
            self.assertEqual("原始 标书.pdf", delivered.name)


if __name__ == "__main__":
    unittest.main()
