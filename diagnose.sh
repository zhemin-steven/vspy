#!/bin/sh
set -u

effective_port="${VLESS_PORT:-${PORT:-10086}}"
scheme="http"
curl_tls_opts=""
if [ -n "${VLESS_CERT:-}" ]; then
    scheme="https"
    curl_tls_opts="-k"
fi

echo "=== environment ==="
env | sort | grep -E '^(PORT|HOST|VLESS_)=' || true
printf 'effective_port=%s\n' "$effective_port"

echo "=== process ==="
ps -ef | grep '[v]s.py' || echo "vs.py is not running"

echo "=== listeners ==="
ss -lntp 2>/dev/null || netstat -lntp 2>/dev/null || true

echo "=== local health (IPv4) ==="
curl $curl_tls_opts -sS -v --max-time 5 "${scheme}://127.0.0.1:${effective_port}/health" || true

echo "=== local health (IPv6) ==="
curl $curl_tls_opts -g -6 -sS -v --max-time 5 "${scheme}://[::1]:${effective_port}/health" || true

echo "=== localhost resolution ==="
getent hosts localhost || true

echo "=== hint ==="
if [ -n "${PORT:-}" ] && [ "$PORT" != "$effective_port" ]; then
    echo "PORT=$PORT differs from effective_port=$effective_port; the platform proxy may be routing to PORT."
fi
