# Shared helpers for the build scripts: locate a C# compiler and the .NET
# Framework reference assemblies, on a developer machine or a CI runner.

function Resolve-Csc {
    param([string]$Explicit)
    if ($Explicit) {
        if (-not (Test-Path $Explicit)) { throw "csc not found: $Explicit" }
        return $Explicit
    }
    $candidates = @()
    # A GitHub Actions windows runner has Visual Studio, not just BuildTools.
    foreach ($edition in 'Enterprise', 'Professional', 'Community', 'BuildTools') {
        foreach ($pf in 'C:\Program Files\Microsoft Visual Studio\2022',
                        'C:\Program Files (x86)\Microsoft Visual Studio\2022') {
            $candidates += Join-Path $pf "$edition\MSBuild\Current\Bin\Roslyn\csc.exe"
        }
    }
    # Last resort: the in-box .NET Framework compiler. It is an older Roslyn,
    # hence /langversion:7.3 everywhere rather than anything newer.
    $candidates += 'C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe'
    $found = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $found) { throw 'No C# compiler found. Pass -Csc <path to csc.exe>.' }
    return $found
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
    # actually present on this machine.
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
build/lib/net472 directory of the NuGet package
Microsoft.NETFramework.ReferenceAssemblies.net472.
'@
}
