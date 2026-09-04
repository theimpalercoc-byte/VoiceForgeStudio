@echo off
title VoiceForge Runtime Setup
cd /d "%~dp0"
echo Starting VoiceForge Portable Setup...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_portable_runtime.ps1"
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [ERROR] Setup encountered a problem. See the output above.
    pause
)
