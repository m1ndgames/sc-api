"""Self test for build/dbxLive64.dll before it is loaded into the game.

Loads the stub, calls GetEventHandler(), walks the returned object's vtable and
invokes all 14 slots the way the game's x64 __thiscall would (this in RCX,
arguments in RDX/R8/R9), then checks the log recorded every call.

Handing an untested DLL to the game process risks crashing it, so this runs the
whole ABI path in a throwaway process first.

Usage:  python tools/test_stub.py
"""
from __future__ import annotations

import ctypes
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
DLL = ROOT / "build" / "dbxLive64.dll"
SLOTS = 14


def main() -> int:
    if not DLL.exists():
        print(f"missing {DLL}; run scripts/build_stub.ps1 first", file=sys.stderr)
        return 2

    log = ROOT / "build" / "stub_selftest.log"
    if log.exists():
        log.unlink()
    os.environ["SC_API_STUB_LOG"] = str(log)

    lib = ctypes.WinDLL(str(DLL))

    get = lib.GetEventHandler
    get.restype = ctypes.c_void_p
    get.argtypes = []

    handler = get()
    print(f"GetEventHandler() -> 0x{handler:x}")
    if not handler:
        print("FAIL: null handler", file=sys.stderr)
        return 1

    # Singleton check: the real DLL returns the same object every time.
    if get() != handler:
        print("FAIL: GetEventHandler is not returning a stable singleton",
              file=sys.stderr)
        return 1
    print("singleton: stable across calls  OK")

    # vtable pointer is the first qword of the object.
    vtbl = ctypes.cast(handler, ctypes.POINTER(ctypes.c_void_p))[0]
    print(f"vtable    -> 0x{vtbl:x}")
    slots = ctypes.cast(vtbl, ctypes.POINTER(ctypes.c_void_p))

    PROTO = ctypes.CFUNCTYPE(
        ctypes.c_uint64,      # return
        ctypes.c_void_p,      # this (RCX)
        ctypes.c_uint64,      # RDX
        ctypes.c_uint64,      # R8
        ctypes.c_uint64,      # R9
    )

    # Pass a readable buffer as arg1 so the guarded memory dump is exercised.
    buf = ctypes.create_string_buffer(bytes(range(64)), 64)
    buf_addr = ctypes.addressof(buf)

    failures = []
    for i in range(SLOTS):
        fn = PROTO(slots[i])
        try:
            rv = fn(handler, buf_addr, 0xA1B2C3D4 + i, 0)
        except OSError as e:
            failures.append(f"slot {i} raised {e}")
            continue
        if rv != 0:
            failures.append(f"slot {i} returned {rv}, expected 0")
    print(f"invoked {SLOTS} slots")

    # Force DLL_PROCESS_DETACH so the log is flushed and closed.
    del lib
    import gc
    gc.collect()

    if not log.exists():
        failures.append(f"no log written at {log}")
    else:
        text = log.read_text(encoding="utf8", errors="replace")
        seen = {i for i in range(SLOTS) if f"slot={i} " in text}
        missing = sorted(set(range(SLOTS)) - seen)
        if missing:
            failures.append(f"log missing calls for slots {missing}")
        if "-> 00 01 02 03" not in text:
            failures.append("guarded memory dump did not record the buffer")
        print(f"log       -> {log} ({len(text)} bytes, "
              f"{len(seen)}/{SLOTS} slots recorded)")

    if failures:
        print("\nFAILURES:")
        for f in failures:
            print("  -", f)
        return 1
    print("\nOK: stub ABI works, all 14 slots callable, logging verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
