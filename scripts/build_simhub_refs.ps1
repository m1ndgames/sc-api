# Build the SimHub reference facades into a directory usable as -RefDir for
# scripts\build_simhub_plugin.ps1.
#
# This exists for CI: SimHub is proprietary and is not installed on a runner,
# so the plugin is compiled against the signature-only facades in
# simhub\refs\. See simhub\refs\README.md for why that is sound.
#
# log4net and Newtonsoft.Json are NOT faked -- both are strong-named, so the
# real signed assemblies are required. Pass them in (NuGet has both).
#
# Usage:
#   scripts\build_simhub_refs.ps1 -OutDir build\refs `
#       -Log4Net <path>\log4net.dll -Newtonsoft <path>\Newtonsoft.Json.dll

param(
    [string]$OutDir = 'build\refs',
    [string]$FrameworkDir,
    [string]$Csc,
    [Parameter(Mandatory = $true)][string]$Log4Net,
    [Parameter(Mandatory = $true)][string]$Newtonsoft
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'common.ps1')

$LangVersion = '7.3'
$csc = Resolve-Csc -Explicit $Csc -LanguageVersion $LangVersion
$fw = Resolve-FrameworkDir $FrameworkDir

if (-not (Test-Path $Log4Net)) { throw "log4net not found: $Log4Net" }
if (-not (Test-Path $Newtonsoft)) { throw "Newtonsoft.Json not found: $Newtonsoft" }

$outFull = if ([System.IO.Path]::IsPathRooted($OutDir)) { $OutDir } else { Join-Path $root $OutDir }
New-Item -ItemType Directory -Force $outFull | Out-Null

Write-Output "compiler : $csc"
Write-Output "framework: $fw"
Write-Output "output   : $outFull"

# The strong-named third-party assemblies are copied in, so -RefDir is a single
# self-contained reference directory.
Copy-Item $Log4Net (Join-Path $outFull 'log4net.dll') -Force
Copy-Item $Newtonsoft (Join-Path $outFull 'Newtonsoft.Json.dll') -Force

$core = @('mscorlib.dll', 'System.dll', 'System.Core.dll') |
    ForEach-Object { Join-Path $fw $_ } | Where-Object { Test-Path $_ }

function Build-Facade {
    param([string]$Source, [string]$Name, [string[]]$Refs)
    $out = Join-Path $outFull $Name
    # NOT quoted: each element is one argument already, and PowerShell 7
    # passes embedded quotes through to the process literally (unlike 5.1),
    # which turns a perfectly good path into one csc cannot open.
    $refArgs = ($core + $Refs) | ForEach-Object { "/reference:$_" }
    # /nostdlib+ so mscorlib comes from the reference assemblies, not from the
    # runtime csc happens to be running on.
    $cscArgs = @('/nologo', '/target:library', '/nostdlib+', "/langversion:$LangVersion",
                 "/out:$out") + $refArgs + @((Join-Path $root $Source))
    Invoke-Csc -Csc $csc -Arguments $cscArgs
    if ($LASTEXITCODE -ne 0) { throw "csc failed building $Name (exit $LASTEXITCODE)" }
    Write-Output ("  facade {0,-24} {1,8:N0} bytes" -f $Name, (Get-Item $out).Length)
}

# Order matters: SimHub.Plugins references GameReaderCommon.
Build-Facade 'simhub\refs\GameReaderCommon.cs' 'GameReaderCommon.dll' @()
Build-Facade 'simhub\refs\SimHub.Logging.cs'   'SimHub.Logging.dll' `
    @((Join-Path $outFull 'log4net.dll'))
Build-Facade 'simhub\refs\SimHub.Plugins.cs'   'SimHub.Plugins.dll' `
    @((Join-Path $outFull 'GameReaderCommon.dll'))

Write-Output ''
Write-Output "reference directory ready: $outFull"
