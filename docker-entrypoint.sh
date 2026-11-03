#!/bin/sh
set -eu

# Explicit command-line arguments always take precedence.
if [ "$#" -gt 0 ]; then
    exec python /app/vs.py "$@"
fi

: "${VLESS_UUID:?VLESS_UUID must be set, for example: docker run -e VLESS_UUID=<uuid> ...}"

set -- \
    --host "${VLESS_HOST:-0.0.0.0}" \
    --port "${VLESS_PORT:-${PORT:-10086}}" \
    --uuid "$VLESS_UUID" \
    --path "${VLESS_PATH:-/vless}" \
    --log-level "${VLESS_LOG_LEVEL:-INFO}"

if [ -n "${VLESS_CERT:-}" ] || [ -n "${VLESS_KEY:-}" ]; then
    : "${VLESS_CERT:?VLESS_CERT and VLESS_KEY must be set together}"
    : "${VLESS_KEY:?VLESS_CERT and VLESS_KEY must be set together}"
    set -- "$@" --cert "$VLESS_CERT" --key "$VLESS_KEY"
fi

exec python /app/vs.py "$@"
