#!/bin/bash
# Install a login-time SSH tunnel so the dashboard stays reachable at
# http://localhost:8501 whenever your Mac is on.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.kalshi15m.dashboard-tunnel.plist"
TUNNEL="$ROOT/scripts/dashboard_tunnel.sh"

chmod +x "$TUNNEL"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.kalshi15m.dashboard-tunnel</string>
  <key>ProgramArguments</key>
  <array>
    <string>$TUNNEL</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>KALSHI_SSH_KEY</key>
    <string>$HOME/.ssh/id_rsa</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$HOME/Library/Logs/kalshi15m-dashboard-tunnel.log</string>
  <key>StandardErrorPath</key>
  <string>$HOME/Library/Logs/kalshi15m-dashboard-tunnel.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.kalshi15m.dashboard-tunnel" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.kalshi15m.dashboard-tunnel"
launchctl kickstart -k "gui/$(id -u)/com.kalshi15m.dashboard-tunnel"

echo "Installed. Dashboard: http://localhost:8501"
echo "Logs: $HOME/Library/Logs/kalshi15m-dashboard-tunnel.log"
echo "Remove with: launchctl bootout gui/$(id -u)/com.kalshi15m.dashboard-tunnel"
