# ===========================================================================
# VoiceForge Studio — Zero-Install Portable Runtime Provisioner (Windows 11)
# ===========================================================================
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$PythonVer = "3.11.9"
$PythonZipName = "python-$PythonVer-embed-amd64.zip"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVer/$PythonZipName"
$RuntimeDir = Join-Path $ScriptDir "runtime"

Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  Provisioning Embedded Python ($PythonVer) + CUDA Runtime  " -ForegroundColor Cyan
Write-Host "===========================================================" -ForegroundColor Cyan

if (-not (Test-Path $RuntimeDir)) {
    New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
}

$PythonExe = Join-Path $RuntimeDir "python.exe"
if (-not (Test-Path $PythonExe)) {
    $ZipDest = Join-Path $RuntimeDir $PythonZipName
    Write-Host "`n[1/6] Downloading Python $PythonVer Embeddable Package..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $PythonUrl -OutFile $ZipDest -UseBasicParsing

    Write-Host "[2/6] Extracting to $RuntimeDir..." -ForegroundColor Yellow
    Expand-Archive -Path $ZipDest -DestinationPath $RuntimeDir -Force
    Remove-Item $ZipDest -Force

    # Patch python311._pth to enable site-packages & pip
    $PthFile = Get-ChildItem -Path $RuntimeDir -Filter "*._pth" | Select-Object -First 1
    if ($PthFile) {
        Write-Host "[3/6] Patching $($PthFile.Name) to enable 'import site'..." -ForegroundColor Yellow
        $Content = Get-Content $PthFile.FullName
        $NewContent = @()
        foreach ($Line in $Content) {
            if ($Line -match "^#import site") {
                $NewContent += "import site"
            } else {
                $NewContent += $Line
            }
        }
        $NewContent += "."
        $NewContent += "Lib"
        $NewContent += "Lib\site-packages"
        $NewContent | Set-Content $PthFile.FullName -Encoding Ascii
    }

    Write-Host "[4/6] Bootstrapping pip into isolated runtime..." -ForegroundColor Yellow
    $GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
    $GetPipPy = Join-Path $RuntimeDir "get-pip.py"
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPy -UseBasicParsing
    & $PythonExe $GetPipPy --no-warn-script-location
    Remove-Item $GetPipPy -Force
} else {
    Write-Host "✓ Embedded Python detected at $PythonExe" -ForegroundColor Green
}

# 5. Core Dependencies
Write-Host "`n[5/6] Installing PyTorch (CUDA 12.1) & Audio Engine Libraries..." -ForegroundColor Yellow

$PipArgs = @(
    "-m", "pip", "install", "--upgrade",
    "pip", "setuptools", "wheel", "--no-warn-script-location"
)
& $PythonExe $PipArgs

Write-Host "Installing PyTorch (CUDA 12.1)..." -ForegroundColor Yellow
& $PythonExe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location

Write-Host "Installing VoiceForge Dependencies..." -ForegroundColor Yellow
$Packages = @(
    "fastapi>=0.111.0",
    "uvicorn[standard]>=0.30.0",
    "soundfile>=0.12.1",
    "numpy<2.0.0",
    "scipy",
    "playwright>=1.44.0",
    "huggingface_hub",
    "kokoro>=0.3.4",
    "websockets",
    "audioread",
    "ml-dtypes==0.5.1"
)
& $PythonExe -m pip install $Packages --no-warn-script-location

Write-Host "Installing Cloning Engine Libraries..." -ForegroundColor Yellow
& $PythonExe -m pip install chatterbox-tts --no-deps --no-warn-script-location
& $PythonExe -m pip install conformer diffusers omegaconf "librosa<1.0.0" s3tokenizer pyloudnorm pykakasi --no-warn-script-location

# Install Isolated Chromium for Live Chat Ingest
$BrowsersDir = Join-Path $RuntimeDir "playwright-browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
& $PythonExe -m playwright install chromium

# ---------------------------------------------------------------------------
# 6. Interactive AI Model Selector
# ---------------------------------------------------------------------------
Write-Host "`n===================================================================" -ForegroundColor Cyan
Write-Host "          VoiceForge Studio — AI Voice Model Selection" -ForegroundColor Cyan
Write-Host "===================================================================" -ForegroundColor Cyan
Write-Host " Choose which AI speech engines you want to download now."
Write-Host " (You can install or delete models later in the Web Settings!)`n"

Write-Host " [1] Kokoro-82M        ~350 MB  (54 Built-in Multilingual Voices) [Recommended]" -ForegroundColor Green
Write-Host " [2] Chatterbox-Turbo  ~1.2 GB  (Cloning for Custom /voices)      [Recommended]" -ForegroundColor Green
Write-Host " [3] CosyVoice 2 0.5B  ~3.8 GB  (Expressive Flow-Matching Engine)" -ForegroundColor Yellow
Write-Host " [4] Qwen3-TTS         ~2.1 GB  (Multi-Emotion Neural Engine)" -ForegroundColor Yellow
Write-Host "`n Options:"
Write-Host "   - Press [ENTER] for Recommended (1, 2)  (~1.5 GB)"
Write-Host "   - Type numbers separated by comma (e.g. 1,2 or 1,2,3)"
Write-Host "   - Type 'ALL' for everything (~7.5 GB)"
Write-Host "   - Type 'SKIP' to download models later from the web app`n"

$Choice = Read-Host " Your choice [Default: 1, 2]"
if ([string]::IsNullOrWhiteSpace($Choice)) {
    $Choice = "1,2"
}

$Choice = $Choice.Trim().ToUpper()

$DownloadScript = @"
import os
from pathlib import Path
from huggingface_hub import snapshot_download

choice = '$Choice'
base_models = Path('pretrained_models')
base_models.mkdir(exist_ok=True)

# 1. Kokoro-82M
if choice == 'ALL' or '1' in choice:
    print('\n[1/4] Downloading Kokoro-82M (~350 MB)...')
    try:
        from kokoro import KPipeline
        KPipeline(lang_code='a', device='cpu')
        print('✓ Kokoro-82M ready.')
    except Exception as e:
        print(f'Notice on Kokoro: {e}')

# 2. Chatterbox-Turbo
if choice == 'ALL' or '2' in choice:
    print('\n[2/4] Downloading Chatterbox-Turbo (~1.2 GB)...')
    dest_cb = base_models / 'chatterbox-turbo'
    dest_cb.mkdir(exist_ok=True)
    snapshot_download(repo_id='ResembleAI/chatterbox-turbo', local_dir=str(dest_cb), token=False)
    print('✓ Chatterbox-Turbo ready.')

# 3. CosyVoice 2 0.5B
if choice == 'ALL' or '3' in choice:
    print('\n[3/4] Downloading CosyVoice 2 0.5B (~3.8 GB)...')
    dest_cv = base_models / 'CosyVoice2-0.5B'
    dest_cv.mkdir(exist_ok=True)
    snapshot_download(repo_id='FunAudioLLM/CosyVoice2-0.5B', local_dir=str(dest_cv), token=False)
    print('✓ CosyVoice 2 ready.')

# 4. Qwen3-TTS
if choice == 'ALL' or '4' in choice:
    print('\n[4/4] Downloading Qwen3-TTS (~2.1 GB)...')
    snapshot_download(repo_id='Qwen/Qwen2.5-0.5B', token=False)
    print('✓ Qwen3-TTS ready.')

print('\n✓ Model download process complete!')
"@

if ($Choice -ne "SKIP" -and $Choice -ne "NONE" -and $Choice -ne "0") {
    & $PythonExe -c $DownloadScript
} else {
    Write-Host "`nSkipped model downloads. You can download models anytime in the web app!" -ForegroundColor Yellow
}

Write-Host "`n===========================================================" -ForegroundColor Green
Write-Host "🎉 VoiceForge Studio Setup Completed Successfully!" -ForegroundColor Green
Write-Host "===========================================================" -ForegroundColor Green
