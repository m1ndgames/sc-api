# sc-api SimHub plugin

Publishes Star Citizen haptic telemetry into SimHub as properties and events, so
ShakeIt (or a dashboard) can drive a haptic seat pad / bass shakers.

```
Star Citizen -> dbxLive64.dll -> sc-api server -> UDP JSON -> this plugin
                                                                 |
                                         properties + events -> ShakeIt
```

## Why it is self-driven

Star Citizen is **not** a SimHub-supported game. SimHub's game readers are
`ETS2Reader`, `RfactorReader`, `CodemastersReader` and friends — there is no Star Citizen
reader, and no mention of it anywhere in the SimHub binaries. So
`IDataPlugin.DataUpdate` reports `GameRunning == false` and its normalised `GameData` stays
empty.

This plugin therefore does **not** read SimHub's game pipeline. It runs its own UDP
listener and publishes what `sc-api` sends it. That decoupling is desirable anyway: the
reverse-engineering stays out of SimHub's API, and the plugin is a plain data consumer.

## Build

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build_simhub_plugin.ps1
powershell -ExecutionPolicy Bypass -File scripts\build_simhub_plugin.ps1 -Install
```

`-Install` copies the DLL into the SimHub directory, which lives under `Program Files`, so
that step needs an **elevated** shell. Otherwise copy `build\ScApi.SimHub.dll` into
`C:\Program Files (x86)\SimHub\` by hand and restart SimHub.

Built with Roslyn `csc` directly rather than MSBuild/NuGet: it is one source file whose
references all already exist on disk, so a project system only adds failure modes.
Targets **.NET Framework 4.7.2** — SimHub's own demo csproj claims v4.8 while the project is
really 4.7.2, both run on CLR 4.0, and the 4.8 reference assemblies are not installed here.

Verified after build: the assembly exposes `ScApi.Telemetry.ScApiPlugin` implementing
`IPlugin, IDataPlugin` with SimHub's `PluginName` / `PluginAuthor` / `PluginDescription`
attributes, resolved against SimHub's real assemblies.

## Properties

All evaluated on demand via `AttachDelegate`, so they are usable in ShakeIt and dashboard
formulas. Names mirror [`scapi/mapping.py`](../scapi/mapping.py), **which is the source of
truth** and records how confident each identification is.

| Property | Notes | Confidence |
|---|---|---|
| `sc.vehicle.speed` | m/s, observed 0 → 200.997 | strong |
| `sc.vehicle.orientation.{x,y,z}` | radians, bounded by ±π | strong |
| `sc.vehicle.angular_velocity.{x,y,z}` | deg/s | inferred |
| `sc.vehicle.velocity.{x,y,z}` | | inferred |
| `sc.vehicle.accel.{x,y,z}` | plus `accel_alt`, same range | inferred |
| `sc.vehicle.decoupled` | toggle | strong |
| `sc.vehicle.name` | e.g. `ANVL_Hornet_F7C_Mk2` | observed |
| `sc.weapon.recoil{,_b,_c,_d}.{x,y,z}` | decay envelope, ~48 Hz | observed |
| `sc.weapon.shot_fired` | 1 on the frame a shot occurs | observed |
| `sc.actor.heart_rate`, `sc.actor.heartbeat_bpm` | ≈58 bpm observed | observed |
| `sc.impact.vector.{x,y,z}` | fired on a crash | strong |
| `sc.link.connected` | bool: a datagram within the last 2 s | — |
| `sc.link.age_ms`, `sc.link.datagrams`, `sc.link.seq_gaps` | link health | — |

Unmapped fields are still published by the server as `sc.raw.e<key>.m<meaning>`, so nothing
is silently dropped — but those are not pre-declared as properties here.

## Events

Raised with `TriggerEvent`, so ShakeIt effects can trigger from them directly:

`ScApiFootstep` · `ScApiHeartbeat` · `ScApiImpact` · `ScApiVehicleTransition` ·
`ScApiShotFired`

Discrete happenings travel over the wire as **monotonic counters** (`*.count`), not one-shot
flags. The plugin diffs the counter, so a lost datagram cannot swallow a footstep — it still
sees that N occurred. Bursts are capped at 4 per tick so a stall cannot fire a hundred
haptic events at once.

Events are raised from `DataUpdate`, not from the UDP thread, so they are delivered on the
thread SimHub expects. `DataUpdate` does nothing else, as it sits on SimHub's critical path.

## Wiring up ShakeIt

ShakeIt exposes a **custom effect** (`ShakeItEffectCustom` in `SimHub.Plugins.dll`) and
SimHub has a full expression engine (`ExpressionProperty`, `ExpressionEditor`), so effect
gain can be driven by any property above. Sensible starting points:

- **Weapon fire** → effect triggered by `ScApiShotFired`, or gain from
  `abs([sc.weapon.recoil.y])` for the full decay envelope rather than a flat thump
- **Footsteps** → triggered by `ScApiFootstep`
- **Impacts / crashes** → `ScApiImpact`, gain scaled by `sc.impact.vector` magnitude
- **Engine / speed rumble** → gain from `sc.vehicle.speed`
- **Heartbeat** → `ScApiHeartbeat`, which is genuinely ~1 Hz and tracks exertion

## Testing without the game

The whole path can be exercised offline from the recorded capture:

```bash
# terminal 1 -- replay 567k real records through the mapping and publisher
python tools/replay_capture.py captures/sc-capture.jsonl --speed 1

# terminal 2 -- confirm datagrams, independently of SimHub
python tools/udp_listen.py --seconds 30 --watch sc.vehicle.speed sc.weapon.shot_fired
```

With the plugin installed, SimHub itself should show the `sc.*` properties updating while a
replay runs — no Star Citizen, no D-BOX hardware needed.

## Configuration

The listen port is currently fixed at **51680** (`ScApiPlugin.DefaultPort`), matching
`python -m scapi.server --udp 127.0.0.1:51680`. There is no settings UI yet: the plugin
implements `IPlugin` and `IDataPlugin` but not `IWPFSettingsV2`, deliberately, so the build
needs no XAML and stays a single-file `csc` compile.
