"""Build clean, runtime-inclusive Mac and Windows archives from an allowlist."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
VERSION = "0.3.0"
NAME = "标书采集平台"
ARTIFACTS = {
    "pbs-source.tar.gz": ("147408cc205f7d02c167248d6fcd21a7c8a9a9f6814bb06cefd810cd95b903fe", "https://codeload.github.com/astral-sh/python-build-standalone/tar.gz/refs/tags/20260825"),
    "mac-arm64.tar.gz": ("149038dd0c194c25d4616d7e42a35f67f2edee96412788f74115819b6a4c8548", "https://releases.astral.sh/github/python-build-standalone/releases/download/20260825/cpython-3.13.15%2B20260825-aarch64-apple-darwin-install_only_stripped.tar.gz"),
    "mac-x86_64.tar.gz": ("d33d61f7f4982c94216e14a43599c75657b7d0839277fc72bc6dbac53e8229bc", "https://releases.astral.sh/github/python-build-standalone/releases/download/20260825/cpython-3.13.15%2B20260825-x86_64-apple-darwin-install_only_stripped.tar.gz"),
    "windows-x64.zip": ("791ada5e20aba24524f8d939cdeb069976d632a699fe5cb65274b23f4545e68a", "https://www.python.org/ftp/python/3.13.15/python-3.13.15-embeddable-amd64.zip"),
    "pypdf.whl": ("ee93a2665670ecf57ee81d197a4ca548f3dc15f9cefc56e59b8140866aaa3de5", "https://files.pythonhosted.org/packages/58/13/645df3995075112cb3cce15e8797c205f0f88fb50acc11012b84b071bc22/pypdf-6.18.1-py3-none-any.whl"),
    "certifi.whl": ("62f22742b58a1a33014a2b6b706588a8d7e2a88ae7bd1a6ebe8c992928483775", "https://files.pythonhosted.org/packages/0b/a7/71ac2cff56fec219ed242bb11b8efb69fcc4bec75db06fb7bfe35de520e6/certifi-2026.7.22-py3-none-any.whl"),
}
MAC_LAUNCH = '''#!/bin/bash
set -euo pipefail
RES="$(cd -- "$(dirname -- "$0")/../Resources" && pwd)"
case "$(uname -m)" in
  arm64) ARCH=arm64 ;;
  x86_64) ARCH=x86_64 ;;
  *) /usr/bin/osascript -e 'display dialog "不支持此 Mac 芯片。" buttons {"好"}'; exit 1 ;;
esac
exec "$RES/runtime/$ARCH/bin/python3" -E -s -B -X utf8 "$RES/desktop.py" install-run "$@"
'''
MAC_STOP = '''#!/bin/bash
set -euo pipefail
SOURCE="${BASH_SOURCE[0]}"
while [ -L "$SOURCE" ]; do
  DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
  SOURCE="$(readlink "$SOURCE")"
  case "$SOURCE" in /*) ;; *) SOURCE="$DIR/$SOURCE" ;; esac
done
RES="$(cd -P "$(dirname "$SOURCE")" && pwd)"
case "$(uname -m)" in arm64) ARCH=arm64 ;; *) ARCH=x86_64 ;; esac
exec "$RES/runtime/$ARCH/bin/python3" -E -s -B -X utf8 "$RES/desktop.py" stop "$@"
'''


def digest(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write(path: Path, content: str, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="")
    path.chmod(0o755 if executable else 0o644)


def copy_app(root: Path, cache: Path):
    shutil.copytree(PROJECT / "src/tender_downloader", root / "app/tender_downloader",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
    for filename in ("desktop.py",):
        shutil.copy2(PROJECT / "packaging" / filename, root / filename)
    shutil.copy2(PROJECT / "config.example.json", root / "config.example.json")
    shutil.copy2(PROJECT / "official_urls.example.csv", root / "official_urls.example.csv")
    for filename in ("pypdf.whl", "certifi.whl"):
        with zipfile.ZipFile(cache / filename) as wheel:
            wheel.extractall(root / "vendor")
    write(root / "DEPENDENCIES.json", json.dumps({name: {"sha256": value[0], "url": value[1]}
                                                for name, value in ARTIFACTS.items()}, indent=2) + "\n")
    source_tar = cache / "pbs-source.tar.gz"
    if source_tar.exists():
        with tarfile.open(source_tar) as archive:
            for member in archive.getmembers():
                parts = Path(member.name).parts[1:]
                if member.isfile() and parts and (parts[0] == "licenses" or parts[0].startswith("LICENSE")):
                    target = root / "licenses/python-build-standalone" / Path(*parts)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
    write(root / "第三方组件说明.txt", """标书采集平台 · NSFOCUS 吉林代表处
Python 3.13.15: Python Software Foundation License。Windows 使用 python.org 官方嵌入式包；Mac 使用 Astral python-build-standalone 20260825。
https://www.python.org/downloads/release/python-31315/
https://github.com/astral-sh/python-build-standalone/releases/tag/20260825
运行时 LICENSE.txt 与附带组件许可保留在 runtime 和 licenses 目录。
pypdf 6.18.1: BSD-3-Clause；certifi 2026.7.22: MPL-2.0。许可见 vendor 中对应 dist-info/licenses 目录。
https://pypi.org/project/pypdf/6.18.1/
https://pypi.org/project/certifi/2026.7.22/
行政区划数据及其来源、Silent Public License 1.0 原文见 app/tender_downloader/data。
运行环境与依赖的原始下载地址和 SHA-256 见 DEPENDENCIES.json。
本工具为内部协作分发包，品牌标识归各自权利人所有。
""")


def manifest(root: Path):
    files = {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*"))
             if p.is_file() and not p.is_symlink() and "runtime" not in p.relative_to(root).parts
             and p.name != "MANIFEST.json"}
    write(root / "MANIFEST.json", json.dumps({"app": "cn.nsfocus.jilin.tendercollector", "version": VERSION,
                                             "files": files}, ensure_ascii=False, indent=2) + "\n")


def clean_runtime(root: Path):
    # Runtime caches carry upstream build paths; regenerate nothing on launch.
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache)
    for item in root.rglob("*.pyc"):
        item.unlink()


def mac_package(stage: Path, cache: Path) -> Path:
    package = stage / f"{NAME}_Mac_v{VERSION}"
    bundle = package / (NAME + ".app")
    resources = bundle / "Contents/Resources"
    resources.mkdir(parents=True)
    copy_app(resources, cache)
    for arch, filename in (("arm64", "mac-arm64.tar.gz"), ("x86_64", "mac-x86_64.tar.gz")):
        with tempfile.TemporaryDirectory(dir=stage) as temp:
            with tarfile.open(cache / filename) as archive:
                archive.extractall(temp, filter="data")
            target = resources / "runtime" / arch
            target.parent.mkdir(exist_ok=True)
            shutil.move(str(Path(temp) / "python"), target)
            clean_runtime(target)
    write(bundle / "Contents/MacOS/nsfocus-tender", MAC_LAUNCH, executable=True)
    write(resources / "stop.command", MAC_STOP, executable=True)
    info = {"CFBundleDevelopmentRegion": "zh_CN", "CFBundleDisplayName": NAME,
            "CFBundleName": NAME, "CFBundleExecutable": "nsfocus-tender",
            "CFBundleIdentifier": "cn.nsfocus.jilin.tendercollector", "CFBundlePackageType": "APPL",
            "CFBundleShortVersionString": VERSION, "CFBundleVersion": VERSION,
            "LSMinimumSystemVersion": "11.0", "LSUIElement": True,
            "NSHighResolutionCapable": True}
    (bundle / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    write(package / "安装并启动.command", '#!/bin/bash\nset -euo pipefail\nBASE="$(cd -- "$(dirname -- "$0")" && pwd)"\nexec "$BASE/' + NAME + '.app/Contents/MacOS/nsfocus-tender" "$@"\n', executable=True)
    write(package / "停止平台.command", '#!/bin/bash\nset -euo pipefail\nBASE="$(cd -- "$(dirname -- "$0")" && pwd)"\nexec "$BASE/' + NAME + '.app/Contents/Resources/stop.command" "$@"\n', executable=True)
    write(package / "使用说明.txt", manual("mac"))
    shutil.copy2(package / "使用说明.txt", resources / "使用说明.txt")
    manifest(resources)
    # Ad-hoc signing supplies local bundle integrity, not Apple notarization.
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(bundle)], check=True)
    subprocess.run(["codesign", "--verify", "--deep", "--strict", str(bundle)], check=True)
    return package


def windows_package(stage: Path, cache: Path) -> Path:
    package = stage / f"{NAME}_Windows_x64_v{VERSION}"
    package.mkdir()
    copy_app(package, cache)
    with zipfile.ZipFile(cache / "windows-x64.zip") as archive:
        archive.extractall(package / "runtime")
    write(package / "runtime/python313._pth", "python313.zip\n.\n../app\n../vendor\n")
    for name, action in (("安装并启动.cmd", "install-run"), ("启动平台.cmd", "install-run"), ("停止平台.cmd", "stop"), ("打开数据目录.cmd", "data")):
        command = ('@echo off\r\nsetlocal DisableDelayedExpansion\r\n'
                   'if not exist "%~dp0runtime\\python.exe" (\r\n'
                   ' echo Please extract the entire ZIP before running this file.\r\n pause\r\n exit /b 1\r\n)\r\n'
                   '"%~dp0runtime\\python.exe" -B -X utf8 "%~dp0desktop.py" ' + action + ' %*\r\n'
                   'if errorlevel 1 (\r\n echo See launcher.log in the application data folder.\r\n pause\r\n exit /b 1\r\n)\r\n')
        write(package / name, command)
    write(package / "使用说明.txt", manual("windows"))
    manifest(package)
    return package


def manual(platform):
    install = ("Mac（Apple Silicon / Intel，macOS 11 及以上）\n"
               "1. 完整解压 ZIP。\n2. 双击“标书采集平台.app”（也可双击“安装并启动.command”）。\n"
               "3. 自动安装到个人“应用程序”目录、创建桌面图标并打开浏览器。\n"
               "4. 以后双击桌面“标书采集平台”即可运行。\n"
               "数据：~/Library/Application Support/NSFOCUS/TenderCollector\n"
               "程序：~/Applications/标书采集平台.app\n"
               "这是未经过 Apple 公证的内部包。首次下载运行若被系统拦截，请核对文件来源和 SHA-256 后，按 macOS“系统设置 → 隐私与安全性”提示允许此应用；不需要关闭系统安全保护。\n"
               if platform == "mac" else
               "Windows 10 / 11，64 位 Intel / AMD\n"
               "1. 右键 ZIP → 全部解压。不要直接在压缩包里打开启动文件。\n"
               "2. 双击“安装并启动.cmd”。\n"
               "3. 自动安装到当前用户目录、创建桌面和开始菜单快捷方式，并打开浏览器。\n"
               "4. 以后双击桌面“标书采集平台”即可运行。\n"
               "数据：%LOCALAPPDATA%\\NSFOCUS\\TenderCollector\\data\n"
               "程序：%LOCALAPPDATA%\\NSFOCUS\\TenderCollector\\app-0.3.0\n"
               "此内部包没有商业代码签名。若公司策略阻止启动，请交给 IT 管理员审核本包，不要关闭防护软件。\n")
    return f"""标书采集平台 · NSFOCUS 吉林代表处
版本 {VERSION}

{install}
无需预装 Python、Node、开发工具或手动安装依赖；部署和启动本身不需要联网，也不要求管理员权限。查询网站和调用 AI 时需要网络。

首次使用
• 页面默认最近 7 天；按需要选择全国 / 省份 / 市 / 区县、行业和关键词，再开始采集。
• 初始数据库为空。公开平台默认启用；招标采购导航网已预置，首次需在来源配置中启用并登录自己的账号。
• 登录网站需要电脑装有 Microsoft Edge 或 Google Chrome（普通界面也可用 Safari）。未安装时请从 https://www.microsoft.com/edge 或 https://www.google.com/chrome/ 安装其中一个，再点击网站的“打开登录窗口”。
• 验证码由用户在真实浏览器中手动完成，完成后工具继续检查登录状态。会员权限和网站访问限制仍然有效。
• AI 为可选项，默认关闭。先填写服务地址和自己的 Key，获取模型列表并选择模型，再测试连接。
• Mac 可点击“保存 Key”存入系统钥匙串。当前 Windows 版本使用临时 Key 或 TENDER_AI_API_KEY 环境变量，尚不支持系统凭据持久保存。

日常使用
• 关闭网页不会停止后台采集；重新双击桌面图标可返回已有服务，不会启动重复任务。
• 要彻底退出，双击“停止标书采集平台”；已采集结果保留，再次启动可以继续查询和导出。
• 默认打开 127.0.0.1:8765。若被其他程序占用，会自动选择空闲端口并打开正确页面。
• 查询期间可以导出已采集的结果。附件链接未验证、会员限制或来源未完整覆盖时，表格保留对应说明；有 URL 不代表已经下载成功。

更新与迁移
先停止平台，再运行新版安装入口。程序与数据目录分开，更新程序保留配置、结果和登录资料。
首次安装成功后可删除解压的安装目录；之后使用桌面图标启动。
本分发包没有真实 Key、账号密码、Cookie、浏览器登录状态、数据库、下载文件或历史查询数据。接收者需要自行配置与登录。
卸载程序时先停止平台，再删除程序目录和桌面快捷方式；数据目录默认保留，按需要另行备份或删除。

故障排查
启动异常请查看数据目录内 launcher.log 和 service.log。
若数据目录无写入权限、来源访问受限或浏览器未安装，按具体提示处理。
只监听本机地址。此版本不是供多人共用的远程服务器部署包。

验收范围
详细验收记录随分发包旁的《打包验收说明》提供。Windows 包在 Mac 上制作，不能将静态检查等同于 Windows 实机运行通过。
"""


def archive_package(package: Path, output: Path) -> Path:
    target = output / (package.name + ".zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(package.rglob("*")):
            name = str(path.relative_to(package.parent))
            if path.is_symlink():
                item = zipfile.ZipInfo(name)
                item.create_system = 3
                item.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(item, str(path.readlink()))
            elif path.is_file():
                archive.write(path, name)
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
    return target


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name, (expected, url) in ARTIFACTS.items():
        path = args.cache / name
        if not path.exists():
            subprocess.run(["curl", "-fL", "--retry", "2", "--max-time", "300", url, "-o", str(path)], check=True)
        if digest(path) != expected:
            raise RuntimeError(f"依赖校验失败：{name}")
    with tempfile.TemporaryDirectory(prefix="tender-release-") as temp:
        stage = Path(temp)
        packages = [mac_package(stage, args.cache), windows_package(stage, args.cache)]
        outputs = [archive_package(package, args.output) for package in packages]
    write(args.output / "SHA256SUMS.txt", "".join(f"{digest(path)}  {path.name}\n" for path in outputs))
    print(json.dumps([{"file": str(p), "bytes": p.stat().st_size, "sha256": digest(p)} for p in outputs], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
