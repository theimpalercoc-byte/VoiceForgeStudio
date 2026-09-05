@echo off
title VoiceForge Master Studio
cd /d "%~dp0"

for /f "tokens=5" %%a in ('netstat -aon ^| findstr :8080 ^| findstr LISTENING 2^>nul') do (
    echo [INFO] Freeing port 8080 from stale process (PID %%a)...
    taskkill /f /pid %%a >nul 2>&1
)

if not exist runtime\python.exe (
    call setup.bat
    if not exist runtime\python.exe (
        echo [ERROR] Setup did not complete.
        pause
        exit /b 1
    )
)

set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

:: Launch in Dedicated Desktop App Window
if exist "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" (
    start "" "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --app=http://localhost:8080 --window-size=1400,900
) else if exist "C:\Program Files\Google\Chrome\Application\chrome.exe" (
    start "" "C:\Program Files\Google\Chrome\Application\chrome.exe" --app=http://localhost:8080 --window-size=1400,900
) else (
    start "" http://localhost:8080
)

runtime\python.exe app.py
