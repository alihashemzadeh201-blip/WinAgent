@echo off
REM WinAgent launcher for Windows: creates a virtual environment on first run, then starts the GUI.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py -3"
) else (
    set "PY=python"
)

if not exist ".venv\Scripts\python.exe" (
    echo [WinAgent] Creating virtual environment...
    %PY% -m venv .venv || goto :fail
    echo [WinAgent] Installing dependencies...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip >nul
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :fail
)

if not exist "config.json" (
    if exist "config.example.json" (
        copy /y "config.example.json" "config.json" >nul
        echo [WinAgent] config.json created from config.example.json - set your api_key in Settings.
    )
)

if "%~1"=="" (
    REM GUI mode: no console window
    start "" ".venv\Scripts\pythonw.exe" -m winagent
) else (
    REM CLI / task mode: keep the console
    ".venv\Scripts\python.exe" -m winagent %*
)
goto :eof

:fail
echo.
echo [WinAgent] Setup failed. Make sure Python 3.10+ is installed and on PATH (https://python.org).
pause
exit /b 1
