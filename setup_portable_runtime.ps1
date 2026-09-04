# Suppress slow PowerShell 5.1 progress GUI to speed up downloads 10x
$ProgressPreference = 'SilentlyContinue'
$ErrorActionPreference = 'Stop'

try {
    # Enforce standard TLS 1.2 (Universally compatible with Windows PowerShell 5.1 and PS 7)
    [System.Net.ServicePointManager]::SecurityProtocol = [System.Net.SecurityProtocolType]::Tls12

    $ScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Definition }
    if (-not $ScriptDir) { $ScriptDir = (Get-Location).Path }
    Set-Location $ScriptDir

    Write-Host "===========================================================" -ForegroundColor Cyan
    Write-Host "  VoiceForge Studio — Portable Runtime & Library Setup     " -ForegroundColor Cyan
    Write-Host "===========================================================" -ForegroundColor Cyan

    $PythonVer = "3.11.9"
    $PythonZipName = "python-$PythonVer-embed-amd64.zip"
    $PythonUrl = "https://www.python.org/ftp/python/$PythonVer/$PythonZipName"
    $RuntimeDir = Join-Path $ScriptDir "runtime"

    if (-not (Test-Path $RuntimeDir)) {
        New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
    }

    $PythonExe = Join-Path $RuntimeDir "python.exe"
    if (-not (Test-Path $PythonExe)) {
        $ZipDest = Join-Path $RuntimeDir $PythonZipName
        Write-Host "`n[1/5] Downloading Python $PythonVer Embeddable Package..." -ForegroundColor Yellow
        Invoke-WebRequest -Uri $PythonUrl -OutFile $ZipDest -UseBasicParsing

        Write-Host "[2/5] Extracting to $RuntimeDir..." -ForegroundColor Yellow
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

        Write-Host "[3/5] Bootstrapping pip..." -ForegroundColor Yellow
        $GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
        $GetPipPy = Join-Path $RuntimeDir "get-pip.py"
        Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPy -UseBasicParsing
        & $PythonExe $GetPipPy --no-warn-script-location
        Remove-Item $GetPipPy -Force
    } else {
        Write-Host "✓ Embedded Python runtime found at $PythonExe" -ForegroundColor Green
    }

    Write-Host "`n[4/5] Installing PyTorch (CUDA 12.1) & Audio Dependencies..." -ForegroundColor Yellow
    & $PythonExe -m pip install --upgrade pip setuptools wheel --no-warn-script-location
    & $PythonExe -m pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --no-warn-script-location

    Write-Host "`n[5/5] Installing VoiceForge Studio Packages..." -ForegroundColor Yellow
    $Packages = @(
        "fastapi>=0.111.0", "uvicorn[standard]>=0.30.0", "soundfile>=0.12.1",
        "numpy<2.0.0", "scipy", "playwright>=1.44.0", "huggingface_hub",
        "kokoro>=0.3.4", "websockets", "audioread", "ml-dtypes==0.5.1"
    )
    & $PythonExe -m pip install $Packages --no-warn-script-location

    & $PythonExe -m pip install chatterbox-tts --no-deps --no-warn-script-location
    & $PythonExe -m pip install conformer diffusers omegaconf "librosa<1.0.0" s3tokenizer pyloudnorm pykakasi --no-warn-script-location

    $BrowsersDir = Join-Path $RuntimeDir "playwright-browsers"
    $env:PLAYWRIGHT_BROWSERS_PATH = $BrowsersDir
    & $PythonExe -m playwright install chromium

    Write-Host "`n===========================================================" -ForegroundColor Green
    Write-Host "🎉 VoiceForge Studio Setup Completed Successfully!" -ForegroundColor Green
    Write-Host "===========================================================" -ForegroundColor Green
    Start-Sleep -Seconds 2

} catch {
    Write-Host "`n===========================================================" -ForegroundColor Red
    Write-Host "  SETUP ENCOUNTERED AN ERROR:                              " -ForegroundColor Red
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    Write-Host "===========================================================" -ForegroundColor Red
    Write-Host "`nPress Enter to exit..." -ForegroundColor Yellow
    Read-Host
    exit 1
}
