$ErrorActionPreference = "Stop"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $ScriptDir

Write-Host "Compiling Native VoiceForgeStudio.exe with MSVC..." -ForegroundColor Cyan

if (Get-Command cmake -ErrorAction SilentlyContinue) {
    cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
    cmake --build build --config Release
    Copy-Item "build\Release\VoiceForgeStudio.exe" -Destination ".\VoiceForgeStudio.exe" -Force
    Write-Host "✓ Compilation successful via CMake." -ForegroundColor Green
} else {
    rc /fo app.res app.rc
    cl /std:c++20 /O2 /MT /EHsc main.cpp app.res /Fe:VoiceForgeStudio.exe /link /SUBSYSTEM:WINDOWS /MANIFEST:EMBED /MANIFESTINPUT:app.manifest Shell32.lib User32.lib Kernel32.lib Ws2_32.lib
    Remove-Item *.obj, app.res -ErrorAction SilentlyContinue
    Write-Host "✓ Compilation successful via MSVC cl.exe." -ForegroundColor Green
}
