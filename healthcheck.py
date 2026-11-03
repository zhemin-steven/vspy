#!/usr/bin/env python3
"""Container health check for vs.py, including direct-TLS mode."""

import os
import ssl
import sys
import urllib.request


def find_server_args() -> list[str]:
    try:
        pids = os.listdir("/proc")
    except OSError:
        return []

    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as handle:
                parts = [part.decode("utf-8", "replace") for part in handle.read().split(b"\0") if part]
        except OSError:
            continue
        if any(part.endswith("/vs.py") or part == "vs.py" for part in parts):
            return parts
    return []


SERVER_ARGS = find_server_args()


def server_arg(flag: str) -> str | None:
    parts = SERVER_ARGS

    for index, part in enumerate(parts):
        if part == flag and index + 1 < len(parts):
            return parts[index + 1]
        if part.startswith(flag + "="):
            return part.split("=", 1)[1]
    return None


port = server_arg("--port") or os.environ.get("VLESS_PORT", "10086")
use_tls = bool(server_arg("--cert") or os.environ.get("VLESS_CERT"))
scheme = "https" if use_tls else "http"
context = ssl._create_unverified_context() if use_tls else None
url = f"{scheme}://127.0.0.1:{port}/health"

try:
    with urllib.request.urlopen(url, timeout=3, context=context) as response:
        if response.status != 200:
            raise RuntimeError(f"unexpected HTTP status {response.status}")
except Exception as exc:
    print(f"healthcheck failed: {url}: {exc}", file=sys.stderr)
    sys.exit(1)
