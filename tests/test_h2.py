import asyncio
from typing import AsyncGenerator, Type
from unittest.mock import Mock

import h2
import pytest

from hypercorn.config import Config
from hypercorn.h2 import H2Server
from hypercorn.typing import ASGIFramework
from .helpers import ErrorFramework, HTTPFramework, MockTransport

BASIC_H2_HEADERS = [
    (':authority', 'hypercorn'), (':path', '/'), (':scheme', 'http'), (':method', 'GET'),
]
BASIC_H2_PUSH_HEADERS = [
    (':authority', 'hypercorn'), (':path', '/push'), (':scheme', 'http'), (':method', 'GET'),
]
FLOW_WINDOW_SIZE = 1


class MockH2Connection:

    def __init__(
            self,
            event_loop: asyncio.AbstractEventLoop,
            *,
            config: Config=Config(),
            framework: Type[ASGIFramework]=HTTPFramework,
    ) -> None:
        self.transport = MockTransport()
        self.server = H2Server(framework, event_loop, config, self.transport)  # type: ignore
        self.connection = h2.connection.H2Connection()

    def send_request(self, headers: list, settings: dict) -> int:
        self.connection.initiate_connection()
        self.connection.update_settings(settings)
        self.server.data_received(self.connection.data_to_send())
        stream_id = self.connection.get_next_available_stream_id()
        self.connection.send_headers(stream_id, headers, end_stream=True)
        self.server.data_received(self.connection.data_to_send())
        return stream_id

    async def get_events(self) -> AsyncGenerator[h2.events.Event, None]:
        while True:
            await self.transport.updated.wait()
            events = self.connection.receive_data(self.transport.data)
            self.transport.clear()
            for event in events:
                if isinstance(event, h2.events.ConnectionTerminated):
                    self.transport.close()
                elif isinstance(event, h2.events.DataReceived):
                    self.connection.acknowledge_received_data(
                        event.flow_controlled_length, event.stream_id,
                    )
                    self.server.data_received(self.connection.data_to_send())
                yield event
            if self.transport.closed.is_set():
                break


@pytest.mark.asyncio
async def test_h2server(event_loop: asyncio.AbstractEventLoop) -> None:
    connection = MockH2Connection(event_loop)
    connection.send_request(BASIC_H2_HEADERS, {})
    response_data = b''
    async for event in connection.get_events():
        if isinstance(event, h2.events.ResponseReceived):
            assert (b':status', b'200') in event.headers
            assert (b'server', b'hypercorn-h2') in event.headers
            assert b'date' in (header[0] for header in event.headers)
        elif isinstance(event, h2.events.DataReceived):
            response_data += event.data
        elif isinstance(event, h2.events.StreamEnded):
            break


@pytest.mark.asyncio
async def test_h2_protocol_error(event_loop: asyncio.AbstractEventLoop) -> None:
    connection = MockH2Connection(event_loop)
    connection.server.data_received(b'broken nonsense\r\n\r\n')
    assert connection.transport.closed.is_set()  # H2 just closes on error


@pytest.mark.asyncio
async def test_close_on_framework_error(event_loop: asyncio.AbstractEventLoop) -> None:
    connection = MockH2Connection(event_loop, framework=ErrorFramework)
    connection.send_request(BASIC_H2_HEADERS, {})
    stream_closed = False
    async for event in connection.get_events():
        if isinstance(event, h2.events.StreamEnded):
            stream_closed = True
            break
    connection.server.close()
    assert stream_closed


@pytest.mark.asyncio
async def test_h2_flow_control(event_loop: asyncio.AbstractEventLoop) -> None:
    connection = MockH2Connection(event_loop)
    connection.send_request(
        BASIC_H2_HEADERS, {h2.settings.SettingCodes.INITIAL_WINDOW_SIZE: FLOW_WINDOW_SIZE},
    )
    response_data = b''
    async for event in connection.get_events():
        if isinstance(event, h2.events.DataReceived):
            assert len(event.data) <= FLOW_WINDOW_SIZE
            response_data += event.data
        elif isinstance(event, h2.events.StreamEnded):
            break


@pytest.mark.asyncio
async def test_h2_push(event_loop: asyncio.AbstractEventLoop) -> None:
    connection = MockH2Connection(event_loop)
    connection.send_request(BASIC_H2_PUSH_HEADERS, {})
    push_received = False
    streams_received = 0
    async for event in connection.get_events():
        if isinstance(event, h2.events.PushedStreamReceived):
            assert (b':path', b'/') in event.headers
            assert (b':method', b'GET') in event.headers
            assert (b':scheme', b'http') in event.headers
            assert (b':authority', b'hypercorn') in event.headers
            push_received = True
        elif isinstance(event, h2.events.StreamEnded):
            streams_received += 1
            if streams_received == 2:
                break
    assert push_received


@pytest.mark.asyncio
async def test_initial_keep_alive_timeout(event_loop: asyncio.AbstractEventLoop) -> None:
    config = Config()
    config.keep_alive_timeout = 0.01
    server = H2Server(HTTPFramework, event_loop, config, Mock())
    await asyncio.sleep(2 * config.keep_alive_timeout)
    server.transport.close.assert_called()  # type: ignore


@pytest.mark.asyncio
async def test_post_response_keep_alive_timeout(event_loop: asyncio.AbstractEventLoop) -> None:
    config = Config()
    config.keep_alive_timeout = 0.01
    connection = MockH2Connection(event_loop, config=config)
    connection.send_request(BASIC_H2_HEADERS, {})
    connection.server.pause_writing()
    await asyncio.sleep(2 * config.keep_alive_timeout)
    assert not connection.transport.closed.is_set()
    connection.server.resume_writing()
    await asyncio.sleep(2 * config.keep_alive_timeout)
    assert connection.transport.closed.is_set()
