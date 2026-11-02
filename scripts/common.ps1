# Shared helpers for the build scripts: locate a C# compiler and the .NET
# Framework reference assemblies, on a developer machine or a CI runner.

function Get-CscCandidates {
    $found = [System.Collections.Generic.List[string]]::new()

    # Ask Visual Studio where it is rather than guessing paths. vswhere ships
    # at a fixed location with any VS 2017+ install, including Build Tools, and
    # knows about editions and years this script has never heard of.
    $vswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
    if (Test-Path $vswhere) {
        $hits = & $vswhere -all -prerelease -products * -find '**\Roslyn\csc.exe' 2>$null
        foreach ($h in $hits) { if ($h -and (Test-Path $h)) { $found.Add($h) } }
    }

    # Fallback for the same thing if vswhere is missing or found nothing.
    # Skipped when vswhere answered, because recursing a full Visual Studio
    # installation is slow enough to notice on a CI runner.
    if ($found.Count -eq 0) {
        foreach ($pf in 'C:\Program Files\Microsoft Visual Studio',
                        'C:\Program Files (x86)\Microsoft Visual Studio') {
            if (-not (Test-Path $pf)) { continue }
            Get-ChildItem -Path $pf -Recurse -Filter 'csc.exe' -ErrorAction SilentlyContinue |
                Where-Object { $_.DirectoryName -like '*\Roslyn' } |
                ForEach-Object { $found.Add($_.FullName) }
        }
    }

    # The .NET SDK carries its own Roslyn, which compiles .NET Framework
    # targets fine given reference assemblies and /nostdlib+. It ships as a
    # managed csc.dll rather than an executable, so it is run through
    # `dotnet exec` -- see Invoke-Csc.
    $dotnet = Get-Command dotnet -ErrorAction SilentlyContinue
    if ($dotnet) {
        $sdkRoot = Join-Path (Split-Path -Parent $dotnet.Source) 'sdk'
        if (Test-Path $sdkRoot) {
            Get-ChildItem -Path $sdkRoot -Directory -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending | ForEach-Object {
                    $c = Join-Path $_.FullName 'Roslyn\bincore\csc.dll'
                    if (Test-Path $c) { $found.Add($c) }
                }
        }
    }

    # LAST. This is the in-box .NET Framework compiler, which predates Roslyn
    # and stops at C# 5. It is listed only so the error message can say so;
    # Test-CscLanguageVersion will reject it for anything newer.
    $inbox = 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    if (Test-Path $inbox) { $found.Add($inbox) }

    return ($found | Select-Object -Unique)
}

function Invoke-Csc {
    <#
    Run the compiler Resolve-Csc picked.

    Visual Studio's Roslyn is csc.exe and runs directly. The .NET SDK's is a
    managed csc.dll and needs `dotnet exec`. Both call sites go through here so
    neither has to care which it got.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Csc,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
    if ($Csc -like '*.dll') {
        & dotnet exec $Csc @Arguments
    } else {
        & $Csc @Arguments
    }
}

function Test-CscLanguageVersion {
    <#
    Does this compiler actually support the language version we need?

    Asking is the whole point. A compiler that cannot do the job must be
    rejected here, loudly, rather than chosen and left to fail later with
    "error CS1617: Invalid option '7.3' for /langversion" -- which is what
    happened on a CI runner whose Visual Studio sat somewhere this script did
    not look, so resolution fell through to the in-box C# 5 compiler.

    `/langversion:?` makes Roslyn list what it supports and exit 0. The legacy
    compiler also exits 0 but just prints its banner, so the OUTPUT is what
    decides, not the exit code.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Csc,
        [Parameter(Mandatory = $true)][string]$Version
    )
    try {
        $out = Invoke-Csc -Csc $Csc -Arguments @('/langversion:?', '/nologo') 2>&1 | Out-String
    } catch {
        return $false
    }
    # Match the version as a whole line, so "5" cannot satisfy a request for
    # "5.0" and "7" cannot satisfy "7.3".
    return [bool]([regex]::IsMatch($out, "(?m)^\s*$([regex]::Escape($Version))\s*$"))
}

function Resolve-Csc {
    param(
        [string]$Explicit,
        [string]$LanguageVersion = '7.3'
    )
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "csc not found: $Explicit" }
        if (-not (Test-CscLanguageVersion -Csc $Explicit -Version $LanguageVersion)) {
            throw "$Explicit does not support C# $LanguageVersion."
        }
        return $Explicit
    }

    $candidates = @(Get-CscCandidates)
    if ($candidates.Count -eq 0) {
        throw 'No C# compiler found at all. Pass -Csc <path to csc.exe>.'
    }

    # Write-Host, not Write-Output: this function RETURNS the chosen path, and
    # anything written to the output stream would be concatenated into that
    # return value.
    Write-Host "  compilers found: $($candidates.Count)"
    foreach ($c in $candidates) { Write-Host "    $c" }

    foreach ($c in $candidates) {
        if (Test-CscLanguageVersion -Csc $c -Version $LanguageVersion) { return $c }
        Write-Host "  (skipping $c -- no C# $LanguageVersion support)"
    }

    throw @"
Found $($candidates.Count) C# compiler(s), but none supports C# ${LanguageVersion}:
$($candidates -join "`n")

The in-box .NET Framework compiler stops at C# 5, so Roslyn is required:
install Visual Studio Build Tools (or the .NET SDK), or pass -Csc explicitly.
"@
}

function Resolve-FrameworkDir {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "framework reference assemblies not found: $Explicit" }
        return (Resolve-Path $Explicit).Path
    }
    # Targets .NET Framework 4.7.2: SimHub's own demo csproj claims v4.8 while
    # the project is really 4.7.2, and both run on CLR 4.0, so a 4.7.2 assembly
    # loads fine in SimHub -- and 4.7.2 reference assemblies are the ones
    # actually present on a typical dev machine.
    $roots = @(
        'C:\Program Files (x86)\Reference Assemblies\Microsoft\Framework\.NETFramework',
        'C:\Program Files\Reference Assemblies\Microsoft\Framework\.NETFramework'
    )
    foreach ($r in $roots) {
        if (-not (Test-Path $r)) { continue }
        $pref = Join-Path $r 'v4.7.2'
        if (Test-Path $pref) { return (Resolve-Path $pref).Path }
        $any = Get-ChildItem $r -Directory -ErrorAction SilentlyContinue |
                 Sort-Object Name | Select-Object -Last 1
        if ($any) { return $any.FullName }
    }
    throw @'
No .NET Framework reference assemblies found. Pass -FrameworkDir, e.g. the
build/.NETFramework/v4.7.2 directory of the NuGet package
Microsoft.NETFramework.ReferenceAssemblies.net472.
'@
}
