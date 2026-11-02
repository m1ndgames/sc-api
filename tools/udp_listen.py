"""Listen to sc-api's UDP telemetry stream -- a stand-in for the SimHub plugin.

Verifies the output path independently of SimHub, and doubles as the reference
for what the C# plugin has to do: receive datagrams, parse one JSON object each,
and track `seq` to notice loss.

Usage:
    python tools/udp_listen.py                      # summarise for 10s
    python tools/udp_listen.py --seconds 30 --print  # dump every datagram
    python tools/udp_listen.py --watch sc.vehicle.speed sc.weapon.shot_fired
"""
from __future__ import annotations

import argparse
import collections
import json
import socket
import time


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=51680)
    ap.add_argument("--seconds", type=float, default=10.0)
    ap.add_argument("--print", dest="dump", action="store_true")
    ap.add_argument("--watch", nargs="*", default=None,
                    help="print only these properties as they change")
    args = ap.parse_args(argv)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.host, args.port))
    sock.settimeout(0.5)
    print(f"listening on udp://{args.host}:{args.port} for {args.seconds:g}s")

    n = 0
    bytes_total = 0
    props: dict[str, object] = {}
    counts: collections.Counter = collections.Counter()
    last_seq = None
    gaps = 0
    watched: dict[str, object] = {}
    t_end = time.monotonic() + args.seconds

    while time.monotonic() < t_end:
        try:
            data, _ = sock.recvfrom(65535)
        except socket.timeout:
            continue
        n += 1
        bytes_total += len(data)
        try:
            msg = json.loads(data.decode("utf8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            print("  ! undecodable datagram")
            continue

        seq = msg.get("seq")
        if last_seq is not None and seq is not None and seq > last_seq + 1:
            gaps += seq - last_seq - 1
        if seq is not None:
            last_seq = seq

        p = msg.get("props", {})
        props.update(p)
        for k in p:
            counts[k] += 1

        if args.dump:
            print(f"  seq={seq} part={msg.get('part', '-')} "
                  f"{len(p)} props {len(data)}B")
        if args.watch:
            for k in args.watch:
                if k in p and p[k] != watched.get(k):
                    watched[k] = p[k]
                    print(f"  {k} = {p[k]}")

    sock.close()
    print(f"\ndatagrams : {n}  ({bytes_total} bytes, "
          f"avg {bytes_total // n if n else 0}B)")
    print(f"seq gaps  : {gaps}")
    print(f"properties: {len(props)} distinct")
    if n == 0:
        print("\nNothing received. Is the server running with --udp, or a "
              "replay in progress?")
        return 1

    interesting = [k for k in sorted(props)
                   if not k.startswith("sc.raw.")][:18]
    print("\nlatest values (named properties):")
    for k in interesting:
        v = props[k]
        if isinstance(v, float):
            v = round(v, 4)
        print(f"  {k:<36} {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
