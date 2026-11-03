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
env | sort | grep -E '^(PORT|HOST|VLESS_[A-Z0-9_]*)=' || true
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

echo "=== default route ==="
ip route 2>/dev/null || true
echo "--- IPv6 route ---"
ip -6 route 2>/dev/null || true

echo "=== outbound DNS (IPv4) ==="
getent ahostsv4 example.com | head -n 3 || true
getent ahostsv4 www.google.com | head -n 3 || true

echo "=== outbound DNS (IPv6) ==="
getent ahostsv6 www.google.com | head -n 3 || true
if ! ip -6 route 2>/dev/null | grep -q '^default '; then
    echo "No IPv6 default route. Direct IPv6 connects will hang; vs.py falls back to IPv4."
fi

echo "=== proxy environment ==="
if env | grep -iE '^(http|https|all|no)_proxy='; then
    echo "Proxy variables are set. curl may be using the proxy, but vs.py uses direct TCP."
else
    echo "no proxy variables"
fi

echo "=== outbound TCP ==="
check_tcp() {
    target_host="$1"
    target_port="$2"
    printf '%s:%s ... ' "$target_host" "$target_port"
    if nc -z -w 5 "$target_host" "$target_port" 2>&1; then
        echo "ok"
    else
        echo "failed"
    fi
}
check_tcp 1.1.1.1 443
check_tcp example.com 443
check_tcp www.google.com 443

echo "=== direct TCP by address family (Python) ==="
python - <<'PY' || true
import asyncio
import socket
import time


async def main():
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(
            "www.google.com",
            443,
            type=socket.SOCK_STREAM,
        )
    except Exception as exc:
        print(f"getaddrinfo failed: {exc!r}")
        return

    seen = set()
    for family, socktype, proto, _, sockaddr in infos:
        key = (family, sockaddr)
        if key in seen:
            continue
        seen.add(key)
        family_name = "IPv4" if family == socket.AF_INET else "IPv6"
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.setblocking(False)
        started = time.monotonic()
        try:
            await asyncio.wait_for(loop.sock_connect(sock, sockaddr), timeout=5)
            print(f"{family_name} {sockaddr[0]}:443 ok in {time.monotonic() - started:.2f}s")
        except Exception as exc:
            print(
                f"{family_name} {sockaddr[0]}:443 failed in "
                f"{time.monotonic() - started:.2f}s: {exc.__class__.__name__}: {exc}"
            )
        finally:
            sock.close()

    started = time.monotonic()
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(
                "www.google.com",
                443,
                happy_eyeballs_delay=0.25,
                interleave=1,
            ),
            timeout=10,
        )
        print(
            f"happy-eyeballs connect ok in {time.monotonic() - started:.2f}s "
            f"via {writer.get_extra_info('peername')}"
        )
        writer.close()
        await writer.wait_closed()
    except Exception as exc:
        print(
            f"happy-eyeballs connect failed in {time.monotonic() - started:.2f}s: "
            f"{exc.__class__.__name__}: {exc}"
        )


asyncio.run(main())
PY

echo "=== outbound HTTPS ==="
curl -sS -o /dev/null -w 'https://example.com status=%{http_code} connect=%{time_connect}s\n' \
    --max-time 10 https://example.com || true
curl -sS -o /dev/null -w 'https://www.google.com status=%{http_code} connect=%{time_connect}s\n' \
    --max-time 10 https://www.google.com || true

echo "=== hint ==="
if [ -n "${PORT:-}" ] && [ "$PORT" != "$effective_port" ]; then
    echo "PORT=$PORT differs from effective_port=$effective_port; the platform proxy may be routing to PORT."
fi
echo "If every outbound TCP/HTTPS check fails, the platform blocks egress; VLESS cannot proxy until outbound access is enabled."
