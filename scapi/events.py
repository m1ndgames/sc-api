"""Event schema registry and payload decoding for the D-BOX LiveMotion protocol.

The protocol is self-describing: a client sends `register_event` with an
`EventDefinition` before posting any instance of that event. This module keeps
those definitions and uses them to turn raw `EventField` unions into readable
records.

`FieldDefinition.type` is the SDK's own type enum, absent from the schema. The
mapping below was derived empirically by correlating declared types against the
`EventField` member Star Citizen actually populated over ~18k captured events.

This matters for correctness, not just cosmetics: proto3 does not transmit a
scalar equal to zero, so a field holding 0.0 is wire-identical to an absent
one. Without the type table a zero reads as "no value", which would silently
drop every idle/neutral sample. With it, we know a value was meant to be there
and report it as 0.

`eventMeaning` and `FieldDefinition.meaning` remain opaque SDK constants,
recorded verbatim so a mapping to Star Citizen's human event names
("DBOX Footstep", "DBOX Recoil Frame", ...) can be built from observation.
"""
from __future__ import annotations

import threading
from typing import Any

# The scalar members of EventField, in field-number order. Exactly one is
# normally set per field; proto3 gives no presence for scalars, so a
# non-default value is the only available signal.
_SCALARS = (
    ("dataInt32", "i32"),
    ("dataInt64", "i64"),
    ("dataFloat32", "f32"),
    ("dataFloat64", "f64"),
    ("dataString", "str"),
    ("dataWideString", "wstr"),
)

# The submessage members, which DO have presence.
_MESSAGES = (
    ("dataFloat32XYZ", "xyz32", ("x", "y", "z")),
    ("dataFloat64XYZ", "xyz64", ("x", "y", "z")),
    ("dataQuadInt32", "quadi32", ("frontLeft", "frontRight", "backLeft", "backRight")),
    ("dataQuadFloat32", "quadf32", ("frontLeft", "frontRight", "backLeft", "backRight")),
    ("dataQuadFloat64", "quadf64", ("frontLeft", "frontRight", "backLeft", "backRight")),
)


# FieldDefinition.type -> (EventField member, short kind name).
#
# CONFIRMED by observation against Star Citizen:
#   23  (0x17) int32          -- confirmed
#   25  (0x19) float32        -- confirmed
#   96  (0x60) string         -- confirmed (carried 'ANVL_Hornet_F7C_Mk2')
#   137 (0x89) float32 XYZ    -- confirmed
#
# INFERRED, not yet observed carrying data:
#   153 (0x99) float32 Quad. The encoding looks like (shape << 4) | base, with
#   base 7=int32 / 9=float32 and shape 1=scalar / 8=XYZ / 9=Quad. Event 18 is
#   the only one declaring it, and it stays silent unless a GROUND vehicle is
#   used -- consistent with the `dbox_gv_wheels` string in StarCitizen.exe,
#   i.e. Quad is per-wheel (frontLeft/frontRight/backLeft/backRight) data.
#
# Anything unmapped falls back to inspecting which member is populated.
FIELD_TYPES: dict[int, tuple[str, str]] = {
    23: ("dataInt32", "i32"),
    25: ("dataFloat32", "f32"),
    96: ("dataString", "str"),
    137: ("dataFloat32XYZ", "xyz32"),
    # inferred:
    153: ("dataQuadFloat32", "quadf32"),
}

_VECTOR_MEMBERS = {
    "dataFloat32XYZ": ("x", "y", "z"),
    "dataFloat64XYZ": ("x", "y", "z"),
    "dataQuadInt32": ("frontLeft", "frontRight", "backLeft", "backRight"),
    "dataQuadFloat32": ("frontLeft", "frontRight", "backLeft", "backRight"),
    "dataQuadFloat64": ("frontLeft", "frontRight", "backLeft", "backRight"),
}


def decode_typed(field, declared_type: int) -> dict[str, Any] | None:
    """Decode using the declared FieldDefinition.type, if we know it.

    Returns None when the type is unmapped, so the caller can fall back to
    inspecting the populated member.
    """
    entry = FIELD_TYPES.get(declared_type)
    if entry is None:
        return None
    name, kind = entry
    members = _VECTOR_MEMBERS.get(name)
    if members:
        sub = getattr(field, name)
        return {"kind": kind, "value": {m: getattr(sub, m) for m in members},
                "type": declared_type}
    # Scalar: read it directly. A zero here is a real zero, not an absence.
    return {"kind": kind, "value": getattr(field, name), "type": declared_type}


def decode_field(field) -> dict[str, Any]:
    """Return {kind, value} for whichever EventField member is populated.

    Fallback for fields whose declared type we do not know. Submessages are
    checked first because they carry real presence; a set-but-all-zero vector
    would otherwise be indistinguishable from "unset".
    """
    for name, kind, members in _MESSAGES:
        if field.HasField(name):
            sub = getattr(field, name)
            return {"kind": kind, "value": {m: getattr(sub, m) for m in members}}

    for name, kind in _SCALARS:
        val = getattr(field, name)
        if val:  # non-default
            if kind == "wstr":
                # dataWideString is bytes; UTF-16-LE is the natural reading on
                # Windows, but this is an assumption -- keep the raw bytes too.
                try:
                    return {"kind": kind, "value": val.decode("utf-16-le").rstrip("\x00"),
                            "raw": val.hex()}
                except UnicodeDecodeError:
                    return {"kind": kind, "value": None, "raw": val.hex()}
            return {"kind": kind, "value": val}

    return {"kind": "empty", "value": None}


class EventRegistry:
    """Thread-safe store of eventKey -> EventDefinition, plus decoding."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._defs: dict[int, Any] = {}
        self._counts: dict[int, int] = {}

    # -- registration ----------------------------------------------------

    def register(self, definition) -> dict[str, Any]:
        """Record an EventDefinition. Returns a summary for logging."""
        with self._lock:
            self._defs[definition.eventKey] = definition
        return {
            "eventKey": definition.eventKey,
            "eventMeaning": definition.eventMeaning,
            "fieldCount": definition.fieldCount,
            "fields": [
                {
                    "idx": i,
                    "type": fd.type,
                    "flags": fd.flags,
                    "meaning": fd.meaning,
                    "offset": fd.offset,
                }
                for i, fd in enumerate(definition.fieldDef)
            ],
        }

    def definition(self, event_key: int):
        with self._lock:
            return self._defs.get(event_key)

    @property
    def known_keys(self) -> list[int]:
        with self._lock:
            return sorted(self._defs)

    def summary(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "eventKey": k,
                    "eventMeaning": d.eventMeaning,
                    "fieldCount": d.fieldCount,
                    "posted": self._counts.get(k, 0),
                }
                for k, d in sorted(self._defs.items())
            ]

    # -- decoding --------------------------------------------------------

    def decode(self, event_info) -> dict[str, Any]:
        """Decode an EventInfo into a readable record.

        `meaning` codes from the registered definition are attached per field
        where available, so the log carries both the value and its (opaque)
        semantic tag.
        """
        key = event_info.eventKey
        with self._lock:
            self._counts[key] = self._counts.get(key, 0) + 1
            definition = self._defs.get(key)

        field_meanings: list[int] = []
        field_types: list[int] = []
        if definition is not None:
            field_meanings = [fd.meaning for fd in definition.fieldDef]
            field_types = [fd.type for fd in definition.fieldDef]

        instances = []
        for data in event_info.eventData:
            decoded = []
            for i, f in enumerate(data.eventField):
                entry = None
                if i < len(field_types):
                    entry = decode_typed(f, field_types[i])
                if entry is None:
                    entry = decode_field(f)
                    if i < len(field_types):
                        entry["type"] = field_types[i]
                if i < len(field_meanings):
                    entry["meaning"] = field_meanings[i]
                decoded.append(entry)
            instances.append(decoded)

        return {
            "eventKey": key,
            "registered": definition is not None,
            "eventMeaning": definition.eventMeaning if definition else None,
            "size": event_info.size,
            "count": event_info.count,
            "instances": instances,
        }
