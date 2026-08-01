"""A stand-in Rotector API and real local HTTP proxies, for routing tests.

The mock enforces a rate limit *per bucket*, and each forward proxy stamps its
own bucket id on what it forwards -- so N proxies behave like N distinct exit
IPs, which is exactly the property the real proxy support depends on. Nothing
is mocked at the httpx layer: the client opens real sockets to real proxies
which forward to a real server.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from urllib.parse import urlsplit

DIRECT_BUCKET = "direct"


class MockRotector:
    """Minimal Rotector-shaped API with a per-bucket sliding-window limit."""

    def __init__(self, limit: int = 50, window: float = 2.0, latency: float = 0.0):
        self.limit = limit
        self.window = window
        self.latency = latency
        self._buckets: dict[str, deque[tuple[float, int]]] = {}
        self.requests: dict[str, int] = {}
        self.ids_served: set[str] = set()
        self.rejections = 0
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    # -- rate accounting ---------------------------------------------------

    def _spend(self, bucket: str, cost: int) -> tuple[bool, int, float]:
        now = time.time()
        events = self._buckets.setdefault(bucket, deque())
        while events and events[0][0] <= now - self.window:
            events.popleft()
        used = sum(c for _, c in events)
        reset = (events[0][0] + self.window) if events else (now + self.window)
        if used + cost > self.limit:
            return False, max(0, self.limit - used), reset
        events.append((now, cost))
        return True, max(0, self.limit - used - cost), reset

    # -- http --------------------------------------------------------------

    async def _handle(self, reader, writer) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            headers = await _read_headers(reader)
            body = await _read_body(reader, headers)

            if target.startswith("http://"):
                target = urlsplit(target).path or "/"

            bucket = headers.get("x-bucket", DIRECT_BUCKET)
            self.requests[bucket] = self.requests.get(bucket, 0) + 1

            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {}
            ids = [str(i) for i in (payload.get("ids") or [])]
            cost = max(1, math.ceil(len(ids) / 50)) if ids else 1

            allowed, remaining, reset = self._spend(bucket, cost)
            extra = {
                "X-RateLimit-Limit": str(self.limit),
                "X-RateLimit-Remaining": str(remaining),
                "X-RateLimit-Reset": f"{reset:.0f}",
            }
            if not allowed:
                self.rejections += 1
                extra["Retry-After"] = f"{max(0.1, reset - time.time()):.1f}"
                _respond(writer, 429, {"success": False, "error": "rate limited"}, extra)
                return

            if self.latency:
                await asyncio.sleep(self.latency)

            if "/roblox/user" in target:
                data = {i: {"id": int(i), "flagType": 0} for i in ids}
            else:
                self.ids_served.update(ids)
                data = {
                    i: {"id": i, "servers": [], "connections": []} for i in ids
                }
            _respond(writer, 200, {"success": True, "data": data}, extra)
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        finally:
            try:
                await writer.drain()
                writer.close()
            except Exception:
                pass


class ForwardProxy:
    """A real HTTP forward proxy that tags what it relays with a bucket id."""

    def __init__(self, bucket: str, fail: bool = False):
        self.bucket = bucket
        self.fail = fail
        self.forwarded = 0
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{self.port}"

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, reader, writer) -> None:
        upstream_w = None
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").split()
            if len(parts) < 2:
                return
            method, target = parts[0], parts[1]
            headers = await _read_headers(reader)
            body = await _read_body(reader, headers)

            if self.fail:
                _respond(writer, 502, {"success": False, "error": "bad gateway"}, {})
                return

            split = urlsplit(target)
            host, port = split.hostname, split.port or 80
            path = split.path or "/"
            if split.query:
                path += "?" + split.query

            upstream_r, upstream_w = await asyncio.open_connection(host, port)
            headers["host"] = f"{host}:{port}"
            headers["x-bucket"] = self.bucket
            headers["connection"] = "close"
            headers.pop("proxy-connection", None)

            out = [f"{method} {path} HTTP/1.1"]
            out += [f"{k}: {v}" for k, v in headers.items()]
            upstream_w.write(("\r\n".join(out) + "\r\n\r\n").encode("latin-1") + body)
            await upstream_w.drain()
            self.forwarded += 1

            while True:
                chunk = await upstream_r.read(65536)
                if not chunk:
                    break
                writer.write(chunk)
            await writer.drain()
        except Exception:
            pass
        finally:
            for stream in (upstream_w, writer):
                try:
                    if stream:
                        stream.close()
                except Exception:
                    pass


# --------------------------------------------------------------------------


async def _read_headers(reader) -> dict[str, str]:
    headers: dict[str, str] = {}
    while True:
        line = await reader.readline()
        if not line or line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("latin-1").partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


async def _read_body(reader, headers: dict[str, str]) -> bytes:
    length = int(headers.get("content-length") or 0)
    return await reader.readexactly(length) if length else b""


def _respond(writer, status: int, payload: dict, extra: dict) -> None:
    body = json.dumps(payload).encode()
    lines = [
        f"HTTP/1.1 {status} {'OK' if status < 400 else 'ERR'}",
        "Content-Type: application/json",
        f"Content-Length: {len(body)}",
        "Connection: close",
        *(f"{k}: {v}" for k, v in extra.items()),
    ]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
