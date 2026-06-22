#!/bin/bash
# Forward local http://localhost:8501 -> the remote Streamlit dashboard.
# The server listens on 8501 but the firewall blocks it from the public
# internet; this SSH tunnel is the secure way to reach it from your machine.
set -euo pipefail

KEY="${KALSHI_SSH_KEY:-$HOME/.ssh/id_rsa}"
SERVER="${KALSHI_SSH_HOST:-ubuntu@your.server.ip}"
LOCAL_PORT="${DASHBOARD_LOCAL_PORT:-8501}"
REMOTE_PORT="${DASHBOARD_REMOTE_PORT:-8501}"

if [[ ! -f "$KEY" ]]; then
  echo "SSH key not found: $KEY" >&2
  echo "Set KALSHI_SSH_KEY to your SSH private key path." >&2
  exit 1
fi

if lsof -iTCP:"$LOCAL_PORT" -sTCP:LISTEN -t >/dev/null 2>&1; then
  echo "Dashboard tunnel already running on http://localhost:$LOCAL_PORT"
  exit 0
fi

echo "Starting dashboard tunnel -> http://localhost:$LOCAL_PORT"
exec ssh -N \
  -i "$KEY" \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 \
  -o ServerAliveCountMax=3 \
  -o StrictHostKeyChecking=accept-new \
  -L "${LOCAL_PORT}:127.0.0.1:${REMOTE_PORT}" \
  "$SERVER"
