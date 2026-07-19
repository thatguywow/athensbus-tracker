#!/usr/bin/env bash
# Athens Bus Tracker — self-hosted dashboard server (χωρίς GitHub).
# PORT / CYCLE_MINUTES: άλλαξέ τα εδώ ή δώσε τα ως μεταβλητές περιβάλλοντος.
cd "$(dirname "$0")/../.."
export PORT="${PORT:-8000}"
export CYCLE_MINUTES="${CYCLE_MINUTES:-15}"
exec python3 scripts/serve_site.py --port "$PORT" --interval "$CYCLE_MINUTES"
