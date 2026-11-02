# Local cache of the D-BOX LiveMotion connector

A place to keep **your own copy** of the unmodified D-BOX connector files, so this
project keeps working after the D-BOX software is uninstalled.

> **Nothing of D-BOX's is in this repository.** Not `dbxLive64.dll`, not their config
> files, not their certificate. This README is the only tracked file in this directory —
> everything else is ignored by git and absent from release builds.
>
> **Get the files from the D-BOX installer.** Install the D-BOX HaptiSync Center, then
> copy what you need out of
> `C:\ProgramData\D-BOX\Gaming\LiveMotion\LiveMotionConnector\` into this directory. That
> installer is the only legitimate source.
>
> Do it **before** running D-BOX's uninstaller, which deletes `C:\ProgramData\D-BOX`.
> Nothing here can regenerate `dbxLive64.dll`; you would have to reinstall to get it back.

## Why the DLL is needed at all

Star Citizen has the D-BOX Live Motion SDK statically linked, and that SDK loads its
transport DLL at runtime from a path it builds itself. The format string is visible in
`StarCitizen.exe`:

```
%sD-BOX\Gaming\LiveMotion\%s\%s
 |                        |   |
 base (ProgramData)       |   dbxLive64.dll
                          appKey ("StarCitizen")
```

So the game requires `C:\ProgramData\D-BOX\Gaming\LiveMotion\StarCitizen\dbxLive64.dll`
to exist. It does **not** read the registry for this, and does **not** need
`C:\Program Files\D-BOX` or any D-BOX service — `sc-api` replaces MotionEngine, which is
the only thing the DLL talks to. That one directory is the entire runtime requirement.

Rebuild it, with D-BOX uninstalled, from a copy kept here:

```bash
sh scripts/make_cert.sh
python tools/patch_connector.py --provision
```

## What goes here

| File | Needed for | Notes |
|---|---|---|
| `dbxLive64.dll` | **required** by `--provision` | the gRPC client the game loads. 10,326,624 bytes, dated 2025-09-19, SHA-256 `85a6e973b3290c4fd39c1ccc2e1a7156c4646ce3987f42976c8fa7747982ba6a` for HaptiSync Center 1.4.0 |
| `LiveMotionConnector.log.config` | optional | enables the DLL's own spdlog output. `--provision` installs it if present and skips it otherwise |

`patch_connector.py --status` prints the SHA-256 of whatever `dbxLive64.dll` is actually
installed and says whether it is the genuine, unmodified file. Worth checking.

`dbxLive32.dll` is not needed: Star Citizen is 64-bit only.

## The two files this project writes itself

Neither is copied from D-BOX — `patch_connector.py` generates them, which is why no
original of either is needed here.

**`LiveMotionConnector.config`** is the whole of the client's configuration. D-BOX's own
ships as `127.0.0.1:61666` with CN `LiveMotionComm`; `--apply` and `--provision` write the
same three elements pointing at `sc-api` instead (and `--apply` keeps a
`.sc-api-backup` of the original in place, which is what `--restore` reads):

```xml
<?xml version="1.0" encoding="utf-8"?>
<Configuration>
	<ReceiverIpAddress>127.0.0.1</ReceiverIpAddress>
	<ReceiverPort>51666</ReceiverPort>
	<CertificateCommonName>LiveMotionComm</CertificateCommonName>
</Configuration>
```

**`livemotion.crt`** is the certificate the client will trust. D-BOX's own is a
self-signed certificate valid 2023-07-04 to 2033-07-01 with subject

```
C=CA, ST=Quebec, L=Longueuil, O=D-BOX Technologies, CN=LiveMotionComm
```

We cannot sign with their private key, so `scripts/make_cert.sh` generates an equivalent
self-signed identity — matching that subject, because the client checks the Common Name
against `<CertificateCommonName>` — and `patch_connector.py` installs ours in its place.
There is no pinning and no mutual TLS, which is why this works at all.

## On replacing the DLL

Replacing `dbxLive64.dll` with a **custom** build is a different matter entirely and is
not done here. Star Citizen ships EasyAntiCheat, which is kernel-level, and an unsigned
module adopting a signed module's filename inside the protected process is
indistinguishable from DLL hijacking regardless of what it actually does. See README
section 3. Use D-BOX's own signed DLL, unmodified.
