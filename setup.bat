@echo off
title VoiceForge Runtime Setup
cd /d "%~dp0"
echo Running VoiceForge Portable Setup...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_portable_runtime.ps1"
pause
