"""Recover a C++ class vtable from an MSVC x64 PE via RTTI.

Used to map `dbox::IEventHandler` / `dbox::LiveMotionConnectorEventHandler` in
`dbxLive64.dll`, whose vtable layout is the ABI we must reproduce for a drop-in
replacement DLL (README section 3, Option B).

How it works -- MSVC lays RTTI out like this:

    TypeDescriptor      { void* vfptr; void* spare; char name[]; }
    _RTTICompleteObjectLocator (x64, image-relative)
                        { u32 signature=1; u32 offset; u32 cdOffset;
                          u32 pTypeDescriptor; u32 pClassDescriptor;
                          u32 pSelf; }
    vtable[-1]          -> pointer to the COL
    vtable[0..]         -> virtual function pointers

So: find the mangled name, step back to the TypeDescriptor, find the COL whose
pTypeDescriptor points at it, find the data pointer to that COL, and the vtable
begins 8 bytes later.

Usage:
    python tools/find_vtable.py <dll> --name ".?AVIEventHandler@dbox@@"
    python tools/find_vtable.py <dll> --list-classes
"""
from __future__ import annotations

import argparse
import pathlib
import struct


class PE:
    def __init__(self, data: bytes) -> None:
        self.d = data
        e = struct.unpack_from("<I", data, 0x3C)[0]
        if data[e:e + 4] != b"PE\0\0":
            raise SystemExit("not a PE")
        self.nsec = struct.unpack_from("<H", data, e + 6)[0]
        opt = e + 24
        self.magic = struct.unpack_from("<H", data, opt)[0]
        if self.magic != 0x20B:
            raise SystemExit("only PE32+ (x64) is supported")
        self.base = struct.unpack_from("<Q", data, opt + 24)[0]
        opt_size = struct.unpack_from("<H", data, e + 20)[0]
        self.secs = []
        for i in range(self.nsec):
            o = opt + opt_size + i * 40
            name = data[o:o + 8].rstrip(b"\0").decode("latin1")
            vsize, vaddr, rsize, raw = struct.unpack_from("<IIII", data, o + 8)
            self.secs.append((name, vaddr, vsize, raw, rsize))

    def rva_to_off(self, rva: int) -> int | None:
        for _n, va, vs, raw, rs in self.secs:
            if va <= rva < va + max(vs, rs):
                off = raw + (rva - va)
                return off if off < len(self.d) else None
        return None

    def off_to_rva(self, off: int) -> int | None:
        for _n, va, vs, raw, rs in self.secs:
            if raw <= off < raw + rs:
                return va + (off - raw)
        return None

    def is_code_rva(self, rva: int) -> bool:
        for n, va, vs, _raw, rs in self.secs:
            if n.startswith(".text") and va <= rva < va + max(vs, rs):
                return True
        return False

    def section_of_rva(self, rva: int) -> str:
        for n, va, vs, _raw, rs in self.secs:
            if va <= rva < va + max(vs, rs):
                return n
        return "?"


def find_all(data: bytes, needle: bytes):
    i = 0
    while (j := data.find(needle, i)) >= 0:
        yield j
        i = j + 1


def recover(pe: PE, mangled: bytes) -> list[dict]:
    d = pe.d
    results = []
    for name_off in find_all(d, mangled + b"\0"):
        # TypeDescriptor begins 16 bytes before the name on x64.
        td_off = name_off - 16
        if td_off < 0:
            continue
        td_rva = pe.off_to_rva(td_off)
        if td_rva is None:
            continue

        # Find a COL whose pTypeDescriptor == td_rva. Scan 4-byte aligned
        # positions in data sections for the RVA, then validate signature.
        for hit in find_all(d, struct.pack("<I", td_rva)):
            col_off = hit - 12            # pTypeDescriptor is at +12
            if col_off < 0:
                continue
            try:
                sig, offset, cd, ptd, pcd, pself = struct.unpack_from(
                    "<IIIIII", d, col_off)
            except struct.error:
                continue
            if sig != 1 or ptd != td_rva:
                continue
            col_rva = pe.off_to_rva(col_off)
            if col_rva is None or pself != col_rva:
                continue

            # A vtable's slot -1 holds the absolute VA of the COL.
            col_va = pe.base + col_rva
            for ref in find_all(d, struct.pack("<Q", col_va)):
                vt_off = ref + 8
                vt_rva = pe.off_to_rva(vt_off)
                if vt_rva is None:
                    continue
                slots = []
                o = vt_off
                while True:
                    if o + 8 > len(d):
                        break
                    (va,) = struct.unpack_from("<Q", d, o)
                    if va == 0:
                        break
                    rva = va - pe.base
                    if not (0 < rva < 1 << 32) or not pe.is_code_rva(rva):
                        break
                    slots.append(rva)
                    o += 8
                    if len(slots) > 256:
                        break
                if slots:
                    results.append({
                        "name": mangled.decode("latin1"),
                        "td_rva": td_rva,
                        "col_rva": col_rva,
                        "vtable_rva": vt_rva,
                        "offset_in_class": offset,
                        "slots": slots,
                    })
    return results


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dll", type=pathlib.Path)
    ap.add_argument("--name", action="append",
                    help="mangled RTTI name, e.g. '.?AVIEventHandler@dbox@@'")
    ap.add_argument("--list-classes", action="store_true")
    ap.add_argument("--filter", default="dbox",
                    help="substring filter for --list-classes (default: dbox)")
    args = ap.parse_args(argv)

    data = args.dll.read_bytes()
    pe = PE(data)
    print(f"{args.dll.name}: imagebase 0x{pe.base:x}, {len(pe.secs)} sections")

    if args.list_classes:
        seen = set()
        for off in find_all(data, b".?A"):
            end = data.find(b"\0", off)
            if end < 0 or end - off > 200:
                continue
            nm = data[off:end].decode("latin1", "replace")
            if args.filter in nm and nm not in seen:
                seen.add(nm)
        for nm in sorted(seen):
            print("  ", nm)
        return 0

    if not args.name:
        print("pass --name or --list-classes")
        return 2

    for nm in args.name:
        print(f"\n=== {nm} ===")
        res = recover(pe, nm.encode("latin1"))
        if not res:
            print("  no vtable recovered (class may be abstract-only, "
                  "or RTTI stripped)")
            continue
        for r in res:
            print(f"  TypeDescriptor rva=0x{r['td_rva']:x}  "
                  f"COL rva=0x{r['col_rva']:x}  "
                  f"vtable rva=0x{r['vtable_rva']:x} "
                  f"(offset_in_class={r['offset_in_class']})")
            print(f"  {len(r['slots'])} slots:")
            for i, rva in enumerate(r["slots"]):
                print(f"    [{i:>2}] rva 0x{rva:08x}  va 0x{pe.base + rva:x}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
