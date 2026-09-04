@echo off
title VoiceForge Master Studio
cd /d "%~dp0"

:: Check if runtime exists, if not, automatically run setup with bypass
if not exist runtime\python.exe (
    echo ========================================================
    echo  VoiceForge Portable Runtime Not Detected
    echo  Starting automatic one-time setup...
    echo ========================================================
    echo.
    powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_portable_runtime.ps1"
    
    if not exist runtime\python.exe (
        echo.
        echo [ERROR] Setup did not complete successfully.
        pause
        exit /b 1
    )
    echo.
    echo ========================================================
    echo  Setup complete! Starting VoiceForge Studio...
    echo ========================================================
    echo.
)

set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

start "" http://localhost:8080
runtime\python.exe app.py
pause
