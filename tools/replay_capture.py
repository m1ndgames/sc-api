"""Replay a recorded capture through the mapping and UDP publisher.

This is how the output side and the SimHub plugin get developed and tested
WITHOUT Star Citizen running: `captures/sc-capture.jsonl` holds 567k real
records, so the whole downstream path can be exercised offline and repeatably.

Usage:
    # list what the capture would produce, without sending anything
    python tools/replay_capture.py captures/sc-capture.jsonl --dry-run

    # replay at original wall-clock speed to 127.0.0.1:51680
    python tools/replay_capture.py captures/sc-capture.jsonl

    # replay fast, 20x, from 10 minutes in
    python tools/replay_capture.py captures/sc-capture.jsonl --speed 20 --skip 600
"""
from __future__ import annotations

import argparse
import collections
import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from scapi.mapping import EVENT_NAMES, map_event  # noqa: E402
from scapi.publish import Aggregator, UdpPublisher  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=pathlib.Path)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51680)
    ap.add_argument("--hz", type=float, default=60.0)
    ap.add_argument("--speed", type=float, default=1.0,
                    help="playback rate multiplier (0 = as fast as possible)")
    ap.add_argument("--skip", type=float, default=0.0,
                    help="skip this many seconds of capture time")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after N posted events")
    ap.add_argument("--dry-run", action="store_true",
                    help="summarise the properties produced; send nothing")
    ap.add_argument("--allow-multiple", action="store_true",
                    help="permit a second concurrent replay (interleaves event "
                         "counters; normally a mistake)")
    args = ap.parse_args(argv)

    if not args.capture.exists():
        print(f"not found: {args.capture}", file=sys.stderr)
        return 2

    # Single-instance guard. Two replays publishing to the same port interleave
    # their independent event counters, which makes a consumer see constant
    # spurious events -- exactly the "impacts rumble forever" symptom. The
    # consumer now detects this, but preventing it is better than coping.
    guard = None
    if not args.dry_run:
        import socket as _s
        guard = _s.socket(_s.AF_INET, _s.SOCK_DGRAM)
        try:
            guard.bind(("127.0.0.1", args.port + 1))
        except OSError:
            print(f"ERROR: another replay already appears to be publishing to "
                  f"port {args.port}.", file=sys.stderr)
            print("Stop it first, or pass --allow-multiple if you really want "
                  "two streams.", file=sys.stderr)
            if not args.allow_multiple:
                return 1
            print("  (continuing anyway: --allow-multiple)", file=sys.stderr)
            guard = None

    pub = agg = None
    if not args.dry_run:
        pub = UdpPublisher(args.host, args.port)
        agg = Aggregator(pub, hz=args.hz)
        print(f"replaying -> udp://{args.host}:{args.port} "
              f"(speed x{args.speed or 'max'}, {args.hz:.0f} Hz)")

    props_seen: dict[str, int] = collections.Counter()
    per_event: dict[int, int] = collections.Counter()
    samples: dict[str, object] = {}
    n = 0
    t0_cap = None
    t0_wall = time.monotonic()

    with args.capture.open(encoding="utf8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not rec.get("kind", "").startswith("post_event"):
                continue

            ts = rec.get("t")
            if t0_cap is None:
                t0_cap = ts
            offset = (ts - t0_cap) if (ts and t0_cap) else 0.0
            if offset < args.skip:
                continue

            props = map_event(rec)
            per_event[rec["eventKey"]] += 1
            for k, v in props.items():
                props_seen[k] += 1
                samples.setdefault(k, v)

            if agg is not None:
                agg.update(props)
                if args.speed > 0:
                    target = (offset - args.skip) / args.speed
                    drift = target - (time.monotonic() - t0_wall)
                    if drift > 0:
                        time.sleep(min(drift, 0.25))

            n += 1
            if args.limit and n >= args.limit:
                break

    if agg is not None:
        agg.maybe_flush(force=True)

    print(f"\nreplayed {n} posted events")
    print("\nevents seen:")
    for k in sorted(per_event):
        print(f"  event {k:>2} ({EVENT_NAMES.get(k, '?')}): {per_event[k]}")

    print(f"\n{len(props_seen)} distinct properties produced:")
    named = sorted(k for k in props_seen if not k.startswith("sc.raw."))
    raw = sorted(k for k in props_seen if k.startswith("sc.raw."))
    for k in named:
        v = samples[k]
        if isinstance(v, float):
            v = round(v, 4)
        print(f"  {k:<36} n={props_seen[k]:<8} e.g. {v}")
    print(f"  ... plus {len(raw)} unmapped raw properties")
    if raw:
        print("    " + ", ".join(raw[:8]) + (" ..." if len(raw) > 8 else ""))

    if pub is not None:
        print(f"\ndatagrams sent: {pub.sent} (flushes {agg.flushes}, "
              f"send errors {pub.dropped})")
        pub.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
