# Запуск backend на Windows (аналог `uvicorn backend.main:app` из README).
#   .\scripts\run.ps1              — http://localhost:8000
#   .\scripts\run.ps1 -Port 8080   — другой порт
param(
    [int]$Port = 8000,
    [string]$HostName = "127.0.0.1"
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

if (-not (Test-Path $python)) {
    throw "Нет .venv. Сначала выполните .\scripts\setup.ps1 (или ./scripts/setup.sh в WSL/Git Bash)."
}
if (-not (Test-Path (Join-Path $root ".env"))) {
    Write-Host "→ .env нет, создаю из .env.example"
    Copy-Item (Join-Path $root ".env.example") (Join-Path $root ".env")
}

Set-Location $root
Write-Host "→ http://localhost:$Port"
& $python -m uvicorn backend.main:app --host $HostName --port $Port
