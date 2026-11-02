"""sc-api -- an open telemetry API for Star Citizen, via D-BOX LiveMotion.

Star Citizen emits rich haptic/motion telemetry, but only over D-BOX's
proprietary LiveMotion gRPC protocol. `sc-api` implements the server side of
that protocol, so the game streams its telemetry to us, and republishes it as
plain newline-delimited JSON over UDP for any consumer to read.
"""
from __future__ import annotations

import pathlib
import sys

__version__ = "0.1.0"


def data_root() -> pathlib.Path:
    """Directory that holds `certs/`, `captures/` and friends at runtime.

    Under a PyInstaller build `__file__` points inside the extracted bundle in
    a temporary directory, which is the wrong place to look for a certificate
    the user generated and the wrong place to write a capture to. When frozen,
    resolve relative to the executable instead.
    """
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent.parent
