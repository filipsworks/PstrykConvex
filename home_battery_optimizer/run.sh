#!/usr/bin/env bash
# Start the Home Battery Optimizer REST service with automatic persistence.
#
# If supervisor is installed, runs under it (auto-restart on crash).
# Otherwise falls back to direct execution.
#
# Usage:
#   ./run.sh                  # start directly
#   ./run.sh --host 127.0.0.1 --port 9000   # with custom args
#   ./run.sh supervisor       # force supervisor mode (fails if not installed)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_DIR="$SCRIPT_DIR"
VENV_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/.venv"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[error] Virtual environment not found at $VENV_DIR" >&2
    exit 1
fi

PYTHON="$VENV_DIR/bin/python"

# Check if supervisor is available
HAS_SUPERVISOR=false
if command -v supervisord &>/dev/null; then
    HAS_SUPERVISOR=true
elif "$PYTHON" -c "import supervisor" 2>/dev/null; then
    # Find the supervisord binary inside the venv
    SUPERVISORD_BIN="$VENV_DIR/bin/supervisord"
    if [[ -x "$SUPERVISORD_BIN" ]]; then
        HAS_SUPERVISOR=true
    fi
fi

# Parse arguments: extract --host/--port/--debug for direct mode,
# or "supervisor" keyword to force supervisor mode.
FORCE_SUPERVISOR=false
EXTRA_ARGS=()

for arg in "$@"; do
    if [[ "$arg" == "supervisor" ]]; then
        FORCE_SUPERVISOR=true
    else
        EXTRA_ARGS+=("$arg")
    fi
done

if $HAS_SUPERVISOR && ! $FORCE_SUPERVISOR; then
    # ── Supervisor mode (auto-restart) ────────────────────────────────
    echo "[supervisor] Starting under supervisord with auto-restart..."

    # Generate config with actual paths
    CONF="$SERVICE_DIR/supervisord.generated.conf"
    sed -e "s|__PYTHON__|$PYTHON|" \
        -e "s|__DIR__|$SERVICE_DIR|" \
        "$SERVICE_DIR/supervisord.conf" > "$CONF"

    exec "$SUPERVISORD_BIN" -c "$CONF"
elif $FORCE_SUPERVISOR && ! $HAS_SUPERVISOR; then
    echo "[error] supervisor is not installed. Install it with:" >&2
    echo "       pip install supervisor" >&2
    exit 1
else
    # ── Direct mode (no persistence) ──────────────────────────────────
    echo "[direct] Starting without supervisor (no auto-restart)." >&2
    echo "         Install supervisor for automatic restart on crash:" >&2
    echo "           pip install supervisor" >&2
    exec "$PYTHON" rest_service.py "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}"
fi
