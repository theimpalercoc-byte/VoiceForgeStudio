@echo off
title VoiceForge Master Studio (Console)
cd /d "%~dp0"

echo ========================================================
echo   Starting VoiceForge Studio on http://localhost:8080
echo ========================================================
echo.

:: 1. Clear any stale process on port 8080
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":8080" ^| findstr "LISTENING"') do (
    echo [INFO] Freeing port 8080 from existing process (PID %%a)...
    taskkill /f /pid %%a >nul 2>&1
)

:: 2. Check runtime exists
if not exist runtime\python.exe (
    echo [SETUP] Portable runtime not found. Running setup.bat...
    call setup.bat
    if not exist runtime\python.exe (
        echo [ERROR] Setup failed to create runtime\python.exe.
        pause
        exit /b 1
    )
)

:: 3. Configure environment
set "PATH=%~dp0runtime;%~dp0runtime\Scripts;%PATH%"
set "PYTHONPATH=%~dp0;%PYTHONPATH%"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"

:: 4. Launch browser interface
start "" http://localhost:8080

:: 5. Launch Python backend
echo Starting backend server...
runtime\python.exe app.py

:: 6. If Python ever exits or crashes, keep window open so you can see the error
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo ========================================================
    echo  [CRASH DETECTED] Server exited with error code %ERRORLEVEL%.
    echo ========================================================
)
echo.
echo VoiceForge server has stopped.
pause
