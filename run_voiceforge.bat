@echo off
title VoiceForge Master Studio
cd /d "%~dp0"

echo ========================================================
echo   Starting VoiceForge Studio (http://localhost:8080)
echo ========================================================
echo.

set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

start "" http://localhost:8080

runtime\python.exe app.py
if errorlevel 1 (
    echo.
    echo ========================================================
    echo  [CRASH DETECTED] Server exited with an error.
    echo ========================================================
    echo.
    pause
)
pause
