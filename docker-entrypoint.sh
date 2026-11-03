#!/bin/sh
set -eu

# Explicit command-line arguments always take precedence.
if [ "$#" -gt 0 ]; then
    exec python /app/vs.py "$@"
fi

: "${VLESS_UUID:?VLESS_UUID must be set, for example: docker run -e VLESS_UUID=<uuid> ...}"

effective_port="${VLESS_PORT:-${PORT:-10086}}"
if [ -n "${PORT:-}" ] && [ "$PORT" != "$effective_port" ]; then
    echo "WARNING: PORT=$PORT but server will listen on $effective_port (VLESS_PORT takes precedence)." >&2
    echo "WARNING: if the platform proxy targets PORT=$PORT, external requests will return 502." >&2
fi

set -- \
    --host "${VLESS_HOST:-0.0.0.0}" \
    --port "$effective_port" \
    --uuid "$VLESS_UUID" \
    --path "${VLESS_PATH:-/vless}" \
    --log-level "${VLESS_LOG_LEVEL:-INFO}"

if [ -n "${VLESS_CERT:-}" ] || [ -n "${VLESS_KEY:-}" ]; then
    : "${VLESS_CERT:?VLESS_CERT and VLESS_KEY must be set together}"
    : "${VLESS_KEY:?VLESS_CERT and VLESS_KEY must be set together}"
    set -- "$@" --cert "$VLESS_CERT" --key "$VLESS_KEY"
fi

exec python /app/vs.py "$@"
