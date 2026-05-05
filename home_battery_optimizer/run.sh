#!/usr/bin/env bash
# Start the Home Battery Optimizer REST service with automatic persistence.
#
# If supervisor is installed, runs under it (auto-restart on crash).
# Otherwise falls back to direct execution via uvicorn.
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
UVICORN="$VENV_DIR/bin/uvicorn"

# Check if supervisor is available
HAS_SUPERVISOR=false
if command -v supervisord &>/dev/null; then
    HAS_SUPERVISOR=true
elif "$PYTHON" -c "import supervisor" 2>/dev/null; then
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

# Build uvicorn command with host/port from args or defaults
UVICORN_HOST="0.0.0.0"
UVICORN_PORT=8000
for ((i=0; i<${#EXTRA_ARGS[@]}; i++)); do
    case "${EXTRA_ARGS[$i]}" in
        --host)  UVICORN_HOST="${EXTRA_ARGS[$((i+1))]}"; ((i++)) ;;
        --port)  UVICORN_PORT="${EXTRA_ARGS[$((i+1))]}"; ((i++)) ;;
    esac
done

if $HAS_SUPERVISOR && ! $FORCE_SUPERVISOR; then
    # ── Supervisor mode (auto-restart) ────────────────────────────────
    echo "[supervisor] Starting under supervisord with auto-restart..."

    CONF="$SERVICE_DIR/supervisord.generated.conf"
    cat > "$CONF" <<EOF
[supervisord]
nodaemon=true
logfile=/dev/null
logfile_maxbytes=0
pidfile=/tmp/supervisord.pid

[program:home-battery-optimizer]
command=$UVICORN rest_service:asgi_app --host $UVICORN_HOST --port $UVICORN_PORT
directory=$SERVICE_DIR
autostart=true
autorestart=true
startsecs=3
startretries=5
stopwaitsecs=10
redirect_stderr=true
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
EOF

    exec "$SUPERVISORD_BIN" -c "$CONF"
elif $FORCE_SUPERVISOR && ! $HAS_SUPERVISOR; then
    echo "[error] supervisor is not installed. Install it with:" >&2
    echo "       pip install supervisor" >&2
    exit 1
else
    # ── Direct mode (no persistence) ──────────────────────────────────
    echo "[direct] Starting via uvicorn (no auto-restart)." >&2
    echo "         Install supervisor for automatic restart on crash:" >&2
    echo "           pip install supervisor" >&2
    exec "$UVICORN" rest_service:asgi_app --host "$UVICORN_HOST" --port "$UVICORN_PORT"
fi
