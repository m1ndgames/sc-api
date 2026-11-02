"""End-to-end self test: drives sc-api's server the way the game is expected to.

Acts as a LiveMotion client -- GetIdentification, then one bidi stream carrying
initialize / open / start / register_event / post_event / post_events /
heartbeat / stop / close / terminate -- and asserts the server answers each.

This validates our proto reconstruction, the server dispatch, the event
registry decoding and the TLS setup. It does NOT prove anything about Star
Citizen; only a real capture run can do that.

Usage:
    python tools/selftest.py                      # TLS against certs/livemotion.crt
    python tools/selftest.py --plaintext
"""
from __future__ import annotations

import argparse
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scapi"))

import grpc  # noqa: E402

import LiveMotion_pb2 as pb  # noqa: E402
import LiveMotion_pb2_grpc as pbg  # noqa: E402

CLIENT_ID = "sc-api-selftest"
EVENT_KEY = 4242
APP_KEY = "SelfTest"


def now_us() -> int:
    return int(time.time() * 1_000_000)


def requests():
    """Yield a plausible session, mirroring the game's expected call order."""
    ci = pb.ClientInfo(client_id=CLIENT_ID, client_type=pb.LiveMotionGame)
    rid = 0

    def env(**kw):
        nonlocal rid
        rid += 1
        return pb.LiveMotionRequest(client_info=ci, request_id=rid,
                                    timestamp=now_us(), **kw)

    yield env(initialize=pb.InitializeParam(
        appKey=APP_KEY, appVersion=1000, apiKey="selftest-api-key"))
    yield env(open=pb.OpenParam())
    yield env(start=pb.StartParam())

    # Declare an event with one field of each interesting shape.
    yield env(register_event=pb.RegisterEventParam(
        eventDefinition=pb.EventDefinition(
            eventKey=EVENT_KEY,
            eventMeaning=7,
            fieldCount=4,
            fieldDef=[
                pb.FieldDefinition(type=3, flags=0, meaning=101, offset=0),
                pb.FieldDefinition(type=7, flags=0, meaning=102, offset=4),
                pb.FieldDefinition(type=10, flags=0, meaning=103, offset=16),
                pb.FieldDefinition(type=5, flags=0, meaning=104, offset=32),
            ],
        )))

    # One instance of it.
    yield env(post_event=pb.PostEventParam(
        eventInfo=pb.EventInfo(
            eventKey=EVENT_KEY, size=40, count=1,
            eventData=[pb.EventData(eventField=[
                pb.EventField(dataFloat32=0.75),
                pb.EventField(dataFloat32XYZ=pb.XYZ_StructFloat32(x=1.0, y=-2.0, z=3.5)),
                pb.EventField(dataQuadFloat32=pb.QuadFloat32(
                    frontLeft=0.1, frontRight=0.2, backLeft=0.3, backRight=0.4)),
                pb.EventField(dataString="footstep"),
            ])],
        )))

    # A batched post, which takes the PostEventsReply path.
    yield env(post_events=pb.PostEventsParam(
        eventBufferedCount=2,
        eventInfo=pb.EventInfo(
            eventKey=EVENT_KEY, size=80, count=2,
            eventData=[
                pb.EventData(eventField=[pb.EventField(dataFloat32=0.25)]),
                pb.EventData(eventField=[pb.EventField(dataFloat32=0.5)]),
            ],
        )))

    # An event posted WITHOUT prior registration -- must not crash the server.
    yield env(post_event=pb.PostEventParam(
        eventInfo=pb.EventInfo(
            eventKey=9999, size=4, count=1,
            eventData=[pb.EventData(eventField=[pb.EventField(dataInt32=17)])],
        )))

    yield env(heartbeat=pb.HeartbeatParam(client_timestamp=now_us()))
    yield env(stop=pb.StopParam())
    yield env(close=pb.CloseParam())
    yield env(terminate=pb.TerminateParam())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="127.0.0.1:61666")
    ap.add_argument("--plaintext", action="store_true")
    ap.add_argument("--cert", type=pathlib.Path, default=ROOT / "certs" / "livemotion.crt")
    args = ap.parse_args(argv)

    if args.plaintext:
        ch = grpc.insecure_channel(args.target)
    else:
        creds = grpc.ssl_channel_credentials(root_certificates=args.cert.read_bytes())
        # The cert's CN/SAN is LiveMotionComm; override authority so verification
        # matches regardless of the dial address.
        ch = grpc.secure_channel(
            args.target, creds,
            options=(("grpc.ssl_target_name_override", "LiveMotionComm"),),
        )

    failures = []
    with ch:
        try:
            grpc.channel_ready_future(ch).result(timeout=8)
        except grpc.FutureTimeoutError:
            print(f"FAIL: cannot connect to {args.target}", file=sys.stderr)
            return 1

        stub = pbg.LiveMotionStub(ch)

        ident = stub.GetIdentification(pb.IdentificationRequest(
            client_info=pb.ClientInfo(client_id=CLIENT_ID, client_type=pb.LiveMotionGame),
            client_version="1.0.0",
        ), timeout=8)
        print(f"identification: id={ident.server_id!r} name={ident.server_name!r} "
              f"version={ident.server_version!r} "
              f"types={[pb.ClientType.Name(t) for t in ident.supported_client_types]} "
              f"single={ident.single_client_mode}")
        if not ident.server_version:
            failures.append("GetIdentification returned empty server_version")

        n = 0
        kinds = set()
        for resp in stub.LiveMotionStream(requests(), timeout=20):
            n += 1
            if resp.HasField("reply"):
                kinds.add("reply")
            if resp.HasField("post_events_reply"):
                kinds.add("post_events_reply")
                if resp.post_events_reply.eventBufferedCount != 2:
                    failures.append("post_events_reply.eventBufferedCount not echoed")
            if resp.HasField("heartbeat_response"):
                kinds.add("heartbeat_response")
                if resp.heartbeat_response.client_timestamp == 0:
                    failures.append("heartbeat did not echo client_timestamp")
            if resp.result_code != 0:
                failures.append(f"non-zero result_code {resp.result_code}")
            if resp.client_id != CLIENT_ID:
                failures.append(f"client_id not echoed: {resp.client_id!r}")

        print(f"stream: {n} responses, payload kinds = {sorted(kinds)}")
        if n != 11:
            failures.append(f"expected 11 responses, got {n}")
        for need in ("reply", "post_events_reply", "heartbeat_response"):
            if need not in kinds:
                failures.append(f"never saw a {need}")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nOK: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
