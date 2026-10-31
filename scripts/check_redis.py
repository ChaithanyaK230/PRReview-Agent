"""Check a Redis URL (e.g. Upstash) before giving it to the deploy. Never prints the password.

    set "REDIS_URL=rediss://default:...@....upstash.io:6379"
    uv run python -m scripts.check_redis
"""

import asyncio
import os
import sys
from urllib.parse import urlsplit

import redis.asyncio as redis

TIMEOUT_S = 10


async def main() -> int:
    url = os.environ.get("REDIS_URL", "")
    parts = urlsplit(url)
    print(f"scheme: {parts.scheme or '(missing)'}   host: {parts.hostname}   port: {parts.port}")
    if parts.scheme != "rediss" and (parts.hostname or "").endswith("upstash.io"):
        print("PROBLEM: Upstash needs TLS, so the URL must start with rediss:// (two s's)")
        return 1
    client = redis.from_url(url, socket_connect_timeout=TIMEOUT_S, socket_timeout=TIMEOUT_S)
    try:
        print("PING ->", await client.ping())
        print("OK: Redis is reachable")
        return 0
    except Exception as e:  # noqa: BLE001 - a diagnostic script: show whatever went wrong
        print(f"FAILED: {type(e).__name__}: {e}")
        if "timeout" in str(e).lower() or isinstance(e, TimeoutError):
            print("Hint: check the port (6379) and that your network doesn't block it; try a phone hotspot")
        return 1
    finally:
        await client.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
