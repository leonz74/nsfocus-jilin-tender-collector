from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("tender_desktop", Path(__file__).resolve().parents[1] / "packaging/desktop.py")
desktop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(desktop)


class DesktopDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="desktop test 中文 ")
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_os_lock_is_exclusive_and_released(self):
        with desktop.instance_lock(self.root) as first:
            self.assertTrue(first)
            with desktop.instance_lock(self.root) as second:
                self.assertFalse(second)
        with desktop.instance_lock(self.root) as third:
            self.assertTrue(third)

    def test_fresh_config_recent_dates_and_no_overwrite_on_restart(self):
        from datetime import date, timedelta
        (self.root / "config.example.json").write_text(json.dumps({"ai": {"enabled": False}, "sources": []}), encoding="utf-8")
        data = self.root / "数据"; data.mkdir()
        with patch.object(desktop.socket, "getaddrinfo", side_effect=OSError("offline")):
            path = desktop.initialize_config(self.root, data)
        config = json.loads(path.read_text())
        self.assertEqual(config["start_date"], (date.today() - timedelta(days=6)).isoformat())
        self.assertEqual(config["end_date"], date.today().isoformat())
        self.assertEqual(config["database"], "output/state.sqlite3")
        path.write_text('{"user_setting": true}', encoding="utf-8")
        desktop.initialize_config(self.root, data)
        self.assertEqual(path.read_text(), '{"user_setting": true}')

    def test_bad_instance_data_is_not_a_running_app(self):
        for value in ({}, {"port": "8765"}, {"port": True}, {"port": 99999}):
            desktop.save_json(self.root / "instance.json", value)
            self.assertIsNone(desktop.control(self.root))

    def test_wrong_app_and_redirects_are_not_followed(self):
        counts = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_POST(self):
                counts.append(self.path)
                self.send_response(302)
                self.send_header("Location", "/different-app")
                self.end_headers()
            def do_GET(self):
                counts.append("WRONG")
                self.send_response(200); self.end_headers()
        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        try:
            desktop.save_json(self.root / "instance.json", {"port": server.server_port, "token": "test-only", "instance": "test"})
            self.assertIsNone(desktop.control(self.root))
            self.assertEqual(counts, [desktop.CONTROL])
        finally:
            server.shutdown(); server.server_close(); worker.join()

    def test_install_does_not_overwrite_unrelated_directory(self):
        source = self.root / "来源.app/Contents/Resources"; source.mkdir(parents=True)
        (source / "MANIFEST.json").write_text('{"version":"0.3.0"}')
        destination = self.root / "已存在.app"; destination.mkdir()
        marker = destination / "用户文件.txt"; marker.write_text("keep")
        with patch.object(desktop.sys, "platform", "darwin"):
            with self.assertRaisesRegex(RuntimeError, "避免覆盖"):
                desktop.install(source, self.root, destination, self.root / "桌面")
        self.assertEqual(marker.read_text(), "keep")

    def test_update_is_blocked_while_existing_service_runs(self):
        source = self.root / "来源.app/Contents/Resources"; source.mkdir(parents=True)
        (source / "MANIFEST.json").write_text('{"version":"new"}')
        destination = self.root / "已安装.app"
        old = destination / "Contents/Resources"; old.mkdir(parents=True)
        (old / "MANIFEST.json").write_text('{"version":"old"}')
        with patch.object(desktop.sys, "platform", "darwin"), patch.object(desktop, "control", return_value={"version": "old"}):
            with self.assertRaisesRegex(RuntimeError, "停止"):
                desktop.install(source, self.root, destination, self.root / "桌面")
        self.assertEqual((old / "MANIFEST.json").read_text(), '{"version":"old"}')

    def test_collection_environment_uses_bundled_modules_and_certificates(self):
        cert = self.root / "vendor/certifi/cacert.pem"; cert.parent.mkdir(parents=True); cert.touch()
        with patch.dict(os.environ, {"PYTHONHOME": "/not-the-app", "PYTHONPATH": "/old-copy"}, clear=True), patch.object(desktop.sys, "platform", "darwin"):
            env = desktop.configure_environment(self.root)
        self.assertNotIn("PYTHONHOME", env)
        self.assertEqual(env["PYTHONPATH"].split(os.pathsep), [str(self.root / "app"), str(self.root / "vendor")])
        self.assertEqual(env["SSL_CERT_FILE"], str(cert))
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")


if __name__ == "__main__":
    unittest.main()
