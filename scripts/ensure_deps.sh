#!/bin/bash
# systemd ExecStartPre for the bot. The small instance (≈400MB) cannot import all
# deps in one process — that OOM-kills — and a blind reinstall on every restart
# also risks an OOM that leaves the venv half-built, which is how the engine ended
# up crash-looping on a missing 'requests'. So: check each module in its OWN
# process (peak memory = one lib), install only what's missing, verify the same
# way, and never import the whole set at once.
set -e
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv

mods=(requests cryptography dotenv pandas plotly streamlit)
declare -A pkg=([dotenv]=python-dotenv)

# Fast path: if every module already imports (one process each), do nothing.
missing=0
for m in "${mods[@]}"; do
  .venv/bin/python -c "import $m" 2>/dev/null || { missing=1; break; }
done
[ "$missing" = 0 ] && exit 0

# Install only what's actually missing.
for m in "${mods[@]}"; do
  .venv/bin/python -c "import $m" 2>/dev/null && continue
  .venv/bin/pip install -q --no-cache-dir "${pkg[$m]:-$m}"
done

# Verify each in its own process (low peak memory); fail loudly if any missing.
for m in "${mods[@]}"; do
  .venv/bin/python -c "import $m"
done
