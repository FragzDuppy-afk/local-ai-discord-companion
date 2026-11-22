@echo off
REM ============================================================================
REM Toggle_AI_Bot.bat — Smart Start/Stop toggle for AI Bot, Offloader, & Overseer
REM ============================================================================

cd /d "%~dp0"

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
    echo [TOGGLE] AI Bot stopped cleanly.
    timeout /t 2 /nobreak >nul
    exit /b 0
) else (
    echo [TOGGLE] Starting AI Bot, Offloader, and Overseer silently...
    start "" pythonw.exe "offloader.py"
    timeout /t 1 /nobreak >nul
    start "" pythonw.exe "overseer.py"
    timeout /t 1 /nobreak >nul
    start "" pythonw.exe "ai_bot.py"
    echo [TOGGLE] AI Bot suite launched.
    timeout /t 2 /nobreak >nul
    exit /b 0
)
