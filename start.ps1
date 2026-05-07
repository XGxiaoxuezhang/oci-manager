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

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r requirements.txt

$ResolvedDataDir = Resolve-Path -Path $DataDir -ErrorAction SilentlyContinue
if (-not $ResolvedDataDir) {
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $ResolvedDataDir = Resolve-Path -Path $DataDir
}

$env:OCI_MANAGER_HOST = $HostName
$env:OCI_MANAGER_PORT = [string]$Port
$env:OCI_MANAGER_DATA_DIR = $ResolvedDataDir.Path
$env:OCI_MANAGER_DEBUG = if ($DebugMode) { "1" } else { "0" }

Write-Host "OCI Manager: http://127.0.0.1:$Port"
Write-Host "Data dir: $($ResolvedDataDir.Path)"
& $VenvPython .\oci-manager\app.py
