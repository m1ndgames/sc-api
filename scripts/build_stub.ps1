# Build native\dbxlive_stub.cpp into build\dbxLive64.dll
#
# Invokes cl.exe with an explicit environment rather than going through
# vcvars64.bat: on this machine the vcvars batch file hangs indefinitely when
# launched from a non-interactive shell, while calling the compiler directly is
# immediate and deterministic.
#
# Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_stub.ps1

$ErrorActionPreference = 'Stop'

function Find-Newest($base) {
    if (-not (Test-Path $base)) { return $null }
    Get-ChildItem $base -Directory |
        Sort-Object { [version]($_.Name -replace '[^0-9.]', '') } |
        Select-Object -Last 1
}

$vsRoots = @(
    'C:\Program Files\Microsoft Visual Studio\2022',
    'C:\Program Files (x86)\Microsoft Visual Studio\2022'
)

$vc = $null
foreach ($root in $vsRoots) {
    if (-not (Test-Path $root)) { continue }
    foreach ($ed in Get-ChildItem $root -Directory) {
        $cand = Join-Path $ed.FullName 'VC\Tools\MSVC'
        $newest = Find-Newest $cand
        if ($newest) { $vc = $newest.FullName; break }
    }
    if ($vc) { break }
}
if (-not $vc) { throw 'No MSVC toolset found under Visual Studio 2022.' }

$sdk = 'C:\Program Files (x86)\Windows Kits\10'
$sdkVer = (Find-Newest (Join-Path $sdk 'Include')).Name
if (-not $sdkVer) { throw "No Windows SDK found under $sdk\Include." }

Write-Output "MSVC toolset : $vc"
Write-Output "Windows SDK  : $sdkVer"

$env:INCLUDE = @(
    "$vc\include",
    "$sdk\Include\$sdkVer\ucrt",
    "$sdk\Include\$sdkVer\shared",
    "$sdk\Include\$sdkVer\um",
    "$sdk\Include\$sdkVer\winrt"
) -join ';'
$env:LIB = @(
    "$vc\lib\x64",
    "$sdk\Lib\$sdkVer\ucrt\x64",
    "$sdk\Lib\$sdkVer\um\x64"
) -join ';'
$env:PATH = "$vc\bin\Hostx64\x64;$env:PATH"

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
New-Item -ItemType Directory -Force (Join-Path $root 'build') | Out-Null

# /MT so the stub carries no CRT DLL dependency -- it is loaded into a game
# process we do not control and must not rely on a redistributable being present.
& cl.exe /nologo /LD /O2 /MT /std:c++17 /W3 /EHsc `
    native\dbxlive_stub.cpp `
    /Fe:build\dbxLive64.dll /Fo:build\ /Fd:build\ `
    /link /DLL user32.lib

if ($LASTEXITCODE -ne 0) { throw "cl.exe failed with exit code $LASTEXITCODE" }

$dll = Get-Item (Join-Path $root 'build\dbxLive64.dll')
Write-Output ''
Write-Output ("built {0} ({1:N0} bytes)" -f $dll.FullName, $dll.Length)
