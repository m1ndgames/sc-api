# Verify a built ScApi.SimHub.dll.
#
# Two modes, because they answer different questions:
#
#   -RefDir <dir>   resolve against the reference facades (what CI has). Checks
#                   the plugin type, its SimHub attributes and the interfaces
#                   it implements are really present in the output.
#
#   (default)       resolve against the local SimHub installation -- the REAL
#                   assemblies. This additionally JIT-prepares Init/End/
#                   DataUpdate, which forces the CLR to resolve every member
#                   reference in those method bodies, including the extension
#                   method calls into SimHub.Plugins, WITHOUT running them.
#                   A plugin compiled against the facades that fails here would
#                   have thrown MissingMethodException inside SimHub.
#
# Usage:
#   scripts\verify_simhub_plugin.ps1                           # real SimHub
#   scripts\verify_simhub_plugin.ps1 -RefDir build\refs        # facades
#   scripts\verify_simhub_plugin.ps1 -Dll build\ci\ScApi.SimHub.dll

param(
    [string]$Dll = 'build\ScApi.SimHub.dll',
    [string]$RefDir,
    [string]$ExpectVersion
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$dllPath = if ([System.IO.Path]::IsPathRooted($Dll)) { $Dll } else { Join-Path $root $Dll }
if (-not (Test-Path $dllPath)) { throw "not found: $dllPath" }

if ($RefDir) {
    $probe = if ([System.IO.Path]::IsPathRooted($RefDir)) { $RefDir } else { Join-Path $root $RefDir }
    $real = $false
} else {
    $probe = @('C:\Program Files (x86)\SimHub', 'C:\Program Files\SimHub') |
                Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $probe) { throw 'SimHub not installed; pass -RefDir <reference directory>.' }
    $real = $true
}
if (-not (Test-Path $probe)) { throw "reference directory not found: $probe" }

Write-Output ("resolving against: $probe" + $(if ($real) { ' (real SimHub assemblies)' } else { ' (reference facades)' }))

# Dependencies are loaded by hand: LoadFrom probes the host application's base
# directory, not the directory of the assembly being loaded.
$script:tried = @{}
[AppDomain]::CurrentDomain.add_AssemblyResolve({
    param($sender, $e)
    $name = ($e.Name -split ',')[0]
    if ($script:tried.ContainsKey($name)) { return $null }
    $script:tried[$name] = $true
    $candidate = Join-Path $probe ($name + '.dll')
    if (Test-Path $candidate) {
        try { return [Reflection.Assembly]::LoadFrom($candidate) } catch { return $null }
    }
    return $null
})

$plug = [Reflection.Assembly]::LoadFrom($dllPath)
Write-Output ''
Write-Output ("assembly : " + $plug.FullName)
foreach ($r in $plug.GetReferencedAssemblies() | Sort-Object Name) {
    Write-Output ("  ref    " + $r.FullName)
}

$failures = @()

$t = $plug.GetType('ScApi.Telemetry.ScApiPlugin', $false)
if (-not $t) { throw 'ScApi.Telemetry.ScApiPlugin not found in the assembly' }
Write-Output ''
Write-Output ("type     : " + $t.FullName)

$ifaces = @($t.GetInterfaces() | ForEach-Object { $_.FullName })
Write-Output ("interfaces: " + ($ifaces -join ', '))
foreach ($want in 'SimHub.Plugins.IPlugin', 'SimHub.Plugins.IDataPlugin') {
    if ($ifaces -notcontains $want) { $failures += "does not implement $want" }
}

$attrs = @($t.GetCustomAttributes($false) | ForEach-Object { $_.GetType().Name })
Write-Output ("attributes: " + ($attrs -join ', '))
foreach ($want in 'PluginNameAttribute', 'PluginAuthorAttribute', 'PluginDescriptionAttribute') {
    if ($attrs -notcontains $want) { $failures += "missing [$want]" }
}

if ($ExpectVersion) {
    $v = $plug.GetName().Version.ToString()
    Write-Output ("version  : $v (expecting $ExpectVersion)")
    if (-not $v.StartsWith($ExpectVersion)) {
        $failures += "assembly version $v does not match $ExpectVersion"
    }
}

if ($real) {
    Write-Output ''
    foreach ($name in 'Init', 'End', 'DataUpdate') {
        $m = $t.GetMethod($name)
        if (-not $m) { $failures += "missing method $name"; continue }
        try {
            [Runtime.CompilerServices.RuntimeHelpers]::PrepareMethod($m.MethodHandle)
            Write-Output ("resolved : $name against the real SimHub -- OK")
        } catch {
            Write-Output ("resolved : $name FAILED")
            $failures += "$name does not bind: $($_.Exception.GetType().Name): $($_.Exception.Message)"
        }
    }
}

Write-Output ''
if ($failures.Count -gt 0) {
    foreach ($f in $failures) { Write-Output "FAIL: $f" }
    exit 1
}
if ($real) {
    Write-Output 'VERIFIED: the plugin binds against the real SimHub assemblies.'
} else {
    Write-Output 'VERIFIED: plugin type, interfaces and attributes are present.'
}
