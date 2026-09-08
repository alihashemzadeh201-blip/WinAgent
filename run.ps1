# WinAgent launcher (PowerShell). Usage:  .\run.ps1            -> GUI
#                                        .\run.ps1 --cli      -> terminal chat
#                                        .\run.ps1 --task "open notepad and type hello"
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "[WinAgent] Creating virtual environment..."
    python -m venv .venv
    Write-Host "[WinAgent] Installing dependencies..."
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip | Out-Null
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
}
if (-not (Test-Path "config.json") -and (Test-Path "config.example.json")) {
    Copy-Item "config.example.json" "config.json"
    Write-Host "[WinAgent] config.json created - set your api_key in Settings."
}
& ".venv\Scripts\python.exe" -m winagent @args
