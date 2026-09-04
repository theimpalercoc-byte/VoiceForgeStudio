@echo off
title VoiceForge Studio - Portable Runtime Setup
cd /d "%~dp0"

echo ========================================================
echo   VoiceForge Studio - Automated Portable Setup
echo ========================================================
echo.

if not exist runtime mkdir runtime

if not exist runtime\python.exe (
    echo [1/4] Downloading embedded Python 3.11 with native curl...
    curl.exe -L --ssl-no-revoke -o python_embed.zip https://www.python.org/ftp/python/3.11.9/python-3.11.9-embed-amd64.zip
    if not exist python_embed.zip (
        echo [ERROR] Failed to download Python. Check your internet connection.
        pause
        exit /b 1
    )

    echo [2/4] Extracting Python runtime with native tar...
    tar.exe -xf python_embed.zip -C runtime
    del /f /q python_embed.zip

    :: Patch python311._pth to enable pip & site-packages
    if exist runtime\python311._pth (
        echo import site>> runtime\python311._pth
        echo .>> runtime\python311._pth
        echo Lib>> runtime\python311._pth
        echo Lib\site-packages>> runtime\python311._pth
    )

    echo [3/4] Bootstrapping pip...
    curl.exe -L --ssl-no-revoke -o runtime\get-pip.py https://bootstrap.pypa.io/get-pip.py
    runtime\python.exe runtime\get-pip.py --no-warn-script-location
    del /f /q runtime\get-pip.py
) else (
    echo [INFO] Embedded Python runtime already present.
)

echo.
echo [4/4] Installing PyTorch (CUDA 12.1) and VoiceForge dependencies...
echo (This will take 2-3 minutes to download and configure)
echo.

runtime\python.exe -m pip install --upgrade pip setuptools wheel --no-warn-script-location

echo Installing PyTorch with CUDA 12.1 acceleration...
runtime\python.exe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location

echo Installing VoiceForge Core Packages...
runtime\python.exe -m pip install fastapi "uvicorn[standard]" soundfile "numpy<2.0.0" scipy playwright huggingface_hub "kokoro>=0.3.4" websockets audioread "ml-dtypes==0.5.1" --no-warn-script-location

echo Installing Voice Cloning Engine...
runtime\python.exe -m pip install chatterbox-tts --no-deps --no-warn-script-location
runtime\python.exe -m pip install conformer diffusers omegaconf "librosa<1.0.0" s3tokenizer pyloudnorm pykakasi --no-warn-script-location

echo Installing Isolated Playwright Chromium...
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0runtime\playwright-browsers"
runtime\python.exe -m playwright install chromium

echo.
echo ========================================================
echo   🎉 VoiceForge Studio Setup Completed Successfully!
echo ========================================================
timeout /t 3 >nul
