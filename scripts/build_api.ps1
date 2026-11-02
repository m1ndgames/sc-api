# Build the sc-api server into a single build\dist\sc-api.exe, so the API can
# be run on a machine with no Python installed. CI runs the same spec; this is
# here so a local build matches it.
#
# Usage:
#   scripts\build_api.ps1
#   scripts\build_api.ps1 -Python .venv\Scripts\python.exe -Verify

param(
    [string]$Python = '.venv\Scripts\python.exe',
    [switch]$Verify
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path $Python)) {
        Write-Output "no interpreter at $Python; falling back to 'python' on PATH"
        $Python = 'python'
    }

    & $Python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Output 'installing pyinstaller'
        & $Python -m pip install --quiet "pyinstaller==6.*"
        if ($LASTEXITCODE -ne 0) { throw 'could not install pyinstaller' }
    }

    & $Python -m PyInstaller --clean --noconfirm `
        --distpath build/dist --workpath build/pyi packaging/sc-api.spec
    if ($LASTEXITCODE -ne 0) { throw "pyinstaller failed ($LASTEXITCODE)" }

    $exe = Get-Item 'build\dist\sc-api.exe'
    Write-Output ''
    Write-Output ("built {0} ({1:N0} bytes)" -f $exe.FullName, $exe.Length)

    if ($Verify) {
        & $exe.FullName --version
        if ($LASTEXITCODE -ne 0) { throw 'sc-api.exe --version failed' }
        # Drive the frozen server through the whole LiveMotion lifecycle. The
        # frozen build has failure modes the source does not: the protobuf
        # stubs import each other by bare module name, and grpc ships a native
        # extension.
        $proc = Start-Process $exe.FullName -ArgumentList '--plaintext', '--port', '51999' -PassThru
        try {
            Start-Sleep -Seconds 5
            & $Python tools\selftest.py --target 127.0.0.1:51999 --plaintext
            if ($LASTEXITCODE -ne 0) { throw "frozen selftest failed ($LASTEXITCODE)" }
        } finally {
            if (-not $proc.HasExited) { Stop-Process -Id $proc.Id -Force }
        }
        Write-Output ''
        Write-Output 'VERIFIED: the executable serves the LiveMotion protocol.'
    }
}
finally { Pop-Location }
