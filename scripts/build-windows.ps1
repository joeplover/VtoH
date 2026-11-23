param([switch]$Portable)

$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot)
New-Item -ItemType Directory -Force .tools/bin, dist/windows | Out-Null
& g++ -std=c++17 -O2 -static native/windows/capture.cpp -o .tools/bin/vtoh_capture.exe -lole32 -lmmdevapi -luuid -lksguid
if ($LASTEXITCODE -ne 0) { throw 'WASAPI build failed.' }
if ($Portable) {
    & python -m PyInstaller --noconfirm --onefile --windowed --name VtoH --workpath .tools/pyinstaller --specpath .tools --distpath dist/windows desktop.py
} else {
    Push-Location client
    try {
        & flutter build windows --release
        if ($LASTEXITCODE -eq 0) {
            Copy-Item -Path build/windows/x64/runner/Release/* -Destination ../dist/windows -Recurse -Force
        }
    } finally {
        Pop-Location
    }
}
if ($LASTEXITCODE -ne 0) { throw 'Windows client build failed.' }
Copy-Item .tools/bin/vtoh_capture.exe dist/windows/
