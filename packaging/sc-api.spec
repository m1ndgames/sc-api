# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for the sc-api server, producing a single sc-api.exe that
# needs no Python installed.
#
# Build:  pyinstaller --clean --noconfirm packaging/sc-api.spec
#
# Two things need saying explicitly:
#
#  * The generated protobuf stubs import each other by BARE MODULE NAME
#    (`import LiveMotion_pb2`), which is how protoc emits them. So `scapi/`
#    itself goes on the analysis path and both modules are named as hidden
#    imports -- PyInstaller cannot see them through that import form.
#
#  * Certificates and captures are resolved relative to the EXECUTABLE at
#    runtime, not to this bundle -- see `scapi.data_root()`. Nothing
#    user-supplied is baked in.
import pathlib

ROOT = pathlib.Path(SPECPATH).resolve().parent

a = Analysis(
    [str(ROOT / 'packaging' / 'sc_api_main.py')],
    pathex=[str(ROOT), str(ROOT / 'scapi')],
    binaries=[],
    datas=[],
    hiddenimports=['LiveMotion_pb2', 'LiveMotion_pb2_grpc'],
    hookspath=[],
    runtime_hooks=[],
    excludes=['tkinter', 'unittest', 'pydoc', 'doctest', 'email', 'http.server'],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name='sc-api',
    console=True,
    debug=False,
    strip=False,
    upx=False,
    bootloader_ignore_signals=False,
    disable_windowed_traceback=False,
)
