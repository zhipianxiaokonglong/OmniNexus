# Release preflight for OmniNexus (Windows PowerShell).
# This script is intentionally read-only; it does not delete files.
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$failed = $false
$python = if (Test-Path (Join-Path $root ".venv\Scripts\python.exe")) {
    Join-Path $root ".venv\Scripts\python.exe"
} else {
    "python"
}

Write-Host "== ruff =="
& $python -m ruff check src tests
if ($LASTEXITCODE -ne 0) { $failed = $true }

Write-Host "== pytest =="
& $python -m pytest -q
if ($LASTEXITCODE -ne 0) { $failed = $true }

Write-Host "== forbidden local artifacts =="
$artifacts = Get-ChildItem -Recurse -Force -File -Include *.db, *.db-wal, *.db-shm, *.log, *.jsonl, .env -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -notmatch "\\\.venv\\" }
if ($artifacts) {
    $artifacts | ForEach-Object { Write-Host "UNEXPECTED: $($_.FullName)" }
    $failed = $true
} else {
    Write-Host "none"
}

Write-Host "== tracked env/secrets patterns =="
$hits = git grep -n -I -E "sk-[A-Za-z0-9]{8,}|AIza[0-9A-Za-z_-]{20,}|xai-[A-Za-z0-9]{8,}|gsk_[A-Za-z0-9]{8,}" -- src tests config .env.example
if ($LASTEXITCODE -eq 0) {
    Write-Host "Review these hits (tests may intentionally contain fake keys):"
    Write-Host $hits
} else {
    Write-Host "none"
}

Write-Host "== test persona markers in runtime code/config =="
$persona = git grep -n -I -E "测试人格|test persona|persona name" -- src tests config
if ($LASTEXITCODE -eq 0) {
    Write-Host "Review these hits:"
    Write-Host $persona
    $failed = $true
} else {
    Write-Host "none"
}

Write-Host "== bundled config matches =="
if ((Get-FileHash config/default.yaml).Hash -ne (Get-FileHash src/omninexus/resources/default.yaml).Hash) {
    Write-Host "MISMATCH: resync src/omninexus/resources/default.yaml"
    $failed = $true
} else {
    Write-Host "ok"
}

if ($failed) {
    Write-Host "PREFLIGHT FAILED"
    exit 1
}
Write-Host "PREFLIGHT OK"
