"""Publish mapped telemetry as newline-delimited JSON over UDP.

UDP on loopback is the right transport here: fire-and-forget, no connection
state, no head-of-line blocking, and a stalled consumer cannot add latency to
the producer -- which matters because this sits behind a ~50 Hz game loop
feeding a haptic device, where a late sample is worse than a lost one.

Each datagram is one JSON object:

    {"seq": 1234, "t": 1760000000.123, "props": {"sc.vehicle.speed": 180.2, ...}}

`seq` increments per datagram so a consumer can detect loss. Loopback UDP
essentially never drops, but discrete events are published as monotonic
*counters* rather than one-shot booleans precisely so that a lost datagram
cannot swallow a footstep or a shot -- the consumer differentiates the counter
and still sees that N events happened.

The payload is kept under ~1200 bytes' worth of properties per datagram where
possible; oversized property sets are split across datagrams sharing a `seq`
but flagged with `part`.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import uuid
from typing import Any

MAX_PAYLOAD = 1200  # conservative, well under loopback MTU


class UdpPublisher:
    """Publishes property snapshots as JSON datagrams.

    Each datagram carries `src`, a per-process instance id. Without it a
    consumer cannot tell "the publisher restarted" from "the counters jumped",
    and the discrete-event counters are only meaningful within one publisher's
    lifetime. Two publishers sending to the same port (two replays, or a replay
    alongside the live server) otherwise interleave their independent counters
    and make the consumer see constant spurious events.
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 51680) -> None:
        self.addr = (host, port)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._lock = threading.Lock()
        self._seq = 0
        # Short, stable for this process; os.getpid alone is reused too readily.
        self.src = f"{os.getpid():x}-{uuid.uuid4().hex[:8]}"
        self.sent = 0
        self.dropped = 0

    # -- plumbing --------------------------------------------------------

    def _send(self, obj: dict[str, Any]) -> None:
        data = json.dumps(obj, separators=(",", ":")).encode("utf8")
        try:
            self._sock.sendto(data, self.addr)
            self.sent += 1
        except OSError:
            # A publisher must never take down the telemetry path.
            self.dropped += 1

    def publish(self, props: dict[str, Any]) -> None:
        if not props:
            return
        with self._lock:
            self._seq += 1
            seq = self._seq

        items = list(props.items())
        # Fast path: everything fits in one datagram.
        payload = {"src": self.src, "seq": seq, "t": time.time(),
                   "props": dict(items)}
        if len(json.dumps(payload, separators=(",", ":"))) <= MAX_PAYLOAD:
            self._send(payload)
            return

        # Split, preserving seq so a consumer can reassemble if it cares.
        chunk: dict[str, Any] = {}
        part = 0
        for k, v in items:
            chunk[k] = v
            if len(json.dumps(chunk, separators=(",", ":"))) > MAX_PAYLOAD:
                chunk.pop(k)
                self._send({"src": self.src, "seq": seq, "t": time.time(),
                            "part": part, "props": chunk})
                part += 1
                chunk = {k: v}
        if chunk:
            self._send({"src": self.src, "seq": seq, "t": time.time(),
                        "part": part, "props": chunk})

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass


class Aggregator:
    """Accumulates mapped properties and publishes at a bounded rate.

    The game posts several event types per frame, each arriving as a separate
    gRPC message. Publishing one datagram per event would multiply traffic and
    hand the consumer a partial view of each frame. Instead properties are
    merged into a running snapshot and flushed on a timer, so a consumer always
    receives a coherent set.

    Discrete events are counted rather than overwritten, so two footsteps
    inside one flush interval are still both visible.
    """

    def __init__(self, publisher: UdpPublisher, hz: float = 60.0) -> None:
        self.pub = publisher
        self.interval = 1.0 / hz if hz > 0 else 0.0
        self._state: dict[str, Any] = {}
        self._counters: dict[str, int] = {}
        self._lock = threading.Lock()
        self._last = 0.0
        self.flushes = 0

    def update(self, props: dict[str, Any]) -> None:
        if not props:
            return
        with self._lock:
            for k, v in props.items():
                if k.endswith(".count"):
                    self._counters[k] = self._counters.get(k, 0) + int(v or 0)
                else:
                    self._state[k] = v
        self.maybe_flush()

    def maybe_flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._last) < self.interval:
            return
        with self._lock:
            if not self._state and not self._counters:
                return
            snapshot = dict(self._state)
            snapshot.update(self._counters)
            self._last = now
        self.pub.publish(snapshot)
        self.flushes += 1
