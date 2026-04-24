#!/usr/bin/env bash
# Generate a self-signed TLS certificate for LAN use.
# Browsers will show a warning; add an exception once per device.
set -euo pipefail

CERT_DIR=/etc/nginx/ssl
LAN_IP=$(hostname -I | awk '{print $1}')

mkdir -p "$CERT_DIR"

openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
    -keyout "$CERT_DIR/bigred.key" \
    -out    "$CERT_DIR/bigred.crt" \
    -subj   "/C=US/ST=NY/L=Ithaca/O=BigRed/CN=bigred.local" \
    -addext "subjectAltName=IP:${LAN_IP},DNS:localhost,DNS:bigred.local"

chmod 600 "$CERT_DIR/bigred.key"
echo "Certificate generated:"
echo "  cert: $CERT_DIR/bigred.crt"
echo "  key:  $CERT_DIR/bigred.key"
echo "  SAN:  IP:${LAN_IP}, DNS:localhost, DNS:bigred.local"
