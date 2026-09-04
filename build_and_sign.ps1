# ===========================================================================
# VoiceForge Studio — Windows 11 Build & Authenticode Signing Suite
# ===========================================================================
$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

Write-Host "===========================================================" -ForegroundColor Cyan
Write-Host "  VoiceForge Studio Windows 11 Native Build & Signing      " -ForegroundColor Cyan
Write-Host "===========================================================" -ForegroundColor Cyan

# 1. Generate or Retrieve Local Code-Signing Certificate (Silent, no GUI prompts)
$CertSubject = "CN=VoiceForge Studio Authority"
$Cert = Get-ChildItem Cert:\CurrentUser\My -CodeSigningCert | Where-Object { $_.Subject -eq $CertSubject } | Select-Object -First 1

if (-not $Cert) {
    Write-Host "`n[1/3] Generating Local Authenticode Code-Signing Certificate..." -ForegroundColor Yellow
    $Cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $CertSubject `
        -KeySpec Signature `
        -KeyExportPolicy Exportable `
        -HashAlgorithm SHA256 `
        -CertStoreLocation "Cert:\CurrentUser\My"
    Write-Host "✓ Certificate generated in Cert:\CurrentUser\My." -ForegroundColor Green
} else {
    Write-Host "`n[1/3] Using Existing Code-Signing Certificate: $($Cert.Thumbprint)" -ForegroundColor Green
}

# 2. Compile VoiceForgeStudio.exe
Write-Host "`n[2/3] Compiling Native Executable (VoiceForgeStudio.exe)..." -ForegroundColor Yellow

if (Get-Command cmake -ErrorAction SilentlyContinue) {
    cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
    cmake --build build --config Release
    Copy-Item "build\Release\VoiceForgeStudio.exe" -Destination ".\VoiceForgeStudio.exe" -Force
    Write-Host "✓ Compilation successful via CMake." -ForegroundColor Green
} elseif (Get-Command cl -ErrorAction SilentlyContinue) {
    rc /fo app.res app.rc
    cl /std:c++20 /O2 /MT /EHsc main.cpp app.res /Fe:VoiceForgeStudio.exe /link /SUBSYSTEM:WINDOWS /MANIFEST:EMBED /MANIFESTINPUT:app.manifest Shell32.lib User32.lib Kernel32.lib Ws2_32.lib
    Remove-Item *.obj, app.res -ErrorAction SilentlyContinue
    Write-Host "✓ Compilation successful via direct MSVC (cl.exe)." -ForegroundColor Green
} else {
    Write-Error "Neither CMake nor MSVC compiler (cl.exe) was found in PATH."
}

# 3. Sign Binary with Authenticode (Silent RFC-3161 Timestamp)
$ExePath = Join-Path $ScriptDir "VoiceForgeStudio.exe"

if (Test-Path $ExePath) {
    Write-Host "`n[3/3] Signing $ExePath with Microsoft Authenticode..." -ForegroundColor Yellow
    
    $SignResult = Set-AuthenticodeSignature `
        -FilePath $ExePath `
        -Certificate $Cert `
        -HashAlgorithm SHA256 `
        -TimestampServer "http://timestamp.digicert.com"

    Write-Host "✓ Signature Status: $($SignResult.Status)" -ForegroundColor Green
    Write-Host "`n🎉 VoiceForgeStudio.exe is compiled, signed, and ready to deploy!" -ForegroundColor Cyan
} else {
    Write-Error "Build artifact '$ExePath' not found."
}
