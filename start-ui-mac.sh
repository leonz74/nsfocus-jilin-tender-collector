#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -x .venv/bin/python ]]; then
  PYTHON_BIN=""
  for candidate in "${TENDER_PYTHON:-}" python3 python3.13 python3.12 python3.11 \
    /opt/homebrew/bin/python3 /usr/local/bin/python3 "$HOME/.local/bin/python3"; do
    if [[ -n "$candidate" ]] && command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      break
    fi
  done
  if [[ -z "$PYTHON_BIN" ]]; then
    echo "需要 Python 3.11 或更新版本。安装后重试，或设置 TENDER_PYTHON 为 Python 可执行文件路径。" >&2
    exit 1
  fi
  "$PYTHON_BIN" -m venv .venv
fi

if ! .venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
  echo "现有 .venv 的 Python 版本过旧。请将 .venv 改名备份后重新运行。" >&2
  exit 1
fi

source .venv/bin/activate
python -m pip install -e '.[pdf]'

if [[ ! -f config.json ]]; then
  cp config.example.json config.json
  python - <<'PY'
import ipaddress
import json
import socket
from datetime import date, timedelta
from pathlib import Path
path = Path("config.json")
config = json.loads(path.read_text(encoding="utf-8"))
config["end_date"] = date.today().isoformat()
config["start_date"] = (date.today() - timedelta(days=6)).isoformat()
try:
    addresses = socket.getaddrinfo("www.jl.gov.cn", 443, type=socket.SOCK_STREAM)
    if any(ipaddress.ip_address(item[4][0]) in ipaddress.ip_network("198.18.0.0/15") for item in addresses):
        config.setdefault("http", {})["allow_benchmark_proxy_hosts"] = True
        print("检测到本机 Fake-IP 代理，已启用 198.18/15 兼容；其他私网地址仍被拒绝。")
except OSError:
    pass
path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY
  echo "已创建 config.json，请在网页中检查日期、来源和 AI 配置。"
fi

export PYTHONUTF8=1
exec python -m tender_downloader web --config config.json --port "${PORT:-8765}" "$@"
