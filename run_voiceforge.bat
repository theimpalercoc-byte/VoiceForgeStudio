@echo off
title VoiceForge Master Studio
cd /d "%~dp0"

if exist runtime\python.exe (
    set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
    set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"
    start "" http://localhost:8080
    runtime\python.exe app.py
) else (
    echo [ERROR] Portable runtime not found!
    echo Right-click 'setup_portable_runtime.ps1' and select 'Run with PowerShell' first.
    pause
)
