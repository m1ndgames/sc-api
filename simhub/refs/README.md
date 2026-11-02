# SimHub reference facades

`simhub/ScApiPlugin.cs` compiles against SimHub's own assemblies
(`SimHub.Plugins.dll`, `SimHub.Logging.dll`, `GameReaderCommon.dll`). Those are
proprietary, are not redistributable, and do not exist on a CI runner.

The files here are **reference-only facades**: the handful of SimHub types and
members the plugin actually touches, with signatures matching the real ones
exactly, and no implementation. They exist purely so the plugin can be compiled
where SimHub is not installed.

This works because SimHub's assemblies are **not strong-named**
(`PublicKeyToken=null`), so the CLR binds by simple name and ignores assembly
version. A plugin compiled against these facades therefore loads against the
real SimHub assemblies at runtime.

Recovered from SimHub 1.0.9658 by reflection:

| Facade | Real identity |
|---|---|
| `GameReaderCommon.cs` | `GameReaderCommon, Version=1.0.0.0, PublicKeyToken=null` |
| `SimHub.Logging.cs` | `SimHub.Logging, Version=1.0.0.0, PublicKeyToken=null` |
| `SimHub.Plugins.cs` | `SimHub.Plugins, Version=1.0.9658.36237, PublicKeyToken=null` |

`log4net` and `Newtonsoft.Json` are **not** faked: both are strong-named, so the
exact signed assemblies are needed, and both are freely available on NuGet
(`log4net` 2.0.15, `Newtonsoft.Json` 13.0.x).

Building locally with SimHub installed uses the **real** assemblies — these
facades are only used when `scripts/build_simhub_plugin.ps1` is pointed at a
reference directory with `-RefDir`. If a future change uses a SimHub member not
declared here, CI will fail to compile while a local build succeeds: add the
member here, copying its real signature.
