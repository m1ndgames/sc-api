"""Point the D-BOX LiveMotion connector at sc-api, or recreate it from scratch.

Star Citizen builds its connector path itself -- the format string
`%sD-BOX\\Gaming\\LiveMotion\\%s\\%s` is visible in `StarCitizen.exe` -- so the game
requires:

    C:\\ProgramData\\D-BOX\\Gaming\\LiveMotion\\<appKey>\\
        dbxLive64.dll                  the gRPC client the game loads
        LiveMotionConnector.config     <ReceiverIpAddress> <ReceiverPort> <CertificateCommonName>
        livemotion.crt                 the server certificate it expects

No registry entry, no `Program Files\\D-BOX`, and no D-BOX service is involved: `sc-api`
replaces MotionEngine, which is the only thing the DLL talks to. That means the D-BOX
software can be uninstalled entirely as long as this one directory survives -- and since
its uninstaller is likely to delete `C:\\ProgramData\\D-BOX`, keep a copy of the needed
files in `vendor/livemotion-connector/` before uninstalling.

`dbxLive64.dll` is D-BOX's signed software and is NOT redistributed by this project: it is
excluded from the repository and from release builds. It comes from an installation of the
D-BOX HaptiSync Center, which is the only legitimate source. `--status` verifies the file
in place against the genuine SHA-256.

Modes:
    --status      show what is installed where
    --provision   create the directory from vendor/ and point it at sc-api.
                  Works with D-BOX completely uninstalled.
    --apply       patch an existing D-BOX install in place (keeps backups)
    --restore     undo --apply, putting D-BOX's own config and cert back

Why the port moves: the real endpoint is 127.0.0.1:61666, but on many Windows machines
that port sits inside an invisible Hyper-V/WinNAT reservation and cannot be bound by a
normal process (WSAEACCES / 10013), even though `netsh int ipv4 show excludedportrange`
does not list it. The port is client-side configuration, so we simply move it.

Usage:
    python tools/patch_connector.py --status
    python tools/patch_connector.py --provision [--app-key StarCitizen] [--port 51666]
    python tools/patch_connector.py --apply --app-key StarCitizen
    python tools/patch_connector.py --restore
"""
from __future__ import annotations

import argparse
import hashlib
import pathlib
import shutil
import sys
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent.parent
VENDOR = ROOT / "vendor" / "livemotion-connector"
OUR_CERT = ROOT / "certs" / "livemotion.crt"

LIVEMOTION = pathlib.Path(r"C:\ProgramData\D-BOX\Gaming\LiveMotion")
CONNECTOR = LIVEMOTION / "LiveMotionConnector"
CONFIG_NAME = "LiveMotionConnector.config"
CERT_NAME = "livemotion.crt"
LOGCFG_NAME = "LiveMotionConnector.log.config"
DLL_NAME = "dbxLive64.dll"
BACKUP_SUFFIX = ".sc-api-backup"

DEFAULT_PORT = 51666
DEFAULT_HOST = "127.0.0.1"
DEFAULT_CN = "LiveMotionComm"
DEFAULT_APP_KEY = "StarCitizen"

# Observed from the live capture; the game sends appKey='StarCitizen'.
GENUINE_DLL_SHA = "85a6e973b3290c4fd39c1ccc2e1a7156c4646ce3987f42976c8fa7747982ba6a"


def sha256(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def config_xml(host: str, port: int, cn: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        "<Configuration>\n"
        f"\t<ReceiverIpAddress>{host}</ReceiverIpAddress>\n"
        f"\t<ReceiverPort>{port}</ReceiverPort>\n"
        f"\t<CertificateCommonName>{cn}</CertificateCommonName>\n"
        "</Configuration>\n"
    )


def backup(path: pathlib.Path) -> pathlib.Path | None:
    """Copy a file aside once. Never overwrites an existing backup."""
    bak = path.with_suffix(path.suffix + BACKUP_SUFFIX)
    if bak.exists() or not path.exists():
        return None
    shutil.copy2(path, bak)
    return bak


def show_config(path: pathlib.Path) -> dict[str, str]:
    if not path.exists():
        return {}
    try:
        return {c.tag: (c.text or "") for c in ET.parse(path).getroot()}
    except ET.ParseError:
        return {"(unparseable)": path.name}


def status() -> int:
    print(f"vendor    : {VENDOR}")
    vdll = VENDOR / DLL_NAME
    if vdll.exists():
        ok = sha256(vdll) == GENUINE_DLL_SHA
        print(f"  {DLL_NAME}: present, {'hash OK' if ok else 'HASH MISMATCH'}")
    else:
        print(f"  {DLL_NAME}: MISSING (not shipped with this project)")
        print( "             copy it from a D-BOX HaptiSync Center install:")
        print(rf"             C:\ProgramData\D-BOX\Gaming\LiveMotion\LiveMotionConnector\{DLL_NAME}")
    print(f"our cert  : {OUR_CERT if OUR_CERT.exists() else "(not generated; run make_cert.sh)"}")

    print(f"\ntarget    : {LIVEMOTION}")
    if not LIVEMOTION.exists():
        print("  directory does not exist (D-BOX uninstalled?) -- use --provision")
        return 0

    for d in sorted(p for p in LIVEMOTION.iterdir() if p.is_dir()):
        dll = d / DLL_NAME
        mark = ""
        if dll.exists():
            mark = "genuine" if sha256(dll) == GENUINE_DLL_SHA else "MODIFIED/unknown"
        print(f"\n  {d.name}/  {DLL_NAME}: {mark or 'absent'}")
        for k, v in show_config(d / CONFIG_NAME).items():
            print(f"      {k} = {v}")
        for n in (CONFIG_NAME, CERT_NAME):
            if (d / (n + BACKUP_SUFFIX)).exists():
                print(f"      {n}: patched (backup present)")
    return 0


def write_target(d: pathlib.Path, host: str, port: int, cn: str,
                 from_vendor: bool) -> None:
    d.mkdir(parents=True, exist_ok=True)

    if from_vendor:
        # The DLL is required. The log config only switches on the DLL's own
        # spdlog output, so a local copy is used if there is one and skipped
        # otherwise -- it is D-BOX's file and is not kept in this repository.
        for name, required in ((DLL_NAME, True), (LOGCFG_NAME, False)):
            src = VENDOR / name
            if not src.exists():
                if required:
                    # provision() checks for the DLL up front with a fuller
                    # explanation; this is the backstop.
                    raise SystemExit(f"missing {src}")
                print(f"      {name}: not present, skipped (optional)")
                continue
            dst = d / name
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            shutil.copy2(src, dst)
            print(f"      {name}: installed from local copy")
    else:
        backup(d / CONFIG_NAME)
        backup(d / CERT_NAME)

    (d / CONFIG_NAME).write_text(config_xml(host, port, cn), encoding="utf8")
    print(f"      {CONFIG_NAME}: -> {host}:{port} (CN={cn})")

    cert = d / CERT_NAME
    if cert.exists() or cert.is_symlink():
        cert.unlink()
    shutil.copy2(OUR_CERT, cert)
    print(f"      {CERT_NAME}: installed our certificate")


def provision(app_keys: list[str], host: str, port: int, cn: str) -> int:
    if not OUR_CERT.exists():
        print(f"missing {OUR_CERT}", file=sys.stderr)
        print("generate it first: sh make_cert.sh", file=sys.stderr)
        return 2
    vdll = VENDOR / DLL_NAME
    if not vdll.exists():
        # Not an oversight: this file is D-BOX's and is deliberately not shipped.
        print(f"missing {vdll}\n", file=sys.stderr)
        print(f"{DLL_NAME} is D-BOX's own signed software and is NOT shipped with this",
              file=sys.stderr)
        print("project. Install the D-BOX HaptiSync Center -- the only legitimate source",
              file=sys.stderr)
        print("-- and copy the file from\n", file=sys.stderr)
        print(rf"  C:\ProgramData\D-BOX\Gaming\LiveMotion\LiveMotionConnector\{DLL_NAME}",
              file=sys.stderr)
        print(f"\ninto\n\n  {VENDOR}\n", file=sys.stderr)
        print("then run --provision again. No D-BOX hardware or service is needed.",
              file=sys.stderr)
        return 2
    if sha256(vdll) != GENUINE_DLL_SHA:
        print(f"WARNING: vendored {DLL_NAME} does not match the expected hash",
              file=sys.stderr)

    for key in app_keys:
        print(f"  {key}/")
        write_target(LIVEMOTION / key, host, port, cn, from_vendor=True)

    print(f"\nProvisioned {len(app_keys)} appKey directory/ies under\n  {LIVEMOTION}")
    print("D-BOX itself is not required: no service, no Program Files, no registry.")
    return 0


def apply(app_keys: list[str], host: str, port: int, cn: str) -> int:
    if not OUR_CERT.exists():
        print(f"missing {OUR_CERT}; run make_cert.sh", file=sys.stderr)
        return 2
    if not CONNECTOR.exists():
        print(f"no existing D-BOX connector at {CONNECTOR}", file=sys.stderr)
        print("use --provision to create it from vendor/ instead", file=sys.stderr)
        return 2

    targets = [CONNECTOR] + [LIVEMOTION / k for k in app_keys]
    for d in targets:
        print(f"  {d.name}/")
        if not d.exists():
            # A missing appKey dir still needs the DLL, so seed it from the
            # existing connector rather than from vendor.
            d.mkdir(parents=True, exist_ok=True)
            for name in (DLL_NAME, LOGCFG_NAME):
                src = CONNECTOR / name
                if src.exists():
                    shutil.copy2(src, d / name)
                    print(f"      {name}: copied from LiveMotionConnector")
        write_target(d, host, port, cn, from_vendor=False)
    return 0


def restore() -> int:
    if not LIVEMOTION.exists():
        print("nothing to restore")
        return 0
    n = 0
    for d in sorted(p for p in LIVEMOTION.iterdir() if p.is_dir()):
        for name in (CONFIG_NAME, CERT_NAME):
            f, bak = d / name, d / (name + BACKUP_SUFFIX)
            if bak.exists():
                if f.exists() or f.is_symlink():
                    f.unlink()
                shutil.copy2(bak, f)
                bak.unlink()
                print(f"  {d.name}/{name}: restored")
                n += 1
    if not n:
        print("no backups found; nothing to restore")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--provision", action="store_true")
    g.add_argument("--apply", action="store_true")
    g.add_argument("--restore", action="store_true")
    ap.add_argument("--app-key", action="append", default=None,
                    help=f"appKey directory (repeatable; default {DEFAULT_APP_KEY})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--cn", default=DEFAULT_CN)
    args = ap.parse_args(argv)

    keys = args.app_key or [DEFAULT_APP_KEY]

    if args.status:
        return status()
    if args.provision:
        return provision(keys, args.host, args.port, args.cn)
    if args.apply:
        return apply(keys, args.host, args.port, args.cn)
    return restore()


if __name__ == "__main__":
    raise SystemExit(main())
