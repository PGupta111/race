#!/usr/bin/env bash
# Big Red Command Center — one-shot install script (Debian/Ubuntu)
set -euo pipefail

RACE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Installing Big Red Command Center from: $RACE_DIR"

# ── 1. Virtual environment ──────────────────────────────────────────────────
if [ ! -d "$RACE_DIR/.venv" ]; then
    python3 -m venv "$RACE_DIR/.venv"
fi
"$RACE_DIR/.venv/bin/pip" install -q --upgrade pip

echo "[1/6] Installing core dependencies…"
"$RACE_DIR/.venv/bin/pip" install -q -r "$RACE_DIR/requirements.txt"

echo "[2/6] Installing optional ML/CV dependencies (ultralytics, opencv)…"
"$RACE_DIR/.venv/bin/pip" install -q ultralytics opencv-python-headless || \
    echo "  ⚠  ML/CV install failed — bib detection will run in simulation mode"

# ── 2. .env file ────────────────────────────────────────────────────────────
if [ ! -f "$RACE_DIR/.env" ]; then
    TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
    cp "$RACE_DIR/.env.example" "$RACE_DIR/.env"
    sed -i "s/RACE_API_TOKEN=.*/RACE_API_TOKEN=$TOKEN/" "$RACE_DIR/.env"
    echo "[3/6] Created .env with token: $TOKEN"
    echo "      ⚠  Save this token — you need it to log into the admin pages"
else
    echo "[3/6] .env already exists — skipping"
fi

# ── 3. TLS certificate ──────────────────────────────────────────────────────
echo "[4/6] Generating self-signed TLS certificate…"
bash "$RACE_DIR/deploy/gen_cert.sh"

# ── 4. nginx ────────────────────────────────────────────────────────────────
echo "[5/6] Configuring nginx…"
if command -v nginx &>/dev/null; then
    cp "$RACE_DIR/deploy/nginx.conf" /etc/nginx/sites-available/bigred
    ln -sf /etc/nginx/sites-available/bigred /etc/nginx/sites-enabled/bigred
    rm -f /etc/nginx/sites-enabled/default
    nginx -t && systemctl reload nginx
else
    echo "  ⚠  nginx not found — skipping. Install with: sudo apt install nginx"
fi

# ── 5. systemd ──────────────────────────────────────────────────────────────
echo "[6/6] Installing systemd units…"
SYSTEMD_DIR=/etc/systemd/system
cp "$RACE_DIR/deploy/bigred.service"        "$SYSTEMD_DIR/"
cp "$RACE_DIR/deploy/bigred-backup.service" "$SYSTEMD_DIR/"
cp "$RACE_DIR/deploy/bigred-backup.timer"   "$SYSTEMD_DIR/"
systemctl daemon-reload
systemctl enable --now bigred.service
systemctl enable --now bigred-backup.timer

echo ""
echo "✅  Big Red Command Center installed and running."
echo "    App:    https://$(hostname -I | awk '{print $1}')"
echo "    Token:  $(grep RACE_API_TOKEN $RACE_DIR/.env | cut -d= -f2)"
echo ""
echo "    Logs:   journalctl -u bigred -f"
echo "    Backup: journalctl -u bigred-backup"
