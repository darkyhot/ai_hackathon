# Установка на Windows (аналог ./scripts/setup.sh): .venv, пакеты, .env из шаблона.
#   .\scripts\setup.ps1                  — модель распознавания речи small
#   .\scripts\setup.ps1 -WhisperModel base
#   .\scripts\setup.ps1 -SkipWhisper     — без модели, голос распознаёт браузер
#   .\scripts\setup.ps1 -PythonExe C:\Python312\python.exe  — если Python не в PATH
param(
    [switch]$SkipWhisper,
    [string]$WhisperModel = "small",
    [string]$PythonExe = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

function Resolve-Python {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }
    foreach ($candidate in @("py", "python", "python3")) {
        $cmd = Get-Command $candidate -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        # Отсекаем заглушку Microsoft Store: она открывает Store вместо установки
        if ($cmd.Source -like "*\WindowsApps\python*.exe") { continue }
        if ($candidate -eq "py") { return @("py", "-3") }
        return @($cmd.Source)
    }
    throw "Python 3.10+ не найден. Установите с https://www.python.org/downloads/ и повторите."
}

$base = Resolve-Python $PythonExe
$baseExe = $base[0]
$baseArgs = @()
if ($base.Count -gt 1) { $baseArgs = $base[1..($base.Count - 1)] }

if (-not (Test-Path ".venv")) {
    Write-Host "→ Создаю виртуальное окружение .venv"
    & $baseExe @baseArgs -m venv .venv
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
Write-Host "→ Ставлю пакеты из requirements.txt"
& $venvPython -m ensurepip --upgrade --default-pip 2>$null | Out-Null
& $venvPython -m pip install --upgrade pip -q
& $venvPython -m pip install -q -r requirements.txt

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "→ Создал .env из .env.example — впишите DEEPSEEK_API_KEY или GIGACHAT_CREDENTIALS"
}

if (-not $SkipWhisper) {
    Write-Host "→ Скачиваю модель распознавания речи $WhisperModel"
    & $venvPython -m backend.stt download $WhisperModel
}

Write-Host ""
Write-Host "Готово. Запуск: .\scripts\run.ps1"
