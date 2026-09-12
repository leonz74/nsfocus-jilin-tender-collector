param(
    [string]$Config = "config.json"
)

$ErrorActionPreference = "Stop"
$env:PYTHONPATH = Join-Path $PSScriptRoot "src"
python -m tender_downloader run --config (Join-Path $PSScriptRoot $Config)

