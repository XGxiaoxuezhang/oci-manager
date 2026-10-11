param(
    [int]$Port = 5080,
    [string]$HostName = "127.0.0.1",
    [string]$DataDir = ".\data",
    [switch]$DebugMode
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    py -3 -m venv .venv
}

# 依赖只装一次（requirements 变更时删除 .venv\.deps-installed 重新触发）
$DepsFlag = Join-Path $ProjectRoot ".venv\.deps-installed"
if (-not (Test-Path $DepsFlag)) {
    & $VenvPython -m pip install --upgrade pip
    & $VenvPython -m pip install -r requirements.txt
    New-Item -ItemType File -Force -Path $DepsFlag | Out-Null
}

$ResolvedDataDir = Resolve-Path -Path $DataDir -ErrorAction SilentlyContinue
if (-not $ResolvedDataDir) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $ResolvedDataDir = Resolve-Path -Path $DataDir
}

$env:OCI_MANAGER_HOST = $HostName
$env:OCI_MANAGER_PORT = [string]$Port
$env:OCI_MANAGER_DATA_DIR = $ResolvedDataDir.Path
$env:OCI_MANAGER_DEBUG = if ($DebugMode) { "1" } else { "0" }

Write-Host ("OCI Manager: http://{0}:{1}" -f $HostName, $Port)
Write-Host "Data dir: $($ResolvedDataDir.Path)"
& $VenvPython .\oci-manager\app.py
