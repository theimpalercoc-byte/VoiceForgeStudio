$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

$PythonVer = "3.11.9"
$PythonZipName = "python-$PythonVer-embed-amd64.zip"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVer/$PythonZipName"
$RuntimeDir = Join-Path $ScriptDir "runtime"

Write-Host "Provisioning Embedded Python ($PythonVer) + CUDA Runtime..." -ForegroundColor Cyan

if (-not (Test-Path $RuntimeDir)) {
    New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
}

$PythonExe = Join-Path $RuntimeDir "python.exe"
if (-not (Test-Path $PythonExe)) {
    $ZipDest = Join-Path $RuntimeDir $PythonZipName
    Write-Host "Downloading Python $PythonVer Embeddable..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $PythonUrl -OutFile $ZipDest -UseBasicParsing

    Expand-Archive -Path $ZipDest -DestinationPath $RuntimeDir -Force
    Remove-Item $ZipDest -Force

    $PthFile = Get-ChildItem -Path $RuntimeDir -Filter "*._pth" | Select-Object -First 1
    if ($PthFile) {
        $Content = Get-Content $PthFile.FullName
        $NewContent = @()
        foreach ($Line in $Content) {
            if ($Line -match "^#import site") { $NewContent += "import site" } else { $NewContent += $Line }
        }
        $NewContent += "."
        $NewContent += "Lib"
        $NewContent += "Lib\site-packages"
        $NewContent | Set-Content $PthFile.FullName -Encoding Ascii
    }

    $GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
    $GetPipPy = Join-Path $RuntimeDir "get-pip.py"
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPy -UseBasicParsing
    & $PythonExe $GetPipPy --no-warn-script-location
    Remove-Item $GetPipPy -Force
}

Write-Host "Installing PyTorch (CUDA 12.1) & Audio Dependencies..." -ForegroundColor Yellow
& $PythonExe -m pip install --upgrade pip setuptools wheel --no-warn-script-location
& $PythonExe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location

$Packages = @(
    "fastapi>=0.111.0", "uvicorn[standard]>=0.30.0", "soundfile>=0.12.1",
    "numpy<2.0.0", "scipy", "playwright>=1.44.0", "huggingface_hub",
    "kokoro>=0.3.4", "websockets", "audioread", "ml-dtypes==0.5.1"
)
& $PythonExe -m pip install $Packages --no-warn-script-location

& $PythonExe -m pip install chatterbox-tts --no-deps --no-warn-script-location
& $PythonExe -m pip install conformer diffusers omegaconf "librosa<1.0.0" s3tokenizer pyloudnorm pykakasi --no-warn-script-location

# Pre-install isolated Chromium browser
$BrowsersDir = Join-Path $RuntimeDir "playwright-browsers"
$env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
& $PythonExe -m playwright install chromium

# Automated Model Caching for standalone bundle
Write-Host "Pre-caching Kokoro & Chatterbox-Turbo models into portable bundle..." -ForegroundColor Yellow
$PreloadScript = @"
from pathlib import Path
from huggingface_hub import snapshot_download

# Cache Chatterbox-Turbo locally
cb_dir = Path('pretrained_models/chatterbox-turbo')
cb_dir.mkdir(parents=True, exist_ok=True)
snapshot_download(repo_id='ResembleAI/chatterbox-turbo', local_dir=str(cb_dir), token=False)

# Cache Kokoro weights
try:
    from kokoro import KPipeline
    KPipeline(lang_code='a', device='cpu')
except Exception:
    pass
print('✓ Base models pre-cached.')
"@
& $PythonExe -c $PreloadScript

Write-Host "✓ Portable Runtime Ready!" -ForegroundColor Green
