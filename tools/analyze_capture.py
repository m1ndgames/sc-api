"""Summarize a capture produced by `scapi.server --jsonl`.

Reports, per registered event: declared field types/meanings, how many instances
were posted, the observed rate, and per-field value ranges. Also cross-tabulates
`FieldDefinition.type` against the `EventField` member actually populated, which
is how the SDK's type enum was mapped in the first place.

Usage:
    python tools/analyze_capture.py captures/sc-capture.jsonl
    python tools/analyze_capture.py captures/sc-capture.jsonl --event 3
    python tools/analyze_capture.py captures/sc-capture.jsonl --types
"""
from __future__ import annotations

import argparse
import collections
import json
import math
import pathlib


def load(path: pathlib.Path):
    regs: dict[int, dict] = {}
    posts: dict[int, list] = collections.defaultdict(list)
    session = collections.Counter()
    for line in path.open(encoding="utf8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            # A capture can be truncated if the server was killed mid-write.
            continue
        kind = r.get("kind", "?")
        session[kind] += 1
        if kind == "register_event":
            regs[r["eventKey"]] = r
        elif kind.startswith("post_event"):
            posts[r["eventKey"]].append(r)
    return regs, posts, session


def field_stats(rows: list[dict]) -> dict:
    stats: dict = collections.defaultdict(
        lambda: {"kind": None, "type": None, "n": 0,
                 "mn": math.inf, "mx": -math.inf, "peak": 0.0, "nonzero": 0}
    )
    for r in rows:
        for inst in r.get("instances", []):
            for idx, f in enumerate(inst):
                key = (idx, f.get("meaning"))
                s = stats[key]
                s["kind"] = f.get("kind")
                s["type"] = f.get("type")
                s["n"] += 1
                v = f.get("value")
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    s["mn"] = min(s["mn"], v)
                    s["mx"] = max(s["mx"], v)
                    if v:
                        s["nonzero"] += 1
                elif isinstance(v, dict):
                    nums = [x for x in v.values() if isinstance(x, (int, float))]
                    if nums:
                        s["mn"] = min(s["mn"], min(nums))
                        s["mx"] = max(s["mx"], max(nums))
                        s["peak"] = max(s["peak"], math.sqrt(sum(x * x for x in nums)))
                        if any(nums):
                            s["nonzero"] += 1
                elif isinstance(v, str) and v:
                    s["nonzero"] += 1
    return stats


def rate(rows: list[dict]) -> str:
    ts = [r["t"] for r in rows if "t" in r]
    if len(ts) < 3:
        return ""
    gaps = sorted(b - a for a, b in zip(ts, ts[1:]))
    med = gaps[len(gaps) // 2]
    if med <= 0:
        return f"span={ts[-1] - ts[0]:.1f}s"
    return f"span={ts[-1] - ts[0]:.1f}s ~{1 / med:.0f}Hz"


def fmt(v) -> str:
    if v in (math.inf, -math.inf):
        return ""
    return f"{v:.4f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("capture", type=pathlib.Path)
    ap.add_argument("--event", type=int, action="append",
                    help="show per-field detail for this eventKey (repeatable)")
    ap.add_argument("--types", action="store_true",
                    help="cross-tabulate declared type vs populated member")
    args = ap.parse_args(argv)

    if not args.capture.exists():
        print(f"not found: {args.capture}")
        return 2

    regs, posts, session = load(args.capture)
    total = sum(session.values())
    print(f"capture : {args.capture}  ({total} records)")
    print(f"session : {dict(session)}")
    sessions = session.get("initialize", 0)
    if sessions > 1:
        print(f"note    : {sessions} initialize records -- the client reconnected "
              f"{sessions - 1} time(s)")

    print(f"\n{'key':>4} {'emean':>5} {'flds':>4} {'posted':>9}  {'timing':<22} types")
    for k in sorted(regs):
        r = regs[k]
        rows = posts.get(k, [])
        types = sorted({f["type"] for f in r["fields"]})
        tag = "" if rows else "   (silent)"
        print(f"{k:>4} {r['eventMeaning']:>5} {r['fieldCount']:>4} {len(rows):>9}  "
              f"{rate(rows):<22} {types}{tag}")

    if args.types:
        obs: dict[int, collections.Counter] = collections.defaultdict(collections.Counter)
        for k, rows in posts.items():
            declared = {f["idx"]: f["type"] for f in regs.get(k, {}).get("fields", [])}
            for r in rows:
                for inst in r.get("instances", []):
                    for i, f in enumerate(inst):
                        t = declared.get(i)
                        if t is not None:
                            obs[t][f["kind"]] += 1
        print("\ndeclared FieldDefinition.type -> populated EventField member")
        print("('empty' means proto3 omitted a zero-valued scalar, not missing data)")
        for t in sorted(obs):
            print(f"  type {t:>3} (0x{t:02x}): {dict(obs[t])}")

    for k in args.event or []:
        rows = posts.get(k, [])
        r = regs.get(k)
        print(f"\n=== eventKey {k} "
              f"(eventMeaning {r['eventMeaning'] if r else '?'}, "
              f"{len(rows)} posts, {rate(rows)}) ===")
        if not rows:
            print("  no instances posted")
            continue
        st = field_stats(rows)
        print(f"  {'idx':>3} {'meaning':>7} {'type':>5} {'kind':>8} {'n':>7} "
              f"{'nonzero':>8}   {'min':>12} {'max':>12} {'peak|v|':>10}")
        for (idx, meaning), s in sorted(st.items()):
            print(f"  {idx:>3} {str(meaning):>7} {str(s['type']):>5} {s['kind']:>8} "
                  f"{s['n']:>7} {s['nonzero']:>8}   "
                  f"{fmt(s['mn']):>12} {fmt(s['mx']):>12} "
                  f"{(f'{s['peak']:.4f}' if s['peak'] else ''):>10}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
