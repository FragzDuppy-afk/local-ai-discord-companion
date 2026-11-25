@echo off
REM ============================================================================
REM Toggle_AI_Bot.bat — Smart Start/Stop toggle for AI Bot, Offloader, & Overseer
REM ============================================================================

cd /d "%~dp0"

REM Use local .venv Python if present; otherwise fall back to system pythonw.exe
set "PYW_EXE=pythonw.exe"
if exist ".venv\Scripts\pythonw.exe" set "PYW_EXE=.venv\Scripts\pythonw.exe"

REM Check if any of the three scripts are currently running
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$targets = @('ai_bot.py', 'offloader.py', 'overseer.py'); " ^
  "$running = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$') -and ($_.CommandLine -match ($targets -join '|')) }; " ^
  "if ($running) { exit 0 } else { exit 1 }"

if %errorlevel%==0 (
    echo [TOGGLE] Active processes detected. Shutting down AI Bot suite...
    powershell -NoProfile -ExecutionPolicy Bypass -Command ^
      "$targets = @('ai_bot.py', 'offloader.py', 'overseer.py'); " ^
      "Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^pythonw?\.exe$') -and ($_.CommandLine -match ($targets -join '|')) } | ForEach-Object { " ^
      "  Write-Host ('Stopping PID ' + $_.ProcessId + ': ' + $_.CommandLine); " ^
      "  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue " ^
      "}"
    if exist "offloader_state.json" del /f /q "offloader_state.json"
    if exist "lms_cli.lock" del /f /q "lms_cli.lock"
    echo [TOGGLE] AI Bot stopped cleanly.
    timeout /t 2 /nobreak >nul
    exit /b 0
) else (
    echo [TOGGLE] Starting AI Bot, Offloader, and Overseer silently...
    start "" "%PYW_EXE%" "offloader.py"
    timeout /t 1 /nobreak >nul
    start "" "%PYW_EXE%" "overseer.py"
    timeout /t 1 /nobreak >nul
    start "" "%PYW_EXE%" "ai_bot.py"
    echo [TOGGLE] AI Bot suite launched.
    timeout /t 2 /nobreak >nul
    exit /b 0
)
