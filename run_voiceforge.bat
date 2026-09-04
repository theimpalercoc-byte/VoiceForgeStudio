@echo off
title VoiceForge Master Studio
cd /d "%~dp0"

if not exist runtime\python.exe (
    call setup.bat
    if not exist runtime\python.exe (
        echo [ERROR] Setup did not complete.
        pause
        exit /b 1
    )
)

set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

start "" http://localhost:8080
runtime\python.exe app.py
pause
