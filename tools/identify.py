"""Query the real D-BOX MotionEngine LiveMotion server for its identification.

Must be run while the genuine `DboxMotionEngine` service is still running. The
values it reports are what our shim replays, so that if the game validates the
handshake we already answer correctly.

Usage:  python tools/identify.py [host:port]
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scapi"))

import grpc  # noqa: E402

import LiveMotion_pb2 as pb  # noqa: E402
import LiveMotion_pb2_grpc as pbg  # noqa: E402

TARGET = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:12740"


def main() -> int:
    print(f"querying {TARGET} ...")
    with grpc.insecure_channel(TARGET) as ch:
        try:
            grpc.channel_ready_future(ch).result(timeout=5)
        except grpc.FutureTimeoutError:
            print(f"ERROR: could not connect to {TARGET}", file=sys.stderr)
            return 1

        stub = pbg.LiveMotionStub(ch)
        req = pb.IdentificationRequest(
            client_info=pb.ClientInfo(
                client_id="sc-api-probe",
                client_type=pb.LiveMotionGame,
            ),
            client_version="1.0.0",
        )
        try:
            resp = stub.GetIdentification(req, timeout=10)
        except grpc.RpcError as e:
            print(f"ERROR: RPC failed: {e.code()}: {e.details()}", file=sys.stderr)
            return 1

    print("--- IdentificationResponse ---")
    print(resp)

    out = {
        "server_id": resp.server_id,
        "server_name": resp.server_name,
        "server_version": resp.server_version,
        "supported_client_types": list(resp.supported_client_types),
        "single_client_mode": resp.single_client_mode,
        "current_client_count": resp.current_client_count,
    }
    dest = ROOT / "captured_identification.json"
    dest.write_text(json.dumps(out, indent=2) + "\n", encoding="utf8")
    print(f"saved -> {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
