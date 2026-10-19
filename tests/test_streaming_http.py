"""Real TCP/HTTP evidence, with no paid provider or ASGI transport shortcut."""
import asyncio
from contextlib import asynccontextmanager

import httpx
import pytest

from ai_gateway.transport import StreamingTransport, TransportError
from tests.test_transport import REQUEST, WIRE, adapter


@asynccontextmanager
async def upstream(*, send_first=True):
    release, closed = asyncio.Event(), asyncio.Event()
    active = set()
    failures = []

    async def serve(reader, writer):
        task = asyncio.current_task()
        active.add(task)
        pending = []
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            length = next(int(line.split(b":", 1)[1]) for line in headers.split(b"\r\n")
                          if line.lower().startswith(b"content-length:"))
            await reader.readexactly(length)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            if send_first:
                writer.write(WIRE.split(b"data: [DONE]")[0])
            await writer.drain()
            pending = [asyncio.create_task(release.wait()), asyncio.create_task(reader.read())]
            done, _ = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if pending[0] in done:
                writer.write(b"data: [DONE]\n\n")
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as exc:
            failures.append(exc)
        finally:
            for item in pending:
                item.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            writer.close()
            await writer.wait_closed()
            active.discard(task)
            closed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield {**REQUEST, "url": f"http://127.0.0.1:{port}/stream"}, release, closed, active
    finally:
        server.close()
        await server.wait_closed()
        for task in list(active):
            task.cancel()
        await asyncio.gather(*active, return_exceptions=True)
        assert not failures


async def test_first_event_arrives_before_upstream_completion():
    terminals = []
    async with upstream() as (request, release, closed, active):
        async with httpx.AsyncClient(trust_env=False) as client:
            async with StreamingTransport(client).stream(request, adapter, lambda *x: terminals.append(x)) as stream:
                first = await asyncio.wait_for(anext(stream), 1)
                assert b"choices" in first and not release.is_set() and active
                release.set()
                rest = b"".join([x async for x in stream])
            await asyncio.wait_for(closed.wait(), 1)
            assert not active
    assert rest == b"data: [DONE]\n\n" and terminals == [(None, "success")]


async def test_disconnect_releases_connection_and_pool_slot():
    terminals = []
    async with httpx.AsyncClient(trust_env=False, limits=httpx.Limits(max_connections=1)) as client:
        for _ in range(3):
            async with upstream() as (request, release, closed, active):
                transport = StreamingTransport(client, timeout=httpx.Timeout(1))
                async with transport.stream(request, adapter, lambda *x: terminals.append(x)) as stream:
                    assert b"choices" in await anext(stream)
                    # Router exits the context on downstream disconnect.
                await asyncio.wait_for(closed.wait(), 1)
                assert not active
    assert terminals == [(None, "cancelled")] * 3


@pytest.mark.parametrize("send_first", [False, True])
async def test_real_read_timeout_closes_upstream(send_first):
    terminals = []
    async with upstream(send_first=send_first) as (request, release, closed, active):
        async with httpx.AsyncClient(trust_env=False) as client:
            transport = StreamingTransport(client, timeout=httpx.Timeout(1, read=0.05))
            if send_first:
                async with transport.stream(request, adapter, lambda *x: terminals.append(x)) as stream:
                    wire = b"".join([x async for x in stream])
                assert b"event: error" in wire and b"[DONE]" not in wire
            else:
                with pytest.raises(TransportError) as exc:
                    async with transport.stream(request, adapter, lambda *x: terminals.append(x)):
                        pytest.fail("timeout must occur before headers")
                assert exc.value.status_code == 504
            await asyncio.wait_for(closed.wait(), 1)
            assert not active
    assert terminals == [(None, "timeout")]
