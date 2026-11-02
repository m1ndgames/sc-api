# sc-api

An open telemetry API for Star Citizen's haptic/motion data, built by implementing the
same interface D-BOX hardware uses.

Star Citizen has a rich, well-structured haptic event pipeline, but it is only addressable
by D-BOX hardware — there is no general-purpose API. `sc-api` stands in the place of the
D-BOX runtime, receives the telemetry the game already emits, and re-publishes it in an
open format so any motion or rumble setup can consume it.

> **Status: the whole chain works.** The protocol is recovered and verified against the
> shipped descriptor; Star Citizen connects to the shim and a 39-minute session yielded
> **567,370 telemetry records** across 14 of the game's 20 event types — flight motion,
> weapon recoil, impacts, footsteps and heart rate. Telemetry is republished over UDP and
> consumed by a SimHub plugin, with the `sc.*` properties **confirmed updating live in
> SimHub**. See [Observed behaviour](#observed-behaviour-capture-2026-10-13) and
> [Output](#6-output-the-open-api).
>
> Remaining: tuning ShakeIt effects against real hardware, a ground-vehicle capture to
> confirm the `Quad` per-wheel type, and most `meaning` constants are still opaque
> (unmapped fields are published raw rather than dropped).

**If you just want to use it, read [`docs/MANUAL.md`](docs/MANUAL.md)** — setup,
running, tuning and reverting. This document is the research writeup: how the
protocol was found, what every field means, and which claims are observed versus
inferred.

Licensed [PolyForm Noncommercial 1.0.0](LICENSE.md) — noncommercial use only. See
[Licence](#licence).

---

## 1. How Star Citizen actually talks to D-BOX

Everything below was observed from the shipped binaries and a live D-BOX installation on a
machine with **no D-BOX hardware attached**. Inferences are marked as such.

```
  StarCitizen.exe
      |
      |  D-BOX Live Motion SDK, STATICALLY linked  (LiveMotionApiMD, dbox.cpp)
      v
  LoadLibrary("dbxLive64.dll")
      C:\ProgramData\D-BOX\Gaming\LiveMotion\<appKey>\dbxLive64.dll
      |
      |  gRPC over TLS  ->  127.0.0.1:61666   (server CN = "LiveMotionComm")
      |  service dbox.proto.v1.LiveMotion
      v
  MotionEngine.exe  --  dbox::LiveMotionComm::GrpcServer
      |               (started only in the "Adaptive Gaming" experience mode)
      v
  Motion mixing -> actuator output (KCU / KAI / HapticBridge USB)
```

There are therefore **two** interception points, not one: the gRPC channel, and the
connector DLL itself.

### The transport is gRPC

Not shared memory, not named pipes, not a UDP broadcast.

| | |
|---|---|
| Service | `dbox.proto.v1.LiveMotion` |
| Endpoint | `127.0.0.1:61666` (TLS) |
| `GetIdentification` | unary — handshake / version exchange |
| `LiveMotionStream` | **bidirectional streaming** — carries everything else |

`LiveMotionStream` is a single long-lived bidi stream. Every operation (initialize, open,
start, register event, post event, heartbeat, terminate) is a variant inside
`LiveMotionRequest`, answered by a `LiveMotionResponse`.

### Where each fact came from

| Fact | Evidence |
|---|---|
| gRPC + full proto schema | Serialized `FileDescriptorProto` embedded in `MotionEngine.exe` at offset `17045568`, length `4209` bytes |
| Endpoint `127.0.0.1:61666`, TLS, CN `LiveMotionComm` | `…\Gaming\LiveMotion\LiveMotionConnector\LiveMotionConnector.config` (plain XML) |
| Server cert identity | `livemotion.crt` — self-signed, `O=D-BOX Technologies, CN=LiveMotionComm`, valid 2023-07-04 → 2033-07-01 |
| SDK statically linked into the game | `LiveMotionApiMD`, `dbox.cpp` in `StarCitizen.exe`; MSVC `…MD` denotes a static lib built against the multithreaded-DLL runtime |
| Game loads the connector DLL at runtime | string `dbxLive64.dll` in `StarCitizen.exe` |
| `dbxLive64.dll` is the gRPC client | contains the same `dbox.proto.v1.*` descriptors, plus `LiveMotionStream` / `GetIdentification` |
| gRPC server runs only in Adaptive Gaming mode | `dbox::ExperienceModeLiveMotionBase::Start` → `GrpcServer::_RunServer`; with MotionEngine in mode 1 (Coded Gaming) nothing listens on 61666 |
| Experience modes | `ExperienceMode{CodedGaming, CodedVideo, AdaptiveGaming, AdaptiveAudio}`; `AdaptiveGaming` derives from `LiveMotionBase`. Matches D-BOX's four published HaptiSync modes |
| MotionEngine version | `C:\Jenkins\workspace\re_motion-engine_release_v6.12.0\…` build paths |

### Ports, for orientation

Observed listeners on the test machine:

| Port | Owner | What it is |
|---|---|---|
| 12740 | MotionEngine | Cysca messaging socket. **Not** gRPC — rejects an HTTP/2 preface and is not TLS. This is what `HaptiSync Api/appsettings.json` calls `MotionEngine.Port` |
| 12745, 61555 | MotionEngine | further `dbox.Base.SocketTcpListener` sockets |
| 40001 | MonitorService | XML/SOAP |
| 42010 / 42011 | HaptiSyncApi.WebApi | REST (admin / public), Swagger enabled in config |
| **61666** | MotionEngine | **LiveMotion gRPC (TLS)** — bound only in Adaptive Gaming mode |

A natural first guess is that 12740 is the telemetry port. It is not; it is the control
socket used by D-BOX's own REST API.

### The connector DLL

`C:\ProgramData\D-BOX\Gaming\LiveMotion\` holds one connector directory per `appKey`:

```
LiveMotion\LiveMotionConnector\   dbxLive32.dll  dbxLive64.dll
                                  LiveMotionConnector.config  livemotion.crt
LiveMotion\UniversalHaptiCode\    -> symlinks to LiveMotionConnector
```

`dbxLive64.dll` exports exactly **one** symbol, `GetEventHandler` — a factory returning an
interface. The SDK's C ABI is therefore a single vtable, not the flat
`DBoxInitialize()` / `DBoxSendTelemetryFrame()` style C API one might expect.

The directory name is the `appKey`, confirmed by `live_motion_code_track.app_key =
'UniversalHaptiCode'` in `MotionEngine.db` matching the `UniversalHaptiCode\` folder.
Star Citizen's own `appKey` is not yet known — *inferred* to be `StarCitizen`, since
`StarCitizen` and `StarCitizenApplication` appear in the executable, but unconfirmed.

---

## 2. The protocol

The complete schema is reconstructed in [`proto/LiveMotion.proto`](proto/LiveMotion.proto),
taken verbatim from the embedded descriptor — field numbers and wire types are observed,
not guessed. `tools/extract_descriptor.py` re-extracts and verifies it.

### Lifecycle

```
GetIdentification                      -> server id / name / version, supported client types
LiveMotionStream (open bidi stream)
    initialize {appKey, appVersion, apiKey}
    open / start
    register_event { EventDefinition }      x N   -- declares each event's schema
    post_event / post_events { EventInfo }  x N    -- actual telemetry
    heartbeat
    stop / close / terminate
```

### Events are self-describing

This is the single most useful property of the protocol. Before posting telemetry, the
client declares each event's layout:

```protobuf
message EventDefinition {
  uint32 eventKey     = 1;   // identifies the event type
  uint32 eventMeaning = 2;   // semantic code (SDK constant)
  uint32 fieldCount   = 3;
  uint64 fieldAddress = 4;
  repeated FieldDefinition fieldDef = 5;
}
message FieldDefinition {
  uint32 type = 1; uint32 flags = 2; uint32 meaning = 3; uint32 offset = 4;
}
```

So a server never hardcodes struct offsets — **the game tells us its own layout at
runtime**. Values then arrive in a union:

```protobuf
message EventField {
  int32  dataInt32   = 1;  int64  dataInt64   = 2;
  float  dataFloat32 = 3;  double dataFloat64 = 4;
  string dataString  = 5;  bytes  dataWideString = 6;
  XYZ_StructFloat32 dataFloat32XYZ = 7;   XYZ_StructFloat64 dataFloat64XYZ = 8;
  QuadInt32 dataQuadInt32 = 9;
  QuadFloat32 dataQuadFloat32 = 10;  QuadFloat64 dataQuadFloat64 = 11;
}
```

Note the `Quad*` types: `frontLeft / frontRight / backLeft / backRight`. Four corners —
directly meaningful for a seat pad.

### What Star Citizen sends

The game's registered event taxonomy, from the `DBOX Register …` strings in
`StarCitizen.exe`. These are **named semantic events**, not merely raw motion vectors:

| Category | Events |
|---|---|
| Vehicle | Vehicle Frame, Ground Vehicle Frame, Vehicle Impact, Vehicle Explosion Impact, Vehicle Decoupled, Enter/Leave Vehicle |
| Weapons | Weapon Fire Frame, Recoil Frame, Turret Frame, Explosive Ordnance Frame, Throw Frame |
| On foot | Footstep, Actor Frame, Impact Frame |
| Ambient / body | Heartbeat Pulse, Vibration Frame, Audio Haptic Frame, Force Reaction Haptic |
| Session | Initialize, Open, Close, Player Role, Global Frame |

The submit path is `DBox_CollectAndSubmitFrame`.

For a haptic seat pad this is a far better signal than a generic surge/sway/heave vector
model — discrete, labelled, already semantic.

### Observed behaviour (capture, 2026-10-13)

A ~39 minute session (on foot, then flying an `ANVL_Hornet_F7C_Mk2`, ending in a crash)
produced **542,207 records**. Star Citizen connects, registers all 20 event schemas and
streams continuously. Analyse a capture with `tools/analyze_capture.py`.

Session identity, as sent by the game:

```
appKey    = 'StarCitizen'
appVersion= 1
apiKey    = 'LiveMotionApiMD.13289'
client_id = 'D-BOX LiveMotionConnector:0b08d5f7-...'   client_type = LiveMotionGame
```

The per-frame events run at **~47-49 Hz**. The client **reconnects by itself** after an
interruption (two `initialize` records in one session), and shuts down with a clean
`stop` / `close` / `terminate`.

| key | fields | posted | rate | identification | confidence |
|---|---|---|---|---|---|
| 0 | 1 | 105,520 | 49 Hz | per-frame global/tick | inferred |
| 1 | 1 | 1 | once | **vehicle identity** — string `ANVL_Hornet_F7C_Mk2` | **observed** |
| 2 | 0 | 1 | once | pure trigger, no payload — enter/leave vehicle | inferred |
| 3 | 14 | 51,560 | 47 Hz | **vehicle/flight frame** — the main motion signal | strong |
| 4 | 19 | 105,520 | 49 Hz | actor frame (all 19 fields float32) | inferred |
| 5 | 2 | 82 | burst | audio-haptic / force reaction | weak |
| 6 | 2 | 5 | — | as above, paired meanings (104, 171) | weak |
| 8 | 4 | 2 | — | **impact** — fired exactly on the crash, vector peak y=11.6 | strong |
| 9 | 1 | 2 | — | **decoupled toggle** — `1` then `0`, matching a decoupled flip | strong |
| 10 | 1 | 105,520 | 49 Hz | per-frame int32 | inferred |
| 12 | 13 | 105,520 | 49 Hz | actor state incl. heart rate | strong |
| 13 | 5 | 65,699 | 48 Hz | **weapon recoil** — see below | **observed** |
| 14 | 1 | 2,252 | **~1 Hz** | **heartbeat** — 2,252 beats / 2,320 s ≈ 58 bpm | **observed** |
| 16 | 7 | 455 | ~6 Hz | **footstep** — count tracks walking | strong |
| 7, 11, 15, 17, 18, 19 | — | 0 | — | silent; 18 is the only `type=153` holder | — |

**Weapon recoil (key 13)** is the clearest example of how usable this data is. Four XYZ
vectors plus an int32. The int32 is non-zero only on the *first* frame of a burst — a
discrete "shot fired" trigger — and the vectors are the recoil decay envelope sampled at
48 Hz. One shot, field `meaning=38`, y component:

```
-0.0520  -0.0634  -0.0549  -0.0328  -0.0153  -0.0085
```

A sharp kick relaxing to zero. For a haptic device that is a trigger plus a ready-made
intensity curve; no envelope synthesis needed.

**Vehicle frame (key 3)** field ranges over 51,560 frames:

| `meaning` | kind | observed range | likely meaning | confidence |
|---|---|---|---|---|
| 94 | f32 | 0 → 200.997 | scalar speed, m/s (Hornet SCM top speed) | strong |
| 41 | xyz32 | ±3.3056 | orientation, radians (bounded by ±π) | strong |
| 40 | xyz32 | ±328 (peak 350) | angular velocity, deg/s | inferred |
| 9 | xyz32 | ±224 | velocity | inferred |
| 38, 68 | xyz32 | ±79.5 (peak 82.6) | acceleration — **identical ranges, duplicated** | inferred |
| 34, 35, 39 | xyz32 | ±39 / ±17 / ±45 | further motion vectors | unknown |
| 4, 42, 43, 92 | scalar | constant 1 / 1 / 2 / 0.5–1 | flags, config | unknown |

### The field type enum

Not in the schema -- derived by correlating declared `FieldDefinition.type` against the
`EventField` member the game actually populated, over ~4.6M field observations:

| type | hex | member | evidence |
|---|---|---|---|
| 23 | `0x17` | `dataInt32` | 251,727 non-zero |
| 25 | `0x19` | `dataFloat32` | 359,445 non-zero |
| 96 | `0x60` | `dataString` | 1 (`ANVL_Hornet_F7C_Mk2`) |
| 137 | `0x89` | `dataFloat32XYZ` | 1,202,878, never absent |
| 153 | `0x99` | `dataQuadFloat32` | **inferred, never observed** |

The encoding looks like `(shape << 4) | base`, with base `7`=int32 / `9`=float32 and shape
`1`=scalar / `8`=XYZ / `9`=Quad. Event 18 is the only event declaring `153`, and it stays
silent — consistent with the `dbox_gv_wheels` string in `StarCitizen.exe`, i.e. **`Quad` is
per-wheel data for ground vehicles**, not a seat layout.

> **Why this matters for correctness.** proto3 does not transmit a scalar equal to zero, so
> a field holding `0` is wire-identical to an absent one. The table above is what lets the
> decoder tell "value is zero" from "no value" — without it every idle/neutral sample
> silently disappears, which for e.g. the recoil trigger means losing the entire
> "not firing" state. Submessages (`XYZ`, `Quad`) are unaffected: they carry real presence,
> which is why `type 137` never shows as absent.

### What `meaning` is not

`FieldDefinition.meaning` is **not** a global semantic vocabulary. `meaning=38` is a ±44
vector in event 3, a ~0.05 recoil vector in event 13, and a ~60 float (heart rate) in
event 14. Values 38–56 behave like generic sequential field slots, while scattered lower
values (9, 34, 35, 68, 92, 93, 94, 104, 154, 171) look like specific named constants.
A full mapping needs the SDK headers or more correlation; do not assume it from the number.

---

## 3. Approach

Because the game is the *client*, `sc-api` is the **server**. That removes the hardest
problem outright: `InitializeParam` carries `appKey` and `apiKey`, and the game sends those
*to us*. We accept them rather than having to satisfy them. No key recovery is needed.

### Option A — gRPC server (fast path)

Serve `dbox.proto.v1.LiveMotion` on 61666 over TLS. The protocol is fully known, and
`LiveMotionConnector.config` lets us redirect host, port and expected certificate CN with a
text edit — no binary patching, fully reversible.

**Dependency:** this keeps D-BOX's `dbxLive64.dll` in the loop, because that DLL *is* the
gRPC client. See [Minimal footprint](#minimal-footprint) — it needs three files on disk,
and nothing else from D-BOX.

### Minimal footprint

**The D-BOX software can be uninstalled completely.** No service, no
`C:\Program Files\D-BOX`, no registry key is required.

The game builds its connector path itself, which is why. The format string sits in
`StarCitizen.exe`:

```
%sD-BOX\Gaming\LiveMotion\%s\%s
 |                        |   |
 base (ProgramData)       |   dbxLive64.dll
                          appKey ("StarCitizen")
```

No registry lookup is involved (`StarCitizen.exe` contains no `SOFTWARE\D-BOX` string, in
ASCII or UTF-16). So the entire runtime requirement is one directory:

```
C:\ProgramData\D-BOX\Gaming\LiveMotion\StarCitizen\
    dbxLive64.dll                  (10 MB, D-BOX's own signed client)
    LiveMotionConnector.config     points at sc-api
    livemotion.crt                 our certificate
    LiveMotionConnector.log.config (optional, enables the DLL's own logging)
```

D-BOX's uninstaller is likely to delete `C:\ProgramData\D-BOX`, so a local copy of those
files belongs in [`vendor/livemotion-connector/`](vendor/livemotion-connector/) first.
To rebuild the directory with D-BOX fully uninstalled:

```bash
sh scripts/make_cert.sh
python tools/patch_connector.py --provision
```

**Order matters:** copy the files aside *before* running the uninstaller.

`dbxLive64.dll` is **D-BOX's signed software and is not redistributed by this project** —
it is excluded from the repository and from release builds, and every user supplies it from
their own D-BOX HaptiSync Center installation. That installer is the only legitimate source,
and `patch_connector.py --status` verifies the file in place is the genuine one by SHA-256.
**Nothing of D-BOX's is in this repository at all** — not the DLL, not their config files,
not their certificate. `vendor/livemotion-connector/` tracks only its own README, which
documents what those files contain; the directory is otherwise ignored, so a local copy can
live there without ever being committed. The config and certificate `sc-api` installs are
generated, not copied: `patch_connector.py` writes the config and `scripts/make_cert.sh`
makes the certificate, and `--restore` reads the `.sc-api-backup` files it left in place
rather than any pristine original.

Services that can be removed: `DboxMotionEngine`, `DboxHaptiSyncApi`,
`DboxMotionPlayerMonitor`, plus the KAI/KCU/HapticBridge drivers. `sc-api` replaces
MotionEngine, which is the only thing `dbxLive64.dll` talks to.

Verification without any tooling: the game itself reports D-BOX status, using these strings
found in `StarCitizen.exe`:

```
D-BOX: running.
D-BOX: idle (in-game menu open / stopped).
D-BOX: init failed (LiveMotion not initialized).
```

### Option B — a custom `dbxLive64.dll` — REJECTED

**Do not do this.** Star Citizen ships **EasyAntiCheat**
(`EasyAntiCheat/EasyAntiCheat_EOS_Setup.exe` in the game directory), which is kernel-level
and inspects modules loaded into the protected game process.

A replacement `dbxLive64.dll` would be an **unsigned** module adopting the filename of a
**D-BOX-signed** one, inside that process. Even though such a stub is behaviourally
harmless — it reads no game memory, hooks nothing, injects nothing, and merely implements
an interface the game asks for — anti-cheat does not evaluate intent, and "benign but
indistinguishable from DLL hijacking" is a poor bet against an account with real money in
it. Self-signing does not help, because a self-signed certificate is not trusted.

The probe source (`native/dbxlive_stub.cpp`) and its build script are kept, because the
recovered native ABI below is the evidence for this section. The tool that *installed* the
built stub into the game's load path has been deleted: nothing in this repository should
make that one command easy.

The only thing Option B bought was avoiding a D-BOX install. That is now solved safely by
vendoring three files instead (see [Minimal footprint](#minimal-footprint)), so the
trade — a few files on disk versus an anti-cheat flag — is not a close one.

The ABI below was fully recovered before this was decided, and is kept as reference
material. A probing stub exists in `native/dbxlive_stub.cpp` and builds and self-tests
cleanly (`tools/test_stub.py`), but **it was never loaded into the game** and should not be.
The installer that could have put it there was deleted rather than kept around.

Verify the genuine DLL is still in place at any time:
`python tools/patch_connector.py --status` reports its SHA-256 against D-BOX's.

Cost: the vtable returned by `GetEventHandler` must be reverse engineered (Ghidra, against
the shipped `dbxLive64.dll`). Option A is the cheap way to learn the protocol's semantics
first — notably the `eventMeaning` constants — which transfers directly to Option B.

#### The native ABI (recovered 2026-10-13)

All of the following is **observed** from `dbxLive64.dll` (SHA-256
`85a6e973…82ba6a`, 10,326,624 bytes, 2025-09-19), via MSVC RTTI and Ghidra 12.1.4.

The single export is a **lazily-initialised singleton getter**, not a factory:

```c
// 0x180131060 -> tail-calls 0x18012f6d0
IEventHandler* GetEventHandler(void);
```

`0x18012f6d0` is the classic MSVC magic-static pattern (`_tls_index`, guard variable
`0x180979ee8`, `_Init_thread_footer`) and returns `&0x180979ec0`. The constructor at
`0x1801318d0` calls a base constructor and then installs
`dbox::LiveMotionConnectorManager::vftable`. So the object handed to the game is a
**`LiveMotionConnectorManager`**; the caller allocates nothing and frees nothing.

Class hierarchy, from RTTI type descriptors:

```
dbox::IEventHandler                        (abstract; no vtable of its own emitted)
  +- dbox::EventHandlerProxy               14 slots @ rva 0x736188
  |    +- dbox::CompatibilityEventHandlerProxy  14 slots @ rva 0x736200
  |         (overrides only slots 0, 8, 9, 13)
  +- dbox::LiveMotionConnectorManager      14 slots @ rva 0x732b38  <-- returned
  +- dbox::LiveMotionConnectorEventHandler 13 slots @ rva 0x733238
```

`LiveMotionConnectorManager` inherits slots 2-7 and 10-12 from `EventHandlerProxy`,
takes slots 8 and 9 from `CompatibilityEventHandlerProxy`, and overrides 0, 1 and 13.
The presence of a *Compatibility* proxy overriding exactly the `PostEvent` family implies
the native event-payload layout has been versioned at least once — worth respecting.

The 13 methods, named from log strings in `.rdata` (`0x7336d8`-`0x7339d8`) in binary
order, which is probably declaration order but **is not confirmed as vtable order**:

```
Initialize  Terminate  Open  Close  Start  Stop  ResetState  RegisterEvent
PostEvent  PostEventToSession  SetOpenExtraSessions  PostEvents  PostEventsToSession
```

Note this differs from the proto field order (`register_event`, `set_open_extra_sessions`,
`reset_state`, …), so **the vtable order cannot be inferred from the .proto**. The proto
numbering appears to reflect chronological additions instead.

Every slot of `LiveMotionConnectorEventHandler` is a thin forwarder to an inner object at
`this+8`. Two of them stand out — they take a second argument, take a lock, and return a
value, while the rest are argument-less and `void`:

| slot | thunk | forwards to | shape |
|---|---|---|---|
| 7 | `0x138de0` | `0x16fe50` | `(this, arg)` → int, locked |
| 8 | `0x138cc0` | `0x16f770` | `(this, arg)` → int, locked |
| 11, 12 | `0x138da0`, `0x138dc0` | both `0x16f9a0` | identical target |

That is consistent with the payload-carrying calls (`RegisterEvent` / `PostEvent`) being
slots 7 and 8, and returning a `LiveMotionResult`.

#### What is still needed

1. **Authoritative slot→name mapping.** Declaration order is a good hypothesis, not proof.
2. **Argument types.** Ghidra's calling-convention inference on these thunks is unreliable
   (it reports `void` for functions that demonstrably return a value).

**Planned approach — empirical, not static.** Build a stub `dbxLive64.dll` exporting
`GetEventHandler`, returning an object with a 14-slot vtable of logging thunks that record
the slot index and raw register arguments and return 0 (which the gRPC capture already
showed means success). Then run the game and correlate the observed call order against the
**known-good sequence from `captures/sc-capture.jsonl`**
(`Initialize → Open → RegisterEvent ×20 → Start → PostEvent…`). That names every slot from
ground truth and validates the ABI in one step, instead of reverse engineering 14
signatures by hand.

Toolchain: MSVC 2022 BuildTools is present
(`…\2022\BuildTools\VC\Tools\MSVC\14.44.35207`, via `vcvars64.bat`); `cl` is not on PATH.

**Other useful finds.** `dbox::LiveMotionComm::GrpcClient` carries
`ConnectWithFallback`, `TryConnectToTargetWithValidation` and `ValidateServerIdentity` —
the client really does verify server identity, which is why the `CN=LiveMotionComm`
certificate was necessary, and `ConnectWithFallback` suggests more than one candidate
endpoint is tried.

**Original starting points.** The work is well-bounded because the gRPC side is already
understood, and the vtable largely mirrors it:

1. Target `C:\ProgramData\D-BOX\Gaming\LiveMotion\LiveMotionConnector\dbxLive64.dll`
   (10,326,624 bytes, dated 2025-09-19). Its export table holds exactly one name,
   `GetEventHandler`.
2. Recover the returned interface's vtable: slot count, calling convention and each
   method's signature. Expect close correspondence to the gRPC surface already mapped —
   `Initialize(appKey, appVersion, apiKey)`, `Open`, `Start`, `RegisterEvent(EventDefinition)`,
   `PostEvent(s)(EventInfo)`, `Heartbeat`, `Stop`, `Close`, `Terminate`.
3. The native struct layouts should be recoverable from the same binary, and
   `EventDefinition.fieldAddress` / `FieldDefinition.offset` (both carried in the proto but
   unused by a gRPC server) exist precisely because the native API passes event payloads as
   a packed struct read via those offsets. They become meaningful in Option B.
4. Validate against a real capture: a correct DLL should make Star Citizen emit the same 20
   event registrations seen in `captures/sc-capture.jsonl`.

Keep Option A working while developing Option B — it is the reference implementation to
diff behaviour against.

### Dropped

Emulating D-BOX hardware over USB (spoofing a KCU/ACM box) is **unnecessary**: interception
happens well above the hardware boundary.

### Corrections worth recording

Two plausible-looking leads were wrong, noted so they are not re-walked:

- `Bin64\livesdk.dll` in the game folder is **Faceware LiveSDK** (facial capture for FOIP),
  not the D-BOX "Live Motion SDK". Unrelated despite the name.
- Many `dbox` strings in `StarCitizen.exe` are false positives: CryEngine debug/bounding
  boxes (`dBoxSize`, `dBoxOpacity`, `dbox_edge`) and an unrelated organisation-infraction
  `dboxTrigger` system from DataForge. The genuine integration is the
  `DBOX Register … Frame` family plus `DBox_CollectAndSubmitFrame`.

---

## 4. Open questions

### Resolved by the capture

| Question | Answer |
|---|---|
| Does the retail client emit? | **Yes.** No gating observed; 542k records in one session |
| Star Citizen's `appKey` | **`StarCitizen`** — the directory must exist under `…\Gaming\LiveMotion\`; `tools/patch_connector.py --app-key StarCitizen` provisions it |
| TLS trust model | A self-signed cert with `CN=LiveMotionComm` installed as `livemotion.crt` is accepted. No pinning, no mutual TLS |
| Result codes | `result_code = 0` and `Reply.liveMotionResult = 0` are accepted as success; the client keeps streaming |
| Reconnection | The client reconnects on its own after an interruption and re-registers all events |
| `FieldDefinition.type` enum | Mapped for 4 of 5 observed values — see [the type enum](#the-field-type-enum) |

### Still open

1. **`eventMeaning` / `FieldDefinition.meaning` constants.** Partially inferred (see the
   tables above), but most remain opaque. `meaning` is per-event, not global.
2. **`type = 153` (float32 Quad).** Declared only by event 18 and never yet observed
   carrying data. Needs a **ground vehicle** session to confirm it is per-wheel data.
3. **Events 7, 11, 15, 17, 18, 19** have never fired. Likely need turrets, ground
   vehicles, explosive ordnance or being shot at.
4. **Duplicate vectors.** Event 3 sends identical ranges under `meaning` 38 and 68 —
   possibly raw vs. filtered acceleration.
5. **`GetEventHandler`'s vtable layout** — required for Option B (no D-BOX install).

---

## 5. Running it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt

# Verify our schema still matches the shipped binary exactly.
python tools/extract_descriptor.py

# Generate the TLS identity (CN must match CertificateCommonName).
sh scripts/make_cert.sh

# Point D-BOX's connector at us (backs everything up; --restore undoes it).
python tools/patch_connector.py --apply --port 51666 --app-key StarCitizen

# Serve, and record everything the game sends.
python -m scapi.server --port 51666 --jsonl captures/sc-capture.jsonl -v

# Prove the server works, independently of the game.
python tools/selftest.py --target 127.0.0.1:51666
```

`tools/patch_connector.py --restore` puts the original config and certificate back and
removes any provisioned appKey directories.

### Why port 51666 and not 61666

The real endpoint is `127.0.0.1:61666`, but on many Windows machines that port falls inside
an invisible Hyper-V / WinNAT reservation and cannot be bound by a normal process
(`WSAEACCES` / 10013) — even though `netsh int ipv4 show excludedportrange` does not list
it. Since the port is *client-side* configuration, the fix is simply to move it. Check with:

```powershell
netsh int ipv4 show excludedportrange protocol=tcp
```

### Reproducing the protocol recovery

No disassembler is required. Protobuf embeds the full `FileDescriptorProto` into its
generated C++ code, so the schema can be read straight out of the shipped binary.
`tools/extract_descriptor.py` locates it (by the `\x0a\x10LiveMotion.proto` marker), walks
its top-level wire fields to find the end of the blob, then compiles
`proto/LiveMotion.proto` and compares the two **semantically** — every message's fields
with their numbers, labels and types, every enum value, and the service's methods with
their streaming flags. Current result:

```
offset : 17045568  length: 4209 bytes
content: 33 messages, 1 enums, 1 service(s)
OK: proto/LiveMotion.proto matches the embedded descriptor exactly
```

### Building the release artifacts

Two artifacts, both Windows-only, both built in CI by
[`.github/workflows/build.yml`](.github/workflows/build.yml) and reproducible locally:

```powershell
scripts\build_api.ps1 -Verify              # -> build\dist\sc-api.exe
scripts\build_simhub_plugin.ps1 -Install   # -> build\ScApi.SimHub.dll
```

`sc-api.exe` is a PyInstaller single-file build, so the API runs with no Python
installed. Certificates and captures resolve next to the executable rather than inside
the bundle (`scapi.data_root()`), and `-Verify` drives the frozen binary through the whole
LiveMotion lifecycle with `tools/selftest.py` — the frozen build has failure modes the
source does not, since the protobuf stubs import each other by bare module name and grpc
ships a native extension.

**The plugin is the interesting one to build in CI.** It normally compiles against
SimHub's own assemblies, which are proprietary and absent from a runner. CI instead
compiles it against signature-only facades in [`simhub/refs/`](simhub/refs/) — the handful
of SimHub types the plugin touches, with no implementation. That is sound because SimHub's
assemblies are **not strong-named** (`PublicKeyToken=null`), so the CLR binds them by
simple name and ignores assembly version.

Soundness is not assumed, it is checked. `scripts/verify_simhub_plugin.ps1`, run on a
machine where SimHub *is* installed, loads the facade-built DLL against the **real** SimHub
assemblies and JIT-prepares `Init` / `End` / `DataUpdate`, which forces the CLR to resolve
every member reference in those bodies — including the extension-method calls that compile
to a direct static call naming `SimHub.Plugins.IPluginExtensions` — without executing any
of it. A faithless facade fails there instead of failing inside SimHub:

```
implements IPlugin (REAL SimHub.Plugins): True
resolved : Init against the real SimHub -- OK
VERIFIED: the plugin binds against the real SimHub assemblies.
```

Tagging `v*` additionally bundles both artifacts with the manual into a release zip. The
bundle deliberately excludes `dbxLive64.dll`: it is D-BOX's, and nothing of theirs is
redistributed here.

## 6. Output: the open API

The point of the project. `sc-api` republishes mapped telemetry as newline-delimited JSON
over **UDP**, which any consumer can read:

```bash
python -m scapi.server --port 51666 --udp 127.0.0.1:51680
```

One JSON object per datagram:

```json
{"seq":1234,"t":1760000000.123,"props":{"sc.vehicle.speed":180.2,"sc.weapon.recoil.y":-0.052}}
```

UDP on loopback is the right transport behind a ~50 Hz game loop feeding a haptic device:
fire-and-forget, no connection state, and a stalled consumer cannot add latency to the
producer. For haptics a late sample is worse than a lost one.

Two deliberate design points:

- **`seq` increments per datagram** so a consumer can detect loss.
- **Discrete events are monotonic counters** (`sc.actor.footstep.count`), not one-shot
  flags. A lost datagram therefore cannot swallow a footstep or a shot: the consumer diffs
  the counter and still knows N occurred.

Properties are flat `{name: number}` because that is what maps onto SimHub properties and
ShakeIt expressions. The event→name mapping lives in
[`scapi/mapping.py`](scapi/mapping.py), which tracks confidence per field
(`observed` / `strong` / `inferred`). **Unmapped fields are still published** as
`sc.raw.e<key>.m<meaning>`, so nothing is silently dropped while the mapping is incomplete.

Several properties per frame are merged into one coherent snapshot and flushed at a bounded
rate (`--udp-hz`, default 60) rather than emitting a datagram per event, so a consumer never
sees half a frame.

### SimHub plugin

A C# plugin consumes that stream and republishes it as SimHub properties and events, so
ShakeIt can drive the hardware. See [`simhub/README.md`](simhub/README.md). Star Citizen is
not a SimHub-supported game, so the plugin is self-driven rather than reading SimHub's game
pipeline.

### Developing without the game

The recorded capture makes the whole output path testable offline and repeatably:

```bash
python tools/replay_capture.py captures/sc-capture.jsonl --dry-run   # what it would emit
python tools/replay_capture.py captures/sc-capture.jsonl --speed 1   # replay to UDP
python tools/udp_listen.py --seconds 30                              # verify receipt
```

Verified end to end on replay: 146 datagrams, **0 sequence gaps**, ~805 bytes average, with
counters accumulating correctly.

## 7. Layout

| Path | Purpose |
|---|---|
| `proto/LiveMotion.proto` | reconstructed D-BOX LiveMotion schema |
| `scapi/server.py` | the shim gRPC server (both RPCs) + UDP publishing |
| `scapi/events.py` | event schema registry + `EventField` decoding |
| `scapi/mapping.py` | event/meaning -> named properties, with confidence |
| `scapi/publish.py` | UDP JSON publisher + frame aggregator |
| `simhub/ScApiPlugin.cs` | SimHub plugin (properties + ShakeIt events) |
| `simhub/README.md` | plugin build, properties, ShakeIt wiring |
| `vendor/livemotion-connector/` | where to keep your own copy of the connector, so D-BOX can be uninstalled (nothing of D-BOX's is committed) |
| `native/dbxlive_stub.cpp` | Option B probe -- built and tested, **never to be used** |
| `tools/extract_descriptor.py` | extract + verify the schema from `MotionEngine.exe` |
| `tools/patch_connector.py` | provision / redirect / restore the connector |
| `tools/selftest.py` | end-to-end client exercising the full lifecycle |
| `tools/identify.py` | query a LiveMotion server's `GetIdentification` |
| `tools/analyze_capture.py` | summarize a capture: events, rates, field ranges, type map |
| `tools/replay_capture.py` | replay a capture through mapping + publisher |
| `tools/udp_listen.py` | inspect the UDP stream without SimHub |
| `tools/find_vtable.py` | recover a C++ vtable from MSVC RTTI |
| `tools/install_shakeit_effects.py` | install/remove the ShakeIt Motors effects |
| `simhub/refs/` | signature-only SimHub facades, so CI can build the plugin |
| `packaging/sc-api.spec` | PyInstaller spec for the standalone `sc-api.exe` |
| `docs/MANUAL.md` | the user-facing manual |
| `LICENSE.md` | PolyForm Noncommercial 1.0.0 |
| `scripts/make_cert.sh` | generate the `CN=LiveMotionComm` TLS identity |
| `scripts/build_api.ps1` | build (and verify) `sc-api.exe` |
| `scripts/build_simhub_plugin.ps1` | build/install the SimHub plugin |
| `scripts/build_simhub_refs.ps1` | build the SimHub reference facades |
| `scripts/verify_simhub_plugin.ps1` | prove the built plugin binds to real SimHub |
| `scripts/build_stub.ps1` | build the Option B probe (not for use in-game) |
| `.github/workflows/build.yml` | CI: both artifacts, and the release bundle on a tag |

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE.md). **Noncommercial use only** — personal use,
hobby projects, research, education and charity are all permitted, including modifying it
and sharing your changes. Commercial use is not: that covers selling it, bundling it into
a paid product or service, and use in the operations of a for-profit organisation. If you
want to do something commercial with it, ask first.

Note that this restricts *this code*, not the knowledge in this document. The protocol
description below is a set of facts about how existing software behaves; anyone is free to
read it and write their own implementation.

## Legal

Interoperability research against a locally installed, licensed D-BOX runtime, so that
hardware the owner already has can consume telemetry the game already produces.

**No D-BOX code, configuration or key material is redistributed.** `dbxLive64.dll` is
D-BOX's own signed software and is supplied by each user from their own D-BOX HaptiSync
Center installation — it is absent from this repository and from every release build, as
are their config files and their certificate. `proto/LiveMotion.proto` is an interface
description recovered for compatibility purposes, and `simhub/refs/*.cs` are
signature-only facades of SimHub's public API for the same reason: both describe
interfaces, and neither carries any implementation.

Nothing here modifies, injects into, or reads memory from Star Citizen. `sc-api` is a
network server the game connects to of its own accord.

D-BOX, HaptiSync, SimHub, ShakeIt and Star Citizen are trademarks of their respective
owners, and this project is not affiliated with or endorsed by any of them.
