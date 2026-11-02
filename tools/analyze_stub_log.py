"""Derive the native vtable slot -> method mapping from a stub-DLL capture.

Reads C:\\ProgramData\\sc-api\\dbxlive_stub.log (written by native/dbxlive_stub.cpp)
and correlates the observed slot call sequence against the known-good gRPC call
sequence from captures/sc-capture.jsonl.

The correlation works because the gRPC capture already told us exactly what the
game does, in order:

    Initialize -> Open -> RegisterEvent x20 -> Start -> PostEvent (x many)
    ... and on shutdown: Stop -> Close -> Terminate

So the slot called exactly once at the very start is Initialize, the slot called
exactly 20 times in a burst is RegisterEvent, the high-frequency slot is
PostEvent, and so on. That is ground truth rather than inference from
declaration order.

Usage:
    python tools/analyze_stub_log.py
    python tools/analyze_stub_log.py --log path\\to\\dbxlive_stub.log
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re

DEFAULT_LOG = pathlib.Path(r"C:\ProgramData\sc-api\dbxlive_stub.log")

CALL = re.compile(
    r"^\[(\d+)\] CALL slot=(\d+) \(\w+\) call#(\d+)\s+"
    r"a1=0x([0-9a-f]+) a2=0x([0-9a-f]+) a3=0x([0-9a-f]+)")
DUMP = re.compile(r"^\s+(a\d) -> ((?:[0-9a-f]{2} )+)")
MODULE = re.compile(r"^\s+module : (.+)$")
HOST = re.compile(r"^\s+host   : (.+?)\s+pid=(\d+)")
TOTAL = re.compile(r"^\s+slot(\d+)\s+calls=(\d+)")

# What the gRPC capture observed, in order. 20 RegisterEvent calls is the
# distinctive fingerprint.
EXPECTED_REGISTRATIONS = 20


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log", type=pathlib.Path, default=DEFAULT_LOG)
    ap.add_argument("--dumps", action="store_true",
                    help="also print the recorded argument memory dumps")
    args = ap.parse_args(argv)

    if not args.log.exists():
        print(f"no stub log at {args.log}")
        print("Has the game been launched since installing the stub?")
        print("Check the connector with: python tools/patch_connector.py --status")
        return 2

    text = args.log.read_text(encoding="utf8", errors="replace")
    lines = text.splitlines()

    modules, hosts, loads = [], [], 0
    calls = []            # (seq, slot, n, a1, a2, a3)
    totals = {}
    dumps = collections.defaultdict(list)   # slot -> [(which, hexbytes)]
    last_slot = None

    for ln in lines:
        if ln.startswith("=== sc-api dbxLive stub loaded"):
            loads += 1
        if m := MODULE.match(ln):
            modules.append(m.group(1))
        if m := HOST.match(ln):
            hosts.append((m.group(1), m.group(2)))
        if m := CALL.match(ln):
            seq, slot, n = int(m.group(1)), int(m.group(2)), int(m.group(3))
            calls.append((seq, slot, n,
                          int(m.group(4), 16), int(m.group(5), 16),
                          int(m.group(6), 16)))
            last_slot = slot
        elif m := DUMP.match(ln):
            if last_slot is not None:
                dumps[last_slot].append((m.group(1), m.group(2).strip()))
        if m := TOTAL.match(ln):
            totals[int(m.group(1))] = int(m.group(2))

    print(f"log        : {args.log} ({len(text)} bytes)")
    print(f"loads      : {loads}")
    for mod in dict.fromkeys(modules):
        print(f"  module   : {mod}")
    for h, pid in dict.fromkeys(hosts):
        print(f"  host     : {h} (pid {pid})")

    if not calls:
        print("\nNo slot calls recorded. The DLL loaded but the game never "
              "called into it, or it loaded from a different directory.")
        return 1

    # Observed counts. Prefer the detach totals (exact); fall back to what we
    # saw, which is capped by kVerboseCallsPerSlot.
    seen = collections.Counter(s for _, s, *_ in calls)
    counts = totals or dict(seen)
    exact = bool(totals)

    print(f"\nslot calls ({'exact, from detach totals' if exact else 'lower bound: detail logging is rate-limited'}):")
    first_seq = {}
    for seq, slot, n, *_ in calls:
        first_seq.setdefault(slot, seq)
    print(f"  {'slot':>4} {'count':>9} {'first seen':>11}")
    for slot in sorted(counts):
        print(f"  {slot:>4} {counts[slot]:>9} {first_seq.get(slot, '-'):>11}")

    print("\norder of first call:")
    print("  " + " -> ".join(f"slot{s}" for s in sorted(first_seq, key=first_seq.get)))

    # --- correlation -------------------------------------------------------
    print("\ninferred mapping (from the gRPC capture's known sequence):")
    order = sorted(first_seq, key=first_seq.get)
    guesses: dict[int, str] = {}

    reg = [s for s, c in counts.items() if c == EXPECTED_REGISTRATIONS]
    if len(reg) == 1:
        guesses[reg[0]] = "RegisterEvent"
    elif reg:
        print(f"  ! several slots called exactly {EXPECTED_REGISTRATIONS}x: {reg}")

    if counts:
        hottest = max(counts, key=lambda s: counts[s])
        if counts[hottest] > 1000:
            guesses[hottest] = "PostEvent / PostEvents (high frequency)"

    if order:
        guesses.setdefault(order[0], "Initialize (first call)")

    once = [s for s, c in counts.items() if c == 1 and s not in guesses]
    if once:
        print(f"  slots called exactly once (Open/Start/Stop/Close/Terminate "
              f"candidates, resolve by order): {sorted(once)}")

    for slot in sorted(guesses):
        print(f"  slot{slot:<3} = {guesses[slot]}")
    if not guesses:
        print("  (not enough signal; play longer or check the expected counts)")

    print("\nNOTE: a2/a3 are only meaningful for slots that really take that "
          "many arguments;\n      unused argument registers contain garbage, "
          "and float args are not captured.")

    if args.dumps:
        print("\nrecorded argument memory:")
        for slot in sorted(dumps):
            print(f"  slot{slot}:")
            for which, hx in dumps[slot][:4]:
                print(f"    {which}: {hx}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
