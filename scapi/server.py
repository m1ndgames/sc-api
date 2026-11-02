"""sc-api -- a stand-in for the D-BOX LiveMotion gRPC server.

Implements `dbox.proto.v1.LiveMotion` so that Star Citizen's statically linked
LiveMotion client (via `dbxLive64.dll`) connects to us instead of D-BOX's
MotionEngine. Everything the game sends is logged; registered event schemas are
used to decode posted telemetry.

The real server listens on 127.0.0.1:61666 over TLS with a certificate whose CN
is "LiveMotionComm" -- both taken from
  C:\\ProgramData\\D-BOX\\Gaming\\LiveMotion\\LiveMotionConnector\\LiveMotionConnector.config

Usage:
    python -m scapi.server                       # TLS on 61666 (default)
    python -m scapi.server --plaintext           # no TLS, for local testing
    python -m scapi.server --port 61666 \\
        --cert certs/livemotion.crt --key certs/livemotion.key
    python -m scapi.server --jsonl capture.jsonl # also append records as JSONL

Note on result codes: `LiveMotionResponse.result_code` and
`Reply.liveMotionResult` semantics are not documented anywhere we can observe.
0 is assumed to mean success. If the game disconnects right after Initialize,
`--result-code` is the first thing to vary.
"""
from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys
import threading
import time
from concurrent import futures

# The generated protobuf stubs import each other by bare module name, so their
# own directory has to be importable.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from scapi import __version__, data_root  # noqa: E402

# Where user files (certs, captures) live -- next to the .exe when frozen.
ROOT = data_root()

import grpc  # noqa: E402

import LiveMotion_pb2 as pb  # noqa: E402
import LiveMotion_pb2_grpc as pbg  # noqa: E402

from scapi.events import EventRegistry  # noqa: E402
from scapi.mapping import map_event  # noqa: E402
from scapi.publish import Aggregator, UdpPublisher  # noqa: E402

log = logging.getLogger("sc-api")

# Defaults observed from LiveMotionConnector.config
DEFAULT_PORT = 61666
DEFAULT_HOST = "0.0.0.0"

# Replayed in GetIdentification. The genuine MotionEngine values could not be
# captured (its gRPC server only runs in Adaptive Gaming mode), so these are
# plausible stand-ins. If the client turns out to validate them, adjust here.
SERVER_ID = "sc-api"
SERVER_NAME = "D-BOX Motion Engine"
SERVER_VERSION = "6.12.0"

# Every request payload field in LiveMotionRequest, in field-number order.
# Treated as a pseudo-oneof: the schema does not declare a real oneof, so we
# test presence on each.
PAYLOADS = (
    "initialize",
    "terminate",
    "open",
    "close",
    "start",
    "stop",
    "register_event",
    "set_open_extra_sessions",
    "reset_state",
    "post_event",
    "post_event_to_session",
    "post_events",
    "post_events_to_session",
    "heartbeat",
)


def now_us() -> int:
    return int(time.time() * 1_000_000)


class Recorder:
    """Appends capture records as JSON Lines, if enabled."""

    def __init__(self, path: pathlib.Path | None) -> None:
        self._path = path
        self._lock = threading.Lock()
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = path.open("a", encoding="utf8") if path else None

    def write(self, kind: str, payload: dict) -> None:
        if not self._fh:
            return
        rec = {"t": time.time(), "kind": kind, **payload}
        with self._lock:
            self._fh.write(json.dumps(rec, default=str) + "\n")
            self._fh.flush()

    def close(self) -> None:
        if self._fh:
            self._fh.close()


class LiveMotionService(pbg.LiveMotionServicer):
    def __init__(self, recorder: Recorder, result_code: int = 0,
                 agg: Aggregator | None = None) -> None:
        self.registry = EventRegistry()
        self.rec = recorder
        self.agg = agg
        self.result_code = result_code
        self._clients = 0
        self._lock = threading.Lock()
        self._post_count = 0

    # -- unary -----------------------------------------------------------

    def GetIdentification(self, request, context):
        log.info(
            "GetIdentification from client_id=%r client_type=%s client_version=%r",
            request.client_info.client_id,
            pb.ClientType.Name(request.client_info.client_type),
            request.client_version,
        )
        self.rec.write(
            "identification_request",
            {
                "client_id": request.client_info.client_id,
                "client_type": int(request.client_info.client_type),
                "client_version": request.client_version,
            },
        )
        with self._lock:
            count = self._clients
        return pb.IdentificationResponse(
            server_id=SERVER_ID,
            server_name=SERVER_NAME,
            server_version=SERVER_VERSION,
            supported_client_types=[
                pb.Default,
                pb.UniversalHaptiCode,
                pb.LiveMotionGame,
            ],
            single_client_mode=False,
            current_client_count=count,
        )

    # -- bidi stream -----------------------------------------------------

    def LiveMotionStream(self, request_iterator, context):
        peer = context.peer()
        with self._lock:
            self._clients += 1
        log.info("stream OPEN from %s", peer)
        self.rec.write("stream_open", {"peer": peer})

        try:
            for req in request_iterator:
                for resp in self._handle(req):
                    yield resp
        except grpc.RpcError as e:  # pragma: no cover
            log.warning("stream RPC error from %s: %s", peer, e)
        finally:
            with self._lock:
                self._clients -= 1
            log.info("stream CLOSED from %s (events posted: %d)", peer, self._post_count)
            self.rec.write("stream_close", {"peer": peer, "posted": self._post_count})
            for row in self.registry.summary():
                log.info("  registered event %s", row)

    # -- dispatch --------------------------------------------------------

    def _base(self, req) -> pb.LiveMotionResponse:
        return pb.LiveMotionResponse(
            client_id=req.client_info.client_id,
            request_id=req.request_id,
            timestamp=now_us(),
            result_code=self.result_code,
        )

    def _handle(self, req):
        which = [p for p in PAYLOADS if req.HasField(p)]
        if not which:
            log.warning("request %d carried no known payload", req.request_id)
            r = self._base(req)
            r.reply.liveMotionResult = self.result_code
            yield r
            return

        for name in which:
            yield from self._dispatch(name, req)

    def _dispatch(self, name: str, req):
        resp = self._base(req)
        payload = getattr(req, name)

        if name == "initialize":
            log.info(
                "INITIALIZE appKey=%r appVersion=%s apiKey=%r",
                payload.appKey, payload.appVersion, payload.apiKey,
            )
            self.rec.write(
                "initialize",
                {
                    "appKey": payload.appKey,
                    "appVersion": payload.appVersion,
                    "apiKey": payload.apiKey,
                },
            )
            resp.reply.liveMotionResult = self.result_code

        elif name == "register_event":
            summary = self.registry.register(payload.eventDefinition)
            log.info("REGISTER_EVENT %s", summary)
            self.rec.write("register_event", summary)
            resp.reply.liveMotionResult = self.result_code

        elif name in ("post_event", "post_event_to_session"):
            decoded = self.registry.decode(payload.eventInfo)
            self._post_count += 1
            log.debug("POST_EVENT %s", decoded)
            self.rec.write(name, decoded)
            if self.agg is not None:
                self.agg.update(map_event(decoded))
            resp.reply.liveMotionResult = self.result_code

        elif name in ("post_events", "post_events_to_session"):
            decoded = self.registry.decode(payload.eventInfo)
            self._post_count += 1
            log.debug("POST_EVENTS %s", decoded)
            self.rec.write(name, decoded)
            if self.agg is not None:
                self.agg.update(map_event(decoded))
            resp.post_events_reply.liveMotionResult = self.result_code
            resp.post_events_reply.eventBufferedCount = payload.eventBufferedCount

        elif name == "heartbeat":
            resp.heartbeat_response.server_timestamp = now_us()
            resp.heartbeat_response.client_timestamp = payload.client_timestamp
            with self._lock:
                resp.heartbeat_response.connected_clients = self._clients
            log.debug("HEARTBEAT client_ts=%s", payload.client_timestamp)

        else:
            # open / close / start / stop / reset_state / terminate /
            # set_open_extra_sessions -- all empty or trivial params.
            log.info("%s", name.upper())
            self.rec.write(name, {})
            resp.reply.liveMotionResult = self.result_code

        yield resp


def build_credentials(cert: pathlib.Path, key: pathlib.Path) -> grpc.ServerCredentials:
    return grpc.ssl_server_credentials(
        [(key.read_bytes(), cert.read_bytes())]
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="sc-api",
        description="sc-api -- D-BOX LiveMotion shim server for Star Citizen")
    ap.add_argument("--version", action="version", version=f"sc-api {__version__}")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--plaintext", action="store_true",
                    help="serve without TLS (the real client expects TLS)")
    ap.add_argument("--cert", type=pathlib.Path, default=ROOT / "certs" / "livemotion.crt")
    ap.add_argument("--key", type=pathlib.Path, default=ROOT / "certs" / "livemotion.key")
    ap.add_argument("--jsonl", type=pathlib.Path, default=None,
                    help="append every record to this JSONL file")
    ap.add_argument("--result-code", type=int, default=0,
                    help="value for result_code / liveMotionResult (0 assumed success)")
    ap.add_argument("--udp", metavar="HOST:PORT", default=None,
                    help="publish mapped telemetry as JSON over UDP "
                         "(e.g. 127.0.0.1:51680) for the SimHub plugin")
    ap.add_argument("--udp-hz", type=float, default=60.0,
                    help="max publish rate for --udp (default 60)")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="log every posted event (very chatty)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(message)s",
    )

    recorder = Recorder(args.jsonl)

    agg = None
    if args.udp:
        host, _, port = args.udp.rpartition(":")
        if not host or not port.isdigit():
            log.error("--udp expects HOST:PORT, got %r", args.udp)
            return 2
        agg = Aggregator(UdpPublisher(host, int(port)), hz=args.udp_hz)
        log.info("publishing telemetry to udp://%s:%s at up to %.0f Hz",
                 host, port, args.udp_hz)

    service = LiveMotionService(recorder, result_code=args.result_code, agg=agg)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pbg.add_LiveMotionServicer_to_server(service, server)

    addr = f"{args.host}:{args.port}"
    if args.plaintext:
        server.add_insecure_port(addr)
        log.info("listening on %s (PLAINTEXT)", addr)
    else:
        if not args.cert.exists() or not args.key.exists():
            log.error("missing cert/key: %s / %s", args.cert, args.key)
            log.error("generate them with make_cert.sh, or use --plaintext")
            return 2
        server.add_secure_port(addr, build_credentials(args.cert, args.key))
        log.info("listening on %s (TLS, cert=%s)", addr, args.cert)

    server.start()
    log.info("sc-api %s -- waiting for Star Citizen ... Ctrl+C to stop", __version__)
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        log.info("shutting down")
        server.stop(grace=1).wait()
    finally:
        recorder.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
