from __future__ import annotations

import tempfile
import threading
import unittest
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tender_downloader.cli import _client
from tender_downloader.config import AppConfig
from tender_downloader.http_client import FetchError, HttpClient
from tender_downloader.utils import sha256_bytes


PAYLOAD = (b"0123456789abcdef" * 8192) + b"tail"


class _RangeHandler(BaseHTTPRequestHandler):
    last_range = ""

    def do_GET(self) -> None:  # noqa: N802
        start = 0
        range_value = self.headers.get("Range", "")
        type(self).last_range = range_value
        if range_value.startswith("bytes="):
            start = int(range_value.split("=", 1)[1].split("-", 1)[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD)-1}/{len(PAYLOAD)}")
        else:
            self.send_response(200)
        body = PAYLOAD[start:]
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/pdf")
        self.send_header("ETag", '"fixture-v1"')
        self.send_header("Content-Disposition", "attachment; filename=test.pdf")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        return


class HTTPDownloadTests(unittest.TestCase):
    def test_resumes_partial_file_and_preserves_hash(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/file.pdf"
            client = HttpClient(
                user_agent="test/1.0 (+mailto:test@example.com)",
                timeout_seconds=5,
                delay_seconds=0,
                max_retries=0,
                allow_private_hosts=True,
            )
            with tempfile.TemporaryDirectory() as directory:
                part = Path(directory) / "file.part"
                part.write_bytes(PAYLOAD[:12345])
                Path(f"{part}.json").write_text(json.dumps({
                    "url": url,
                    "etag": '"fixture-v1"',
                    "last_modified": "",
                }), encoding="utf-8")
                result = client.download(url, part)
                self.assertEqual(PAYLOAD, part.read_bytes())
                self.assertEqual(sha256_bytes(PAYLOAD), result.sha256)
                self.assertEqual("test.pdf", result.original_name)
        finally:
            server.shutdown()
            server.server_close()

    def test_partial_without_validator_is_discarded(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/file.pdf"
            client = HttpClient(
                user_agent="test/1.0 (+mailto:test@example.com)",
                timeout_seconds=5,
                delay_seconds=0,
                max_retries=0,
                allow_private_hosts=True,
            )
            with tempfile.TemporaryDirectory() as directory:
                part = Path(directory) / "file.part"
                part.write_bytes(b"untrusted-old-prefix")
                result = client.download(url, part)
                self.assertEqual(PAYLOAD, part.read_bytes())
                self.assertEqual("", _RangeHandler.last_range)
                self.assertEqual(sha256_bytes(PAYLOAD), result.sha256)
        finally:
            server.shutdown()
            server.server_close()

    def test_private_address_is_rejected_by_default(self) -> None:
        client = HttpClient(
            user_agent="test/1.0 (+mailto:test@example.com)",
            timeout_seconds=1,
            delay_seconds=0,
            max_retries=0,
        )
        with self.assertRaises(FetchError):
            client.request("http://127.0.0.1/internal")

    @staticmethod
    def _dns_result(address: str, port: int = 443):
        return [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                (address, port),
            )
        ]

    def test_benchmark_proxy_option_allows_only_domain_dns_results(self) -> None:
        disabled = HttpClient(
            user_agent="test/1.0",
            timeout_seconds=1,
            delay_seconds=0,
            max_retries=0,
        )
        try:
            with patch(
                "tender_downloader.http_client.socket.getaddrinfo",
                return_value=self._dns_result("198.18.0.25"),
            ):
                with self.assertRaisesRegex(FetchError, "非公网地址"):
                    disabled._validate_url(
                        "https://procurement.example.gov.cn/notices"
                    )
        finally:
            disabled.close()

        client = HttpClient(
            user_agent="test/1.0",
            timeout_seconds=1,
            delay_seconds=0,
            max_retries=0,
            allow_benchmark_proxy_hosts=True,
        )
        try:
            with patch(
                "tender_downloader.http_client.socket.getaddrinfo",
                return_value=self._dns_result("198.18.0.25"),
            ):
                client._validate_url("https://procurement.example.gov.cn/notices")

            for literal in ("198.18.0.25", "3323068441", "0xc6120019"):
                with self.subTest(literal=literal):
                    with self.assertRaisesRegex(FetchError, "非公网 IP"):
                        client._validate_url(f"https://{literal}/notices")

            with patch(
                "tender_downloader.http_client.socket.getaddrinfo",
                return_value=self._dns_result("10.23.4.5"),
            ):
                with self.assertRaisesRegex(FetchError, "非公网地址"):
                    client._validate_url("https://private-target.example/notices")

            forked = client.fork_session()
            try:
                self.assertTrue(forked.allow_benchmark_proxy_hosts)
                self.assertFalse(forked.allow_private_hosts)
            finally:
                forked.close()
        finally:
            client.close()

    def test_cli_passes_benchmark_proxy_option_to_http_client(self) -> None:
        config = AppConfig(
            Path("config.json"),
            {
                "http": {
                    "allow_private_hosts": False,
                    "allow_benchmark_proxy_hosts": True,
                }
            },
        )
        client = _client(config)
        try:
            self.assertTrue(client.allow_benchmark_proxy_hosts)
            self.assertFalse(client.allow_private_hosts)
        finally:
            client.close()

    def test_curl_download_validates_each_redirect_before_requesting_it(self) -> None:
        unsafe_locations = (
            "file:///C:/Windows/win.ini",
            "http://127.0.0.1/internal",
            "https://user:password@public.example/internal",
        )
        for location in unsafe_locations:
            with self.subTest(location=location), tempfile.TemporaryDirectory() as directory:
                part = Path(directory) / "candidate.part"

                def redirect_once(command, **_kwargs):
                    header_path = Path(command[command.index("-D") + 1])
                    output_path = Path(command[command.index("-o") + 1])
                    header_path.write_text(
                        f"HTTP/1.1 302 Found\r\nLocation: {location}\r\n\r\n",
                        encoding="iso-8859-1",
                    )
                    output_path.write_bytes(b"")
                    return SimpleNamespace(
                        returncode=0,
                        stdout=b"__CURL_META__302\thttps://93.184.216.34/file",
                        stderr=b"",
                    )

                client = HttpClient(
                    user_agent="test/1.0",
                    timeout_seconds=1,
                    delay_seconds=0,
                    max_retries=0,
                )
                try:
                    with (
                        patch(
                            "tender_downloader.http_client.shutil.which",
                            return_value="curl.exe",
                        ),
                        patch(
                            "tender_downloader.http_client.subprocess.run",
                            side_effect=redirect_once,
                        ) as run,
                    ):
                        with self.assertRaises(FetchError):
                            client._curl_download(
                                "https://93.184.216.34/file",
                                part,
                                suggested_name="file.pdf",
                                headers=None,
                            )
                    self.assertEqual(1, run.call_count)
                finally:
                    client.close()


if __name__ == "__main__":
    unittest.main()
