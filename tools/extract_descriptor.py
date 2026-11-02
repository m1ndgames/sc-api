"""Extract the D-BOX LiveMotion protobuf schema from MotionEngine.exe and verify
that proto/LiveMotion.proto reproduces it exactly.

Protobuf's C++ codegen embeds the serialized FileDescriptorProto of every .proto
file into the compiled binary. That means the complete schema -- message names,
field numbers, wire types, enums and the service definition -- can be read
straight out of the shipped executable, with no disassembler involved.

The descriptor is located by its leading bytes: field 1 (name) of
FileDescriptorProto, length-delimited, holding "LiveMotion.proto".

Comparison is SEMANTIC, not byte-exact. A descriptor compiled from our .proto
legitimately differs in serialization details (field ordering, json_name
population, syntax markers), so the check compares the structures that matter:
every message's fields with their numbers, labels and types; every enum value;
and the service's methods with their streaming flags.

Usage:
    python tools/extract_descriptor.py
    python tools/extract_descriptor.py --exe "C:\\path\\to\\MotionEngine.exe"
    python tools/extract_descriptor.py --dump-proto   # print recovered schema
"""
from __future__ import annotations

import argparse
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent

DEFAULT_EXE = pathlib.Path(
    r"C:\Program Files\D-BOX\Motion Engine\MotionEngine.exe"
)
PROTO_NAME = b"LiveMotion.proto"
# 0x0a = field 1, wire type 2; followed by the single-byte length of the name.
MARKER = bytes([0x0A, len(PROTO_NAME)]) + PROTO_NAME

try:
    from google.protobuf import descriptor_pb2
except ImportError:  # pragma: no cover
    print("protobuf is required: pip install -r requirements.txt", file=sys.stderr)
    raise


def find_descriptor(data: bytes) -> tuple[int, bytes]:
    """Locate and return (offset, bytes) of the embedded FileDescriptorProto."""
    starts = []
    i = 0
    while (j := data.find(MARKER, i)) >= 0:
        starts.append(j)
        i = j + 1
    if not starts:
        raise SystemExit("no embedded LiveMotion.proto descriptor found")
    if len(starts) > 1:
        print(f"note: {len(starts)} candidate offsets {starts}; using the first")

    start = starts[0]
    # The descriptor is not length-prefixed in the binary, so walk its top-level
    # fields on the wire and stop at the first thing that cannot belong to a
    # FileDescriptorProto. Field numbers 1..12 are defined; anything else, or a
    # wire type other than varint/length-delimited, marks the end of the blob.
    end = _walk_top_level(data, start)
    blob = data[start:end]

    # Validate by actually parsing it.
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.ParseFromString(blob)
    if fdp.name != PROTO_NAME.decode() or not fdp.message_type:
        raise SystemExit(f"marker at {start} did not yield a usable descriptor")
    return start, blob


def _read_varint(b: bytes, i: int) -> tuple[int, int]:
    val = shift = 0
    while True:
        if i >= len(b) or shift > 63:
            raise ValueError("truncated varint")
        c = b[i]
        i += 1
        val |= (c & 0x7F) << shift
        if not c & 0x80:
            return val, i
        shift += 7


def _walk_top_level(data: bytes, start: int) -> int:
    """Return the offset just past the last field belonging to the descriptor."""
    i = start
    limit = min(start + 1 << 20, len(data))
    while i < limit:
        try:
            key, j = _read_varint(data, i)
        except ValueError:
            break
        field_no, wire = key >> 3, key & 7
        if field_no == 0 or field_no > 12:
            break
        if wire == 2:
            try:
                size, j2 = _read_varint(data, j)
            except ValueError:
                break
            if j2 + size > len(data) or size > (1 << 20):
                break
            i = j2 + size
        elif wire == 0:
            try:
                _, i = _read_varint(data, j)
            except ValueError:
                break
        else:
            break
    return i


def summarize(fdp) -> dict:
    """Reduce a FileDescriptorProto to the structure we care about."""
    out = {"package": fdp.package, "messages": {}, "enums": {}, "services": {}}

    def add_msg(m, prefix=""):
        full = f"{prefix}{m.name}"
        out["messages"][full] = {
            f.name: (f.number, f.label, f.type, f.type_name) for f in m.field
        }
        for n in m.nested_type:
            add_msg(n, full + ".")
        for e in m.enum_type:
            out["enums"][f"{full}.{e.name}"] = {v.name: v.number for v in e.value}

    for m in fdp.message_type:
        add_msg(m)
    for e in fdp.enum_type:
        out["enums"][e.name] = {v.name: v.number for v in e.value}
    for s in fdp.service:
        out["services"][s.name] = {
            mm.name: (mm.input_type, mm.output_type,
                      mm.client_streaming, mm.server_streaming)
            for mm in s.method
        }
    return out


def compile_our_proto() -> "descriptor_pb2.FileDescriptorProto":
    """Compile proto/LiveMotion.proto to a descriptor set via grpc_tools.protoc."""
    proto = ROOT / "proto" / "LiveMotion.proto"
    if not proto.exists():
        raise SystemExit(f"missing {proto}")
    with tempfile.TemporaryDirectory() as td:
        out = pathlib.Path(td) / "ours.desc"
        cmd = [
            sys.executable, "-m", "grpc_tools.protoc",
            f"-I{proto.parent}", f"--descriptor_set_out={out}", str(proto),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise SystemExit(f"protoc failed:\n{res.stderr}")
        fds = descriptor_pb2.FileDescriptorSet()
        fds.ParseFromString(out.read_bytes())
    return fds.file[0]


def diff(theirs: dict, ours: dict) -> list[str]:
    problems: list[str] = []

    if theirs["package"] != ours["package"]:
        problems.append(f"package: {theirs['package']!r} vs {ours['package']!r}")

    for section in ("messages", "enums", "services"):
        t, o = theirs[section], ours[section]
        for name in sorted(set(t) - set(o)):
            problems.append(f"{section}: missing from ours: {name}")
        for name in sorted(set(o) - set(t)):
            problems.append(f"{section}: extra in ours: {name}")
        for name in sorted(set(t) & set(o)):
            if t[name] != o[name]:
                tk, ok_ = t[name], o[name]
                for k in sorted(set(tk) | set(ok_)):
                    if tk.get(k) != ok_.get(k):
                        problems.append(
                            f"{section} {name}.{k}: theirs={tk.get(k)} ours={ok_.get(k)}"
                        )
    return problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", type=pathlib.Path, default=DEFAULT_EXE)
    ap.add_argument("--dump-proto", action="store_true",
                    help="print the recovered schema and exit")
    args = ap.parse_args(argv)

    if not args.exe.exists():
        print(f"not found: {args.exe}", file=sys.stderr)
        print("D-BOX may not be installed; pass --exe explicitly.", file=sys.stderr)
        return 2

    data = args.exe.read_bytes()
    offset, blob = find_descriptor(data)
    fdp = descriptor_pb2.FileDescriptorProto()
    fdp.ParseFromString(blob)

    print(f"source : {args.exe}")
    print(f"offset : {offset}  length: {len(blob)} bytes")
    print(f"file   : {fdp.name}   package: {fdp.package}")
    print(f"content: {len(fdp.message_type)} messages, {len(fdp.enum_type)} enums, "
          f"{len(fdp.service)} service(s)")

    if args.dump_proto:
        print()
        print(fdp)
        return 0

    ours = compile_our_proto()
    problems = diff(summarize(fdp), summarize(ours))
    print()
    if problems:
        print(f"MISMATCH ({len(problems)} difference(s)) between the embedded "
              f"descriptor and proto/LiveMotion.proto:")
        for p in problems:
            print("  -", p)
        return 1

    print("OK: proto/LiveMotion.proto matches the embedded descriptor exactly")
    print("    (messages, field numbers, labels, types, enums, service methods"
          " and streaming flags)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
