"""Install sc-api haptic effects into SimHub's ShakeIt Motors profile.

SimHub must be CLOSED: it holds its settings in memory and flushes them on exit,
so edits made while it runs are silently overwritten.

The effect definitions in EFFECTS below are the thing to tune. Amplitudes are
grounded in the recorded capture rather than guessed:

    property              typical        max      scaling used
    sc.vehicle.speed      p95 123       201.0     / 2      -> 0..100
    sc.weapon.recoil.y    p99 0.033     0.0735    * 1360   -> 0..100
    sc.impact.vector      (2 samples)   12.3      * 8      -> 0..100
    angular_velocity      p95 43        350.2     / 3.5    -> 0..100

Note recoil is ZERO about 95% of the time -- it is a sparse, bursty signal, so
it is scaled against its maximum, not a percentile. Scaling a sparse signal by
p95 would divide by zero.

Discrete happenings use `sc.pulse.*` properties from the plugin, which ramp
100 -> 0 over ~100-260 ms. ShakeIt custom effects are driven by formulas, and
the raw counters are monotonic (useless in a formula), so the pulse is what
makes footsteps/impacts usable as amplitude.

Formulas use NCalc, which SimHub ships -- `Abs`, `Max`, `Min`, `Sqrt`, `Pow`
are all available. Property references need the `ScApiPlugin.` prefix.

Usage:
    python tools/install_shakeit_effects.py --status
    python tools/install_shakeit_effects.py --install
    python tools/install_shakeit_effects.py --remove     # take ours back out
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import shutil
import sys
import uuid

ROOT = pathlib.Path(__file__).resolve().parent.parent
SETTINGS = pathlib.Path(
    r"C:\Program Files (x86)\SimHub\PluginsData\Common\ShakeITMotorsSettingsV2.json")
BACKUP_DIR = ROOT / "simhub" / "profile-backups"

# Every container we add carries this prefix, so --remove and re---install are
# exact and never touch the user's own effects.
TAG = "sc-api: "

P = "ScApiPlugin."   # SimHub prefixes plugin properties with the plugin name


def gamma(value=1.0, threshold=0, input_gain=100.0, minimum=0):
    return {
        "GammaValue": value,
        "InputGain": input_gain,
        "MinimumForce": minimum,
        "Threshold": threshold,
        "FilterType": "GammaFilter",
    }


# Four-corner formulas: (front_left, front_right, rear_left, rear_right).
# An empty string means "do not drive this corner".
EFFECTS = [
    {
        "name": "Ship speed",
        "enabled": True,
        "gain": 35.0,
        # Continuous: more rumble the faster you go. Gamma 2.0 keeps low
        # speeds quiet so it builds as you accelerate rather than being a
        # constant buzz from the moment you lift off.
        "corners": (f"[{P}sc.vehicle.speed] / 2",) * 4,
        "filter": gamma(2.0, threshold=2),
        "note": "continuous; speed 0-201 m/s scaled to 0-100",
    },
    {
        "name": "Weapon fire / recoil",
        "enabled": True,
        "gain": 100.0,
        # The game sends a real decay envelope at ~48 Hz, so drive amplitude
        # from it directly instead of synthesising a pulse. Front-biased: the
        # kick belongs under the shoulders/chest, not the thighs.
        #
        # Max() with the pulse guarantees a felt hit even if a recoil frame is
        # missed between publishes -- the envelope gives the texture, the pulse
        # guarantees the onset.
        "corners": (
            f"Max(Abs([{P}sc.weapon.recoil.y]) * 1360, [{P}sc.pulse.shot])",
            f"Max(Abs([{P}sc.weapon.recoil.y]) * 1360, [{P}sc.pulse.shot])",
            f"Abs([{P}sc.weapon.recoil.y]) * 680",
            f"Abs([{P}sc.weapon.recoil.y]) * 680",
        ),
        "filter": gamma(1.0),   # linear: preserve the game's own envelope
        "note": "FPS recoil confirmed; ship weapons NOT yet confirmed in data",
    },
    {
        "name": "Impacts / taking hits",
        "enabled": True,
        "gain": 100.0,
        "corners": (f"[{P}sc.pulse.impact]",) * 4,
        "filter": gamma(1.0),
        "note": "only 2 samples in the capture, both from the crash -- "
                "'being shot at' is UNCONFIRMED and may be a different event",
    },
]

# Deliberately not installed: Footsteps, Heartbeat and Maneuvering, which the
# user tried and disabled. They remain available in git history, and the
# properties (sc.pulse.footstep, sc.pulse.heartbeat, angular_velocity.*) are
# still published, so they can be re-added by hand in SimHub at any time.


def container(spec: dict) -> dict:
    """Build one CustomEffectContainer, matching SimHub's own schema exactly."""
    fl, fr, rl, rr = spec["corners"]
    return {
        "ContainerType": "CustomEffectContainer",
        "IsEnabled": spec["enabled"],
        "Gain": spec["gain"],
        "Description": TAG + spec["name"],
        "FrontLeftFormula": {"Expression": fl},
        "FrontRightFormula": {"Expression": fr},
        "RearLeftFormula": {"Expression": rl},
        "RearRightFormula": {"Expression": rr},
        "ForceFrequencies": False,
        "FrontLeftFrequencyFormula": {"Expression": ""},
        "FrontRightFrequencyFormula": {"Expression": ""},
        "RearLeftFrequencyFormula": {"Expression": ""},
        "RearRightFrequencyFormula": {"Expression": ""},
        # The "process effect when game is not running" checkbox. Essential:
        # Star Citizen is not a SimHub-supported game, so without this nothing
        # ever runs.
        "AlwaysExecute": True,
        "ContainerId": str(uuid.uuid4()),
        "AggregationMode": "Mono",
        "Filter": spec["filter"],
        "Output": {"OutputType": "VibrationOutput"},
    }


def simhub_running() -> bool:
    try:
        import subprocess
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq SimHubWPF.exe"],
            capture_output=True, text=True, timeout=20)
        return "SimHubWPF" in out.stdout
    except Exception:
        return False


def load() -> tuple[dict, str]:
    raw = SETTINGS.read_text(encoding="utf8")
    return json.loads(raw), raw


def save(data: dict, raw: str) -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = BACKUP_DIR / f"ShakeITMotorsSettingsV2.{stamp}.json"
    bak.write_text(raw, encoding="utf8")
    print(f"backed up original -> {bak}")

    # Match SimHub's own formatting so a diff stays readable.
    indent = 2 if '\n  "' in raw or '\n    "' in raw else None
    SETTINGS.write_text(
        json.dumps(data, indent=indent, ensure_ascii=False), encoding="utf8")
    print(f"wrote {SETTINGS}")


def active_profile(data: dict) -> dict:
    wanted = data.get("activeProfileId")
    profs = data.get("Profiles", [])
    if not profs:
        raise SystemExit("no profiles in the ShakeIt settings")
    for p in profs:
        if p.get("ProfileId") == wanted:
            return p
    return profs[0]


def status() -> int:
    if not SETTINGS.exists():
        print(f"not found: {SETTINGS}")
        return 2
    data, _ = load()
    prof = active_profile(data)
    print(f"profile  : {prof.get('Name')!r}  ({len(prof['EffectsContainers'])} containers)")
    print(f"global   : gain={data.get('GlobalGain')} muted={data.get('IsMuted')}")
    print(f"simhub   : {'RUNNING (close it before --install)' if simhub_running() else 'not running'}")
    print("\ncontainers mentioning sc-api / ScApiPlugin:")
    found = 0
    for i, c in enumerate(prof["EffectsContainers"]):
        blob = json.dumps(c)
        if "ScApiPlugin" in blob or (c.get("Description") or "").startswith(TAG):
            found += 1
            corners = [c.get(k, {}).get("Expression", "")
                       for k in ("FrontLeftFormula", "FrontRightFormula",
                                 "RearLeftFormula", "RearRightFormula")]
            live = sum(1 for x in corners if x)
            print(f"  [{i:>2}] {c.get('Description')!r} enabled={c.get('IsEnabled')} "
                  f"gain={c.get('Gain')} corners_driven={live}")
    if not found:
        print("  (none)")
    return 0


def install() -> int:
    if simhub_running():
        print("SimHub is running -- close it first, or your edits will be "
              "overwritten when it exits.", file=sys.stderr)
        return 1
    if not SETTINGS.exists():
        print(f"not found: {SETTINGS}", file=sys.stderr)
        return 2

    data, raw = load()
    prof = active_profile(data)
    cons = prof["EffectsContainers"]

    # Idempotent: drop any previous sc-api containers first.
    before = len(cons)
    cons[:] = [c for c in cons
               if not (c.get("Description") or "").startswith(TAG)]
    removed = before - len(cons)
    if removed:
        print(f"replacing {removed} existing sc-api effect(s)")

    # The user's own test effect feeds RAW speed (0-201) into a 0-100 scale, so
    # it saturates permanently and would fight our properly scaled version.
    # Disable rather than delete, so it is trivially recoverable.
    for c in cons:
        desc = (c.get("Description") or "")
        if "star citizen" in desc.lower() and c.get("IsEnabled"):
            c["IsEnabled"] = False
            print(f"disabled your effect {desc!r} (unscaled speed would "
                  f"saturate; re-enable any time)")

    for spec in EFFECTS:
        cons.append(container(spec))
        state = "on " if spec["enabled"] else "off"
        print(f"  + [{state}] {TAG}{spec['name']:<32} gain={spec['gain']:<6} "
              f"-- {spec['note']}")

    save(data, raw)
    print(f"\nprofile {prof.get('Name')!r} now has {len(cons)} containers")
    print("Start SimHub, then replay or play to feel it:")
    print("  python tools/replay_capture.py captures/sc-capture.jsonl --speed 1 --skip 1100")
    return 0


def remove() -> int:
    if simhub_running():
        print("SimHub is running -- close it first.", file=sys.stderr)
        return 1
    data, raw = load()
    prof = active_profile(data)
    cons = prof["EffectsContainers"]
    before = len(cons)
    cons[:] = [c for c in cons
               if not (c.get("Description") or "").startswith(TAG)]
    gone = before - len(cons)
    if not gone:
        print("no sc-api effects present")
        return 0
    save(data, raw)
    print(f"removed {gone} sc-api effect(s); your own effects untouched")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--install", action="store_true")
    g.add_argument("--remove", action="store_true")
    args = ap.parse_args(argv)
    if args.status:
        return status()
    if args.install:
        return install()
    return remove()


if __name__ == "__main__":
    raise SystemExit(main())
