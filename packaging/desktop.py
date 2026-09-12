"""Bundled desktop entry point. No pip, system Python, or admin rights needed."""
from __future__ import annotations

import argparse
import contextlib
import hmac
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
import uuid
import webbrowser

ROOT = Path(__file__).resolve().parent
NAME = "标书采集平台"
VERSION = "0.3.0"
APP_ID = "cn.nsfocus.jilin.tendercollector"
CONTROL = "/__desktop__/control"


def data_directory() -> Path:
    if os.environ.get("TENDER_DATA_DIR"):
        return Path(os.environ["TENDER_DATA_DIR"]).expanduser().resolve()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/NSFOCUS/TenderCollector"
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local"))) / "NSFOCUS/TenderCollector/data"


def runtime(root: Path, *, windowed: bool = False) -> Path:
    if sys.platform == "darwin":
        import platform
        arch = "arm64" if platform.machine() == "arm64" else "x86_64"
        return root / "runtime" / arch / "bin/python3"
    return root / "runtime" / ("pythonw.exe" if windowed else "python.exe")


def configure_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    # Only our own modules should reach collection subprocesses.
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = os.pathsep.join(str(root / part) for part in ("app", "vendor"))
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    cert = root / "vendor/certifi/cacert.pem"
    if sys.platform == "darwin" and not env.get("SSL_CERT_FILE") and cert.exists():
        env["SSL_CERT_FILE"] = str(cert)
    return env


def save_json(path: Path, value: dict) -> None:
    temp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def initialize_config(root: Path, data: Path) -> Path:
    from datetime import date, timedelta
    config = data / "config.json"
    if config.exists():
        return config
    template = json.loads((root / "config.example.json").read_text(encoding="utf-8"))
    template.update(start_date=(date.today() - timedelta(days=6)).isoformat(), end_date=date.today().isoformat())
    template["output_dir"] = "output"
    template["database"] = "output/state.sqlite3"
    # Fake-IP compatibility is detected without widening private-network access.
    try:
        import ipaddress
        addresses = socket.getaddrinfo("www.jl.gov.cn", 443, type=socket.SOCK_STREAM)
        if any(ipaddress.ip_address(x[4][0]) in ipaddress.ip_network("198.18.0.0/15") for x in addresses):
            template.setdefault("http", {})["allow_benchmark_proxy_hosts"] = True
    except OSError:
        pass
    save_json(config, template)
    return config


@contextlib.contextmanager
def instance_lock(data: Path, name: str = "service.lock"):
    """OS releases this lock even after a crash; never trust a stored PID."""
    stream = (data / name).open("a+b")
    acquired = False
    try:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                acquired = True
            except OSError:
                pass
        else:
            import fcntl
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except BlockingIOError:
                pass
        yield acquired
    finally:
        if acquired and os.name == "nt":
            import msvcrt
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        stream.close()


def control(data: Path, action: str = "status") -> dict | None:
    connection = None
    try:
        info = json.loads((data / "instance.json").read_text(encoding="utf-8"))
        port = info["port"]
        if type(port) is not int or not 1 <= port <= 65535:
            return None
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
        connection.request(
            "POST", CONTROL, body=json.dumps({"action": action}).encode(),
            headers={"Authorization": "Bearer " + info["token"], "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        if response.status != 200:
            return None
        result = json.loads(response.read(65536))
        if result.get("app") != APP_ID or result.get("instance") != info["instance"]:
            return None
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError, http.client.HTTPException):
        return None
    finally:
        if connection:
            connection.close()


def serve(root: Path, data: Path, port: int) -> None:
    with instance_lock(data) as acquired:
        if not acquired:
            return
        os.environ.update(configure_environment(root))
        sys.path[:0] = [str(root / "app"), str(root / "vendor")]
        from tender_downloader.webui.server import create_server, WebUIHandler, WebUIAlreadyRunningError
        from tender_downloader.webui.browser_login import BrowserLoginManager
        config = initialize_config(root, data)
        token, identity = secrets.token_urlsafe(32), uuid.uuid4().hex

        class DesktopHandler(WebUIHandler):
            def do_POST(self):
                if self.path != CONTROL:
                    return super().do_POST()
                if (not self._local_host_ok() or not self._origin_ok()
                        or not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token)):
                    self.close_connection = True
                    return self._send_json(403, {"error": "Forbidden"})
                try:
                    request = self._read_json()
                    action = request.get("action")
                    if action not in {"status", "stop"}:
                        return self._send_json(400, {"error": "Unknown action"})
                    self._send_json(200, {"app": APP_ID, "version": VERSION, "instance": identity,
                                          "port": self.server.server_address[1], "pid": os.getpid()})
                    if action == "stop":
                        threading.Thread(target=self.server.shutdown, daemon=True).start()
                except Exception as exc:
                    self._handle_error(exc)

        try:
            server, _ = create_server(config, port=port, browser_login_manager=BrowserLoginManager(profile_root=data / "browser_profiles"))
        except WebUIAlreadyRunningError:
            # The occupied port may belong to a developer copy or another app.
            server, _ = create_server(config, port=0, browser_login_manager=BrowserLoginManager(profile_root=data / "browser_profiles"))
        server.RequestHandlerClass = DesktopHandler
        save_json(data / "instance.json", {"port": server.server_address[1], "token": token, "instance": identity})
        def shutdown(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, shutdown)
        try:
            server.serve_forever(poll_interval=0.2)
        finally:
            server.server_close()  # Stops active collection and closes managed browsers.
            (data / "instance.json").unlink(missing_ok=True)


def launch(root: Path, data: Path, port: int, *, no_browser: bool) -> dict:
    info = control(data)
    if info is None:
        log = data / "service.log"
        if log.exists() and log.stat().st_size > 5 * 1024 * 1024:
            os.replace(log, data / "service.previous.log")
        with log.open("ab") as output:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
            subprocess.Popen([str(runtime(root)), "-B", "-X", "utf8", str(root / "desktop.py"), "serve",
                              "--data-dir", str(data), "--port", str(port), "--no-dialog"],
                             cwd=data, env=configure_environment(root), stdin=subprocess.DEVNULL,
                             stdout=output, stderr=subprocess.STDOUT, **kwargs)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            info = control(data)
            if info:
                break
            time.sleep(0.15)
    if not info:
        raise RuntimeError(f"启动未成功。请查看日志：{data / 'service.log'}")
    if info["version"] != VERSION:
        raise RuntimeError("旧版本仍在运行。请先双击“停止标书采集平台”，再打开新版本；已采集结果会保留。")
    url = f"http://127.0.0.1:{info['port']}/"
    if not no_browser:
        webbrowser.open(url)
    if sys.stdout:
        print(f"{NAME}已运行：{url}")
    return info


def stop(data: Path) -> bool:
    if not control(data, "stop"):
        return False
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        with instance_lock(data) as acquired:
            if acquired:
                return True
        time.sleep(0.2)
    raise RuntimeError("服务仍在保存结果，请稍候再试。没有强制终止进程。")


def windows_folder(csidl: int) -> Path:
    import ctypes
    buffer = ctypes.create_unicode_buffer(32768)
    result = ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buffer)
    if result != 0:
        raise OSError("无法定位 Windows 快捷方式目录")
    return Path(buffer.value)


def shortcuts(root: Path, desktop: Path | None) -> None:
    if sys.platform == "darwin":
        desktop = desktop or Path.home() / "Desktop"
        desktop.mkdir(parents=True, exist_ok=True)
        for name, target in ((NAME + ".app", root.parents[1]), ("停止" + NAME + ".command", root / "stop.command")):
            link = desktop / name
            if link.is_symlink():
                link.unlink()
            if not link.exists():
                link.symlink_to(target)
    else:
        destinations = [desktop] if desktop else [windows_folder(0x10), windows_folder(0x02) / NAME]
        entries = []
        for folder in destinations:
            folder.mkdir(parents=True, exist_ok=True)
            for title, action in ((NAME, "run"), ("停止" + NAME, "stop"), ("打开平台数据目录", "data")):
                entries.append({"link": str(folder / (title + ".lnk")), "target": str(runtime(root, windowed=True)),
                                "args": subprocess.list2cmdline(["-B", "-X", "utf8", str(root / "desktop.py"), action]),
                                "cwd": str(root), "icon": str(runtime(root, windowed=True))})
        script = "$ErrorActionPreference='Stop'; $w=New-Object -ComObject WScript.Shell; " \
                 "$items=ConvertFrom-Json $env:TENDER_SHORTCUTS_JSON; foreach($i in $items){" \
                 "$s=$w.CreateShortcut($i.link);$s.TargetPath=$i.target;$s.Arguments=$i.args;" \
                 "$s.WorkingDirectory=$i.cwd;$s.IconLocation=$i.icon;$s.Save()}"
        env = os.environ.copy()
        env["TENDER_SHORTCUTS_JSON"] = json.dumps(entries, ensure_ascii=True)
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-Command", script],
                       env=env, check=True, creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def install(root: Path, data: Path, destination: Path | None, desktop: Path | None) -> Path:
    if sys.platform == "darwin":
        source = root.parents[1]
        destination = destination or Path.home() / "Applications" / (NAME + ".app")
        relative = Path("Contents/Resources")
    else:
        source = root
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        destination = destination or base / "NSFOCUS/TenderCollector" / ("app-" + VERSION)
        relative = Path(".")
    destination = destination.expanduser().resolve()
    if source.resolve() != destination:
        new_manifest = root / "MANIFEST.json"
        old_manifest = destination / relative / "MANIFEST.json"
        identical = old_manifest.exists() and old_manifest.read_bytes() == new_manifest.read_bytes()
        if not identical:
            if destination.exists() and not old_manifest.exists():
                raise RuntimeError(f"安装位置存在其他文件，为避免覆盖已停止安装：{destination}")
            if control(data):
                raise RuntimeError("请先双击“停止标书采集平台”，再安装更新。配置和采集结果会保留。")
            destination.parent.mkdir(parents=True, exist_ok=True)
            temp = destination.with_name(destination.name + ".install-" + uuid.uuid4().hex)
            backup = destination.with_name(destination.name + ".previous-" + uuid.uuid4().hex)
            try:
                shutil.copytree(source, temp, symlinks=True)
                if destination.exists():
                    destination.rename(backup)
                try:
                    temp.rename(destination)
                except OSError:
                    if backup.exists():
                        backup.rename(destination)
                    raise
                if backup.exists():
                    shutil.rmtree(backup)
            finally:
                if temp.exists():
                    shutil.rmtree(temp)
    installed_root = destination / relative
    shortcuts(installed_root, desktop)
    return installed_root


def notify(message: str, *, error: bool = False) -> None:
    if sys.platform == "darwin":
        script = 'on run argv\ndisplay dialog (item 1 of argv) with title "标书采集平台" buttons {"好"} default button "好"\nend run'
        subprocess.run(["/usr/bin/osascript", "-e", script, message], check=False, stdout=subprocess.DEVNULL)
    elif os.name == "nt":
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, message, NAME, 0x10 if error else 0x40)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=NAME + " · NSFOCUS 吉林代表处")
    parser.add_argument("action", choices=("install-run", "run", "serve", "stop", "data"), nargs="?", default="install-run")
    parser.add_argument("--data-dir", type=Path, default=data_directory())
    parser.add_argument("--install-dir", type=Path)
    parser.add_argument("--desktop-dir", type=Path)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-dialog", action="store_true")
    args = parser.parse_args(argv)
    data = args.data_dir.expanduser().resolve()
    try:
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        if args.action == "serve":
            serve(ROOT, data, args.port)
        elif args.action == "stop":
            stopped = stop(data)
            if not args.no_dialog:
                notify("平台已停止，已采集结果已保留。" if stopped else "平台当前没有运行。")
        elif args.action == "data":
            if os.name == "nt":
                os.startfile(str(data))
            else:
                subprocess.run(["open", str(data)], check=True)
        else:
            root = ROOT
            if args.action == "install-run":
                with instance_lock(data, "install.lock") as acquired:
                    if not acquired:
                        raise RuntimeError("另一安装窗口正在工作，请等待它完成。")
                    root = install(ROOT, data, args.install_dir, args.desktop_dir)
            launch(root, data, args.port, no_browser=args.no_browser)
        return 0
    except Exception as exc:
        message = str(exc)
        try:
            with (data / "launcher.log").open("a", encoding="utf-8") as log:
                traceback.print_exc(file=log)
        except OSError:
            pass
        if sys.stderr:
            print(message, file=sys.stderr)
        if not args.no_dialog:
            notify(message, error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
