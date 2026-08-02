"""The gateway must survive being dropped, because it will be.

Discord closes connections routinely -- for maintenance, with op 7, or by
simply going quiet, which is what "no close frame received" looks like from
this end. These tests run a real local websocket server and cut it off in each
of those ways.
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import rsb.discord.gateway as gw
from rsb.discord.gateway import FATAL_CLOSE_CODES, DiscordGateway, GatewayError
from websockets.asyncio.server import serve

ok = lambda m: print(f"[ok] {m}")

HEARTBEAT_MS = 120


class FakeDiscord:
    """A gateway that speaks just enough protocol to be cut off convincingly."""

    def __init__(self, *, ack=True, close_code=None, drop_after=None,
                 op7_after=None, invalid_session_after=None):
        self.ack = ack
        self.close_code = close_code
        self.drop_after = drop_after
        self.op7_after = op7_after
        self.invalid_session_after = invalid_session_after
        self.identifies = 0
        self.resumes = 0
        self.connections = 0
        self.heartbeats = 0
        self.sessions: list[str] = []
        self._server = None
        self.url = ""

    async def start(self):
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = next(iter(self._server.sockets)).getsockname()[1]
        self.url = f"ws://127.0.0.1:{port}"
        return self.url

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle(self, ws):
        self.connections += 1
        mine = self.connections
        await ws.send(json.dumps(
            {"op": 10, "d": {"heartbeat_interval": HEARTBEAT_MS}}
        ))
        received = 0
        try:
            async for raw in ws:
                msg = json.loads(raw)
                op = msg.get("op")
                received += 1

                # a rejected token is refused at the handshake; READY never
                # arrives, so this has to come before the op handling
                if self.close_code and mine == 1:
                    await ws.close(code=self.close_code)
                    return

                if op == 2:  # IDENTIFY
                    self.identifies += 1
                    session = f"session-{self.identifies}"
                    self.sessions.append(session)
                    await ws.send(json.dumps({
                        "op": 0, "s": 1, "t": "READY",
                        "d": {
                            "user": {"id": "9", "username": "me"},
                            "session_id": session,
                            "resume_gateway_url": self.url,
                        },
                    }))
                elif op == 6:  # RESUME
                    self.resumes += 1
                    await ws.send(json.dumps(
                        {"op": 0, "s": 2, "t": "RESUMED", "d": {}}
                    ))
                elif op == 1:  # heartbeat
                    self.heartbeats += 1
                    if self.ack:
                        await ws.send(json.dumps({"op": 11}))

                if self.op7_after and mine == 1 and received >= self.op7_after:
                    await ws.send(json.dumps({"op": 7, "d": None}))
                    await ws.close()
                    return
                if self.invalid_session_after and mine == 1 and received >= self.invalid_session_after:
                    await ws.send(json.dumps({"op": 9, "d": False}))
                    await ws.close()
                    return
                if self.drop_after and mine == 1 and received >= self.drop_after:
                    # vanish without a close frame, the way a dead link does
                    ws.transport.abort()
                    return
        except Exception:
            return


async def connected(server, **kwargs):
    gw.RECONNECT_BASE = 0.05
    gw.RECONNECT_MAX = 0.2
    gw.GATEWAY_URL = server.url
    client = DiscordGateway("fake.test.token")
    for key, value in kwargs.items():
        setattr(client, key, value)
    await client.connect(timeout=10)
    return client


async def test_survives_an_abrupt_drop():
    """The exact reported failure: closed with no close frame."""
    server = FakeDiscord(drop_after=2)
    await server.start()
    try:
        client = await connected(server)
        assert client.user is not None
        ok("connected and READY")

        for _ in range(60):
            await asyncio.sleep(0.1)
            if server.connections >= 2 and client._connected.is_set():
                break

        assert server.connections >= 2, server.connections
        ok(f"the socket was dropped without a close frame and reopened "
           f"({server.connections} connections)")
        assert client.reconnects >= 1
        assert client._fatal is None, client._fatal
        ok(f"{client.reconnects} reconnect(s), and nothing was treated as fatal")
        assert client._connected.is_set()
        ok("it is connected again, so a scan in progress carries on")
        await client.close()
    finally:
        await server.stop()


async def test_resumes_rather_than_reidentifying():
    server = FakeDiscord(drop_after=2)
    await server.start()
    try:
        client = await connected(server)
        for _ in range(60):
            await asyncio.sleep(0.1)
            if server.resumes >= 1:
                break
        assert server.resumes >= 1, (server.identifies, server.resumes)
        assert server.identifies == 1, server.identifies
        ok(f"it resumed the session ({server.resumes} resume, "
           f"{server.identifies} identify) rather than starting over")
        await client.close()
    finally:
        await server.stop()


async def test_zombie_connection_is_detected():
    """A socket that stops acknowledging heartbeats is dead, not idle."""
    server = FakeDiscord(ack=False)
    await server.start()
    try:
        client = await connected(server)
        ok("connected to a server that never acknowledges heartbeats")

        for _ in range(80):
            await asyncio.sleep(0.1)
            if server.connections >= 2:
                break
        assert server.connections >= 2, (
            f"only {server.connections} connection(s); a missing ACK went "
            f"unnoticed and the link stayed silently dead"
        )
        ok(f"the missing ACK was noticed and the link rebuilt "
           f"({server.connections} connections)")
        assert client._fatal is None
        await client.close()
    finally:
        await server.stop()


async def test_op7_and_invalid_session_are_routine():
    for label, kwargs in [
        ("op 7 reconnect", {"op7_after": 2}),
        ("invalid session", {"invalid_session_after": 2}),
    ]:
        server = FakeDiscord(**kwargs)
        await server.start()
        try:
            client = await connected(server)
            for _ in range(80):
                await asyncio.sleep(0.1)
                if server.connections >= 2 and client._connected.is_set():
                    break
            assert server.connections >= 2, (label, server.connections)
            assert client._fatal is None, (label, client._fatal)
            ok(f"{label} is handled as routine, not fatal "
               f"({server.connections} connections)")
            await client.close()
        finally:
            await server.stop()


async def test_auth_failure_is_fatal():
    """4004 means the token is wrong; retrying forever would be pointless."""
    server = FakeDiscord(close_code=4004)
    await server.start()
    try:
        gw.RECONNECT_BASE = 0.05
        gw.GATEWAY_URL = server.url
        client = DiscordGateway("fake.test.token")
        try:
            await client.connect(timeout=6)
            raise AssertionError("connected despite a 4004")
        except GatewayError as exc:
            assert "4004" in str(exc) or "authentication" in str(exc).lower()
            ok(f"a 4004 stops immediately: {exc}")
        assert server.connections <= 3, (
            f"{server.connections} attempts against a rejected token"
        )
        ok(f"and it does not hammer the endpoint ({server.connections} attempts)")
        await client.close()
    finally:
        await server.stop()

    assert 4004 in FATAL_CLOSE_CODES and 4014 in FATAL_CLOSE_CODES
    assert 4000 not in FATAL_CLOSE_CODES and 1006 not in FATAL_CLOSE_CODES
    ok("only the codes that mean 'do not bother retrying' are fatal")


async def test_send_waits_through_a_reconnect():
    """A send during a reconnect should pause, not raise.

    Deliberately without a server: with one running, the supervisor reopens the
    socket on its own timing and the test would be racing it rather than
    testing the gate.
    """
    sent = []

    class StubSocket:
        async def send(self, payload):
            sent.append(payload)

        async def close(self):
            pass

    client = DiscordGateway("fake.test.token")
    gw.RECONNECT_WAIT = 5.0

    async def restore():
        await asyncio.sleep(0.3)
        client._ws = StubSocket()
        client._socket_ready.set()

    task = asyncio.create_task(restore())
    await client._send({"op": 1, "d": None}, metered=False)
    await task
    assert sent, "the send never went out"
    ok(f"a send issued while disconnected waited {0.3:.1f}s and then went out")

    client._ws = None
    client._socket_ready.clear()
    gw.RECONNECT_WAIT = 0.2
    try:
        await client._send({"op": 1, "d": None}, metered=False)
        raise AssertionError("send succeeded with no connection")
    except GatewayError:
        ok("but it gives up rather than hanging forever")
    finally:
        gw.RECONNECT_WAIT = 90.0

    # and the handshake is never gated on the handshake completing
    client._ws = StubSocket()
    client._socket_ready.set()
    client._connected.clear()
    await client._identify()
    assert len(sent) >= 2
    ok("IDENTIFY sends before READY, so the handshake cannot deadlock itself")


async def test_close_stops_reconnecting():
    server = FakeDiscord(drop_after=2)
    await server.start()
    try:
        client = await connected(server)
        await client.close()
        settled = server.connections
        await asyncio.sleep(0.6)
        assert server.connections == settled, (
            f"kept reconnecting after close ({settled} -> {server.connections})"
        )
        ok("closing deliberately stops the supervisor rather than looping")
    finally:
        await server.stop()


async def main():
    await test_survives_an_abrupt_drop()
    print()
    await test_resumes_rather_than_reidentifying()
    await test_zombie_connection_is_detected()
    print()
    await test_op7_and_invalid_session_are_routine()
    print()
    await test_auth_failure_is_fatal()
    await test_send_waits_through_a_reconnect()
    await test_close_stops_reconnecting()
    print("\nALL GATEWAY RESILIENCE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
