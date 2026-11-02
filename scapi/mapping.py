"""Map raw D-BOX LiveMotion events onto named, flat telemetry properties.

The wire protocol identifies fields only by opaque SDK integers: an `eventKey`
plus a `FieldDefinition.meaning` per field. Those constants are not in the
schema, so the names below were derived by correlating a 567k-record capture
against known gameplay (see README "Observed behaviour").

Output is deliberately FLAT -- `{"name": number}` -- because that is what a
SimHub plugin can publish as properties and what ShakeIt expressions can read.

Confidence is tracked honestly and exposed in the output, so a consumer can
tell a solid reading from a guess:

    observed  the value's identity is directly evidenced
    strong    consistent with gameplay correlation, not proven
    inferred  plausible from range/shape alone
    unknown   emitted under a raw name only

Anything unmapped is still published, under `sc.raw.e<key>.m<meaning>[.x|y|z]`,
so no data is silently dropped while the mapping is incomplete.
"""
from __future__ import annotations

from typing import Any

OBSERVED, STRONG, INFERRED = "observed", "strong", "inferred"

# (eventKey, meaning) -> (property base name, confidence)
#
# Event identities, from the capture:
#   1  vehicle identity (string)        14  heartbeat pulse (~1 Hz)
#   2  enter/leave vehicle trigger      16  footstep (~6 Hz while walking)
#   3  vehicle/flight frame (~47 Hz)    13  weapon recoil (~48 Hz)
#   8  impact (fired on a crash)         9  decoupled toggle
#   12 actor state incl. heart rate      0/4/10 per-frame streams
FIELDS: dict[tuple[int, int], tuple[str, str]] = {
    # --- event 3: vehicle / flight frame -------------------------------
    (3, 94): ("sc.vehicle.speed", STRONG),            # 0 -> 200.997 m/s
    (3, 41): ("sc.vehicle.orientation", STRONG),      # bounded by +-pi
    (3, 40): ("sc.vehicle.angular_velocity", INFERRED),
    (3, 9):  ("sc.vehicle.velocity", INFERRED),
    (3, 38): ("sc.vehicle.accel", INFERRED),
    (3, 68): ("sc.vehicle.accel_alt", INFERRED),      # same range as m38
    (3, 34): ("sc.vehicle.vec34", INFERRED),
    (3, 35): ("sc.vehicle.vec35", INFERRED),
    (3, 39): ("sc.vehicle.vec39", INFERRED),

    # --- event 12: actor state -----------------------------------------
    # m47 carries the identical float as event 14 at the same instant.
    (12, 47): ("sc.actor.heart_rate", OBSERVED),

    # --- event 14: heartbeat pulse -------------------------------------
    # ~1 Hz, 2252 beats / 2320 s ~= 58 bpm.
    (14, 38): ("sc.actor.heartbeat_bpm", OBSERVED),

    # --- event 13: weapon recoil ---------------------------------------
    # m42 is non-zero only on the first frame of a burst: a shot trigger.
    (13, 42): ("sc.weapon.shot_fired", OBSERVED),
    (13, 38): ("sc.weapon.recoil", OBSERVED),
    (13, 39): ("sc.weapon.recoil_b", INFERRED),
    (13, 40): ("sc.weapon.recoil_c", INFERRED),
    (13, 41): ("sc.weapon.recoil_d", INFERRED),

    # --- event 9: decoupled --------------------------------------------
    (9, 38): ("sc.vehicle.decoupled", STRONG),

    # --- event 8: impact -----------------------------------------------
    (8, 38): ("sc.impact.vector", STRONG),
    (8, 39): ("sc.impact.a", INFERRED),
    (8, 40): ("sc.impact.b", INFERRED),
    (8, 41): ("sc.impact.c", INFERRED),

    # --- event 1: vehicle identity (string) ----------------------------
    (1, 30): ("sc.vehicle.name", OBSERVED),
}

# Events that are momentary: a consumer should treat them as a pulse that
# decays, not as a level that persists. Value is the pulse property name.
PULSE_EVENTS: dict[int, str] = {
    2:  "sc.vehicle.transition",      # enter/leave, no payload
    14: "sc.actor.heartbeat",         # discrete beat
    16: "sc.actor.footstep",          # discrete step
    8:  "sc.impact",                  # discrete impact
}

# Human labels for the events we are confident about, for logs/UI.
EVENT_NAMES: dict[int, str] = {
    1: "vehicle_identity",
    2: "vehicle_transition",
    3: "vehicle_frame",
    8: "impact",
    9: "decoupled_toggle",
    12: "actor_state",
    13: "weapon_recoil",
    14: "heartbeat",
    16: "footstep",
}

_VEC_KEYS = ("x", "y", "z")
_QUAD_KEYS = ("frontLeft", "frontRight", "backLeft", "backRight")
_QUAD_SHORT = {"frontLeft": "fl", "frontRight": "fr",
               "backLeft": "bl", "backRight": "br"}


def _emit(out: dict[str, Any], name: str, value: Any) -> None:
    """Flatten a decoded value into one or more scalar properties."""
    if isinstance(value, dict):
        if set(value) >= set(_QUAD_KEYS):
            for k in _QUAD_KEYS:
                out[f"{name}.{_QUAD_SHORT[k]}"] = value[k]
        else:
            for k in _VEC_KEYS:
                if k in value:
                    out[f"{name}.{k}"] = value[k]
    else:
        out[name] = value


def map_event(decoded: dict) -> dict[str, Any]:
    """Turn one decoded event (from EventRegistry.decode) into properties.

    `decoded` is the structure produced by scapi.events.EventRegistry.decode:
    {eventKey, instances: [[{kind, value, meaning, type}, ...]], ...}
    """
    key = decoded.get("eventKey")
    out: dict[str, Any] = {}

    if key in PULSE_EVENTS:
        # Published as a counter; a consumer differentiates it to get a pulse.
        out[PULSE_EVENTS[key] + ".count"] = decoded.get("_count", 1)

    for inst in decoded.get("instances", []):
        for field in inst:
            meaning = field.get("meaning")
            value = field.get("value")
            if value is None:
                continue
            named = FIELDS.get((key, meaning))
            if named:
                _emit(out, named[0], value)
            else:
                _emit(out, f"sc.raw.e{key}.m{meaning}", value)
    return out


def confidence_of(name: str) -> str:
    for base, conf in FIELDS.values():
        if name == base or name.startswith(base + "."):
            return conf
    return "unknown"


def describe() -> list[dict[str, str]]:
    """The mapping as a table, for documentation and for the SimHub UI."""
    rows = []
    for (key, meaning), (name, conf) in sorted(FIELDS.items()):
        rows.append({
            "event": str(key),
            "event_name": EVENT_NAMES.get(key, "?"),
            "meaning": str(meaning),
            "property": name,
            "confidence": conf,
        })
    return rows
