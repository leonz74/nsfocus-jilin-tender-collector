param(
    [string]$Config = "config.json",
    [ValidateRange(1, 65535)]
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = "Stop"

# MyInvocation works in Windows PowerShell 5.1 even when PSScriptRoot is empty.
$launcherPath = $MyInvocation.MyCommand.Path
if ([string]::IsNullOrWhiteSpace($launcherPath)) {
    throw "Cannot locate start-ui.ps1. Please use start-ui.cmd."
}
$projectRoot = [System.IO.Path]::GetDirectoryName(
    [System.IO.Path]::GetFullPath($launcherPath)
)
$examplePath = Join-Path $projectRoot "config.example.json"

if (-not (Test-Path -LiteralPath $examplePath -PathType Leaf)) {
    throw "Missing example config: $examplePath"
}

if ([System.IO.Path]::IsPathRooted($Config)) {
    $configPath = [System.IO.Path]::GetFullPath($Config)
}
else {
    $configPath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $Config))
}

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    $configParent = Split-Path -Parent $configPath
    if (-not (Test-Path -LiteralPath $configParent -PathType Container)) {
        New-Item -ItemType Directory -Path $configParent | Out-Null
    }
    Copy-Item -LiteralPath $examplePath -Destination $configPath
    Write-Host "Created config: $configPath"
}

$env:PYTHONPATH = Join-Path $projectRoot "src"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

Write-Host "Starting local UI: http://127.0.0.1:$Port"
$pythonArguments = @(
    "-m", "tender_downloader", "web",
    "--config", $configPath,
    "--port", [string]$Port
)
if ($NoBrowser) {
    $pythonArguments += "--no-browser"
}
& python @pythonArguments
exit $LASTEXITCODE
