#!/bin/bash
# Install the Antigravity auto-login so it runs at desktop startup.
# Idempotent: safe to re-run. Use --uninstall to remove.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
ENTRY="$AUTOSTART_DIR/antigravity-autologin.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$ENTRY"
    echo "removed $ENTRY"
    exit 0
fi

chmod +x "$ROOT/bin/ag-start.sh" "$ROOT/bin/shim/xdg-open" 2>/dev/null || true
chmod 600 "$ROOT/lab.env" 2>/dev/null || true
mkdir -p "$AUTOSTART_DIR" "$ROOT/logs"

cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=Antigravity Auto Login
Comment=Sign in to Antigravity and finish lab setup at startup
Exec=$ROOT/bin/ag-start.sh
Icon=antigravity
Terminal=false
X-GNOME-Autostart-enabled=true
StartupNotify=false
EOF

echo "installed $ENTRY"
echo
echo "credentials: $ROOT/lab.env  (env vars of the same name override it)"
echo "logs:        $ROOT/logs/"
echo
echo "run now without rebooting:   $ROOT/bin/ag-start.sh"
echo "re-run setup from scratch:   /opt/venv/bin/python3 $ROOT/bin/ag_autologin.py --force"
