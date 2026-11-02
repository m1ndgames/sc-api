# sc-api — manual

Star Citizen streams a lot of haptic telemetry — speed, orientation, weapon
recoil, impacts, footsteps, even the player's heart rate — but only over D-BOX's
proprietary LiveMotion protocol, and only to D-BOX hardware.

`sc-api` implements the **server** side of that protocol. The game connects to it
and streams its telemetry, which `sc-api` republishes as plain JSON over UDP. A
SimHub plugin consumes that stream, so any ShakeIt-driven device — a haptic seat
pad, bass shakers, a motion rig — can use data that was previously locked to one
vendor.

```
Star Citizen --(gRPC/TLS, LiveMotion)--> sc-api --(UDP JSON)--> SimHub plugin --> ShakeIt --> hardware
```

For *how* the protocol was recovered and what every field means, read `README.md`
(next to this file in the release zip, at the repository root in the source
tree). This manual is only about using it.

---

## 1. Read this first

**`sc-api` never touches Star Citizen.** It does not inject code, read game
memory, hook anything, or modify a single file in the game directory. It is a
network server the game chooses to connect to, and all it does is accept
telemetry the game already sends.

That matters because Star Citizen ships **EasyAntiCheat**, which is kernel-level
and inspects what is loaded into the game process.

> **Never replace `dbxLive64.dll` with a custom build.** It is tempting — it
> would remove the need for any D-BOX software at all — and this project
> deliberately does not do it. A replacement would be an *unsigned* module
> adopting the filename of a *D-BOX-signed* one, inside an EAC-protected
> process. Even a behaviourally harmless stub is indistinguishable from DLL
> hijacking, and anti-cheat does not evaluate intent. `sc-api` uses D-BOX's own
> signed DLL, unmodified, and changes only a text config file beside it.

Everything `sc-api` changes is reversible with one command (§8).

## 2. What you need

| | |
|---|---|
| **Star Citizen** | any live build |
| **The D-BOX HaptiSync Center** | free download from D-BOX. It supplies `dbxLive64.dll`, which is **not** included here. No D-BOX *hardware* needed |
| **SimHub** | free; <https://www.simhubdash.com/> |
| **A ShakeIt-capable device** | tested with a Next Level Racing HF8 Pro (ShakeIt **Motors**) |
| **Python 3.11+** | **only for one-time setup.** `sc-api.exe` itself needs no Python |
| **OpenSSL** | for the certificate. Git for Windows ships one — use Git Bash |

### About the connector files

The game loads D-BOX's gRPC client itself, from a path it builds internally:

```
C:\ProgramData\D-BOX\Gaming\LiveMotion\StarCitizen\
    dbxLive64.dll                  D-BOX's own signed client — the gRPC client
    LiveMotionConnector.config     where to connect (this is what we edit)
    livemotion.crt                 the certificate it will trust
```

No registry key, no `Program Files\D-BOX`, and **no D-BOX service** is involved.
`sc-api` takes MotionEngine's place, and MotionEngine is the only thing that DLL
talks to — so the D-BOX software can be uninstalled completely, as long as that
one directory survives.

### Where `dbxLive64.dll` comes from

**It is not in this download, and it never will be.** `dbxLive64.dll` is D-BOX's
own signed software, it is their property, and this project does not
redistribute any of it. You supply it yourself:

1. Install the **D-BOX HaptiSync Center** (free, from D-BOX). You do not need
   D-BOX hardware and you do not need to run or register anything — the
   installer is simply the only legitimate source of this file.
2. It lands in
   `C:\ProgramData\D-BOX\Gaming\LiveMotion\LiveMotionConnector\dbxLive64.dll`,
   and the installer normally also creates the per-game directories beside it.
3. `python setup/patch_connector.py --status` will confirm it is there and that
   it is the genuine, unmodified file (it checks the SHA-256).

That is all the D-BOX software is needed for. Once the connector directory
exists, **no D-BOX service has to run** and `C:\Program Files\D-BOX` is not used
at all — `sc-api` takes the place of MotionEngine, the only thing that DLL talks
to. So the HaptiSync Center can be uninstalled afterwards if you want it gone.

> **If you do uninstall it, copy the connector directory somewhere safe first.**
> The uninstaller deletes `C:\ProgramData\D-BOX`, and nothing can regenerate
> `dbxLive64.dll` — you would have to reinstall HaptiSync Center to get it back.
> Keeping a copy in `vendor/livemotion-connector/` lets
> `patch_connector.py --provision` rebuild the directory from nothing.

## 3. Setup

Unzip the release and work from the directory it creates. Five steps, once.

### 3.1 Generate the certificate

The LiveMotion client requires TLS and checks the certificate's Common Name
against the config, so the name is not arbitrary.

```bash
sh setup/make_cert.sh
```

Writes `certs/livemotion.crt` and `certs/livemotion.key` with
`CN=LiveMotionComm`. Run it from the unzipped directory, in Git Bash.

### 3.2 Point the connector at sc-api

```bash
python setup/patch_connector.py --status
python setup/patch_connector.py --apply --app-key StarCitizen
```

`--apply` backs up D-BOX's own config and certificate first (`*.sc-api-backup`),
writes a config pointing at `127.0.0.1:51666`, and installs your certificate as
the trust anchor. `--status` prints what is currently installed, and whether
`dbxLive64.dll` is still the genuine, unmodified D-BOX file — worth checking.

If the D-BOX software is already uninstalled and you kept your own copy of the
three files, put them in `vendor/livemotion-connector/` and use `--provision`
instead, which rebuilds the directory from scratch.

> **Why port 51666 and not 61666?** 61666 is the real D-BOX endpoint, but on many
> Windows machines it falls inside an invisible Hyper-V / WinNAT reservation and
> cannot be bound by a normal process (`WSAEACCES` / 10013) — even though
> `netsh int ipv4 show excludedportrange protocol=tcp` does not list it. The port
> is client-side configuration, so the simplest fix is to move it. Any free port
> works, as long as `--port` matches on both sides.

### 3.3 Install the SimHub plugin

Close SimHub, copy `ScApi.SimHub.dll` into SimHub's own directory
(`C:\Program Files (x86)\SimHub\`), and start SimHub. It will offer to enable a
new plugin — accept.

The plugin listens on **UDP 51680** and republishes everything as `sc.*`
properties. There is no settings UI: it implements `IPlugin` and `IDataPlugin`
only, deliberately, so it stays a single-file build with no XAML.

Star Citizen is not a SimHub-supported game, so the plugin does **not** read
SimHub's game pipeline — it runs its own listener. This also means every ShakeIt
effect using these properties needs **"process effect when game is not
running"** switched on, or it will never run.

### 3.4 Install the haptic effects

Close SimHub first — it holds its settings in memory and writes them on exit, so
edits made while it runs are silently discarded.

```bash
python setup/install_shakeit_effects.py --status
python setup/install_shakeit_effects.py --install
```

This adds three effects to your active **ShakeIt Motors** profile, each tagged
`sc-api: `, and never touches effects you made yourself. It backs the profile up
first. `--remove` takes exactly those three back out.

| Effect | Driven by | Feel |
|---|---|---|
| `sc-api: Ship speed` | `sc.vehicle.speed`, gamma 2.0 | quiet at low speed, builds as you accelerate |
| `sc-api: Weapon fire / recoil` | `sc.weapon.recoil.y` envelope + `sc.pulse.shot`, front-biased | the game's own recoil decay, under the shoulders |
| `sc-api: Impacts / taking hits` | `sc.pulse.impact` | a single thump per impact |

### 3.5 Run it

```
sc-api.exe --port 51666 --udp 127.0.0.1:51680
```

then start SimHub, then start Star Citizen. The order is not critical — the
LiveMotion client reconnects on its own and re-registers everything — but
starting `sc-api` first means you see the connection happen.

Useful flags:

| Flag | |
|---|---|
| `--jsonl captures\session.jsonl` | record everything the game sends, for later replay |
| `-v` | log every posted event (very chatty) |
| `--udp-hz 90` | publish rate; default 60 |
| `--plaintext` | no TLS. For testing only — the real client requires TLS |

The game itself reports the link in-game, with these messages:

```
D-BOX: running.                                  <- working
D-BOX: idle (in-game menu open / stopped).
D-BOX: init failed (LiveMotion not initialized). <- not connecting
```

## 4. Checking it works

In SimHub, under **Properties**, the `sc.*` entries should update while you play.
Four of them describe the link itself rather than the game:

| Property | |
|---|---|
| `sc.link.connected` | a datagram arrived within the last 2 s |
| `sc.link.age_ms` | milliseconds since the last datagram |
| `sc.link.datagrams` | total received |
| `sc.link.seq_gaps` | datagrams lost — should stay at 0 on loopback |

You can also watch the stream directly, without SimHub:

```bash
pip install -r setup/requirements.txt
python setup/udp_listen.py --seconds 30 --watch sc.vehicle.speed
```

And you can test the whole chain **without the game at all**, by recording a
session with `--jsonl` and replaying it later:

```bash
python setup/replay_capture.py captures/session.jsonl --speed 1
python setup/replay_capture.py captures/session.jsonl --dry-run   # just summarise it
```

Only run **one** publisher at a time — a replay and the live server together,
or two replays, interleave their event counters and make every discrete effect
fire constantly. `replay_capture.py` refuses to start a second instance for
exactly that reason.

## 5. Tuning the effects

Tune in SimHub's ShakeIt UI — it is immediate and you can feel it. The installed
effects are only a starting point.

A few things worth knowing:

- **Speed feels dead in normal flight?** Lower the Gamma from 2.0 toward 1.0
  (more linear) rather than raising the gain. Gain just makes the top end harsh.
- **Recoil** is driven by the game's real decay envelope at ~48 Hz, not a
  synthetic pulse, so keep its filter linear (Gamma 1.0) or you will flatten the
  texture that makes it feel like a weapon.
- **Discrete happenings** (shot, impact, footstep, heartbeat) arrive as
  monotonic *counters*, which are useless in a formula. The plugin converts each
  into an `sc.pulse.*` property that ramps 100 → 0 over 100–260 ms, and that is
  what an amplitude formula should use.
- Property references in ShakeIt formulas need the plugin prefix:
  `[ScApiPlugin.sc.vehicle.speed]`. The expression engine is NCalc, so `Abs`,
  `Max`, `Min`, `Sqrt` and `Pow` are available.

To change the installed defaults instead, edit the `EFFECTS` table at the top of
`install_shakeit_effects.py` and re-run `--install`; it replaces its own effects
in place.

## 6. Using the data without SimHub

The UDP stream is the actual open API, and nothing about it is SimHub-specific.
One JSON object per datagram, newline-delimited:

```json
{"src":"3f8c-1a2b3c4d","seq":1234,"t":1760000000.123,
 "props":{"sc.vehicle.speed":180.2,"sc.weapon.recoil.y":-0.052,"sc.weapon.shot.count":17}}
```

| Key | |
|---|---|
| `src` | publisher instance id. **If this changes, reset any counter state** — it means the publisher restarted, or a second one is running |
| `seq` | increments per datagram, so loss is detectable |
| `t` | Unix timestamp, seconds |
| `props` | flat `{name: value}`; `.count` keys are monotonic counters |
| `part` | present only when one frame had to be split across datagrams |

Two deliberate choices: discrete events are **counters, not flags**, so a lost
datagram cannot swallow a shot — a consumer diffs the counter and still knows N
occurred; and properties are merged per frame and flushed at a bounded rate, so
a consumer never sees half a frame.

Unrecognised fields are still published, as `sc.raw.e<key>.m<meaning>`, so
nothing the game sends is silently dropped. The full property list and how
confident each identification is lives in `scapi/mapping.py` in the source
repository.

## 7. Troubleshooting

**Nothing arrives; the game says "init failed".**
Check `sc-api` is listening on the port the config names
(`python setup/patch_connector.py --status`), and that the certificate's CN
matches `<CertificateCommonName>`. Both sides must agree on all three of host,
port and CN.

**`WSAEACCES` / "10013" on startup.**
The port is reserved by Hyper-V / WinNAT. Pick another and set both ends:
`sc-api.exe --port 51777` and `patch_connector.py --apply --port 51777`.

**`sc-api` runs, SimHub shows nothing.**
`--udp 127.0.0.1:51680` must be on the `sc-api` command line — without it
nothing is published. Then check `sc.link.datagrams` in SimHub: if it climbs,
the link is fine and the problem is in the effect (most often "process effect
when game is not running" being off).

**An effect fires constantly, or far too often.**
Almost always two publishers at once — a replay still running alongside the
live server, or two replays. Each keeps its own counters, and interleaving them
makes a consumer see endless events. Check SimHub's log for:

```
sc-api: publisher changed or restarted (src=...) -- counters reset
```

The plugin detects this and resets, and `replay_capture.py` refuses to start a
second instance, but two deliberate publishers on one port will still collide.

**SimHub's log** is at `C:\Program Files (x86)\SimHub\SimHub.txt` — the plugin
logs its listener startup, every publisher change, and periodic counts there.

## 8. Reverting everything

```bash
python setup/patch_connector.py --restore
python setup/install_shakeit_effects.py --remove     # SimHub closed
```

`--restore` puts D-BOX's own config and certificate back and removes any
directory `--provision` created. `--remove` deletes only the three `sc-api: `
effects. Then delete `ScApi.SimHub.dll` from the SimHub directory.

Nothing in the Star Citizen directory was ever touched, so there is nothing to
undo there.

## 9. Limits worth knowing

- **Being shot at is not confirmed.** The impact event fired twice in the
  recorded session, both times from a crash. Incoming fire may well be a
  different event, and six of the twenty events the game registers have never
  been seen to fire at all.
- **Ship weapons are not confirmed either.** Recoil is confirmed for
  first-person weapons; the ship-weapon case has not been captured yet.
- Many field meanings are inferred rather than observed. `scapi/mapping.py`
  records which is which, per property.
- Ground vehicles are unexplored: one event declares a four-element float type
  that is probably per-wheel data, and it has never carried a value.

## 10. Licence

PolyForm Noncommercial 1.0.0 — see `LICENSE.md`. **Noncommercial use only.**
Personal use, hobby projects, research, education and charitable or government
use are all permitted, including modifying it and sharing your changes.
Commercial use is not, which covers selling it, bundling it into a paid product
or service, and use in the operations of a for-profit organisation. Ask first if
you want to do something commercial with it.

`dbxLive64.dll` is not covered by this licence: it is D-BOX's own software, it
is not distributed here, and you obtain it from them (§2).
