from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tender_downloader.artifact_check import inspect_artifact


class ArtifactCheckTests(unittest.TestCase):
    def test_html_login_disguised_as_pdf_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.pdf"
            path.write_bytes(b"<html><body>login</body></html>")
            result = inspect_artifact(path, "fake.pdf", "application/octet-stream")
        self.assertTrue(result.blocked)

    def test_pdf_mime_without_pdf_magic_requires_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fake.bin"
            path.write_bytes(b'{"error":"not a file"}')
            result = inspect_artifact(path, "fake.bin", "application/pdf")
        self.assertTrue(result.needs_review)


if __name__ == "__main__":
    unittest.main()
