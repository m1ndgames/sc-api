#!/bin/sh
# Generate the TLS identity sc-api serves with.
#
# The D-BOX LiveMotion client expects a server certificate whose Common Name
# matches <CertificateCommonName> in LiveMotionConnector.config -- "LiveMotionComm"
# by default. SANs are included because modern TLS stacks ignore CN for
# hostname verification. CA:TRUE is set so the same file can also be dropped in
# as the client's trust anchor (livemotion.crt).
set -eu

# Git Bash / MSYS rewrites "/CN=..." into a Windows path. Harmless elsewhere.
MSYS_NO_PATHCONV=1
MSYS2_ARG_CONV_EXCL='*'
export MSYS_NO_PATHCONV MSYS2_ARG_CONV_EXCL

CN="${1:-LiveMotionComm}"
OUT="${2:-certs}"
mkdir -p "$OUT"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$OUT/livemotion.key" -out "$OUT/livemotion.crt" \
  -days 3650 \
  -subj "/C=CA/ST=Quebec/L=Longueuil/O=D-BOX Technologies/CN=$CN" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost,DNS:$CN" \
  -addext "basicConstraints=critical,CA:TRUE" \
  -addext "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign" \
  -addext "extendedKeyUsage=serverAuth"

echo "wrote $OUT/livemotion.crt and $OUT/livemotion.key (CN=$CN)"
