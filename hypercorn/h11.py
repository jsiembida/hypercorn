import asyncio
from itertools import chain
from time import time
from typing import Optional, Type, Union
from urllib.parse import unquote, urlparse

import h11

from .base import ASGIState, HTTPServer, suppress_body
from .config import Config
from .logging import AccessLogAtoms
from .typing import ASGIFramework


class WrongProtocolError(Exception):

    def __init__(self, request: h11.Request) -> None:
        self.request = request


class WebsocketProtocolRequired(WrongProtocolError):
    pass


class H2CProtocolRequired(WrongProtocolError):
    pass


class H11Server(HTTPServer):

    def __init__(
            self,
            app: Type[ASGIFramework],
            loop: asyncio.AbstractEventLoop,
            config: Config,
            transport: asyncio.BaseTransport,
    ) -> None:
        super().__init__(loop, config, transport, 'h11')
        self.app = app
        self.connection = h11.Connection(
            h11.SERVER, max_incomplete_event_size=self.config.h11_max_incomplete_size,
        )

        self.app_queue: asyncio.Queue = asyncio.Queue(loop=loop)
        self.response: Optional[dict] = None
        self.scope: Optional[dict] = None
        self.state = ASGIState.REQUEST

        self.last_activity = time()
        self.handle_keep_alive_timeout()

    def data_received(self, data: bytes) -> None:
        super().data_received(data)
        self.last_activity = time()
        self.connection.receive_data(data)
        self.handle_events()

    def eof_received(self) -> bool:
        self.connection.receive_data(b'')
        self.last_activity = time()
        return True

    def close(self) -> None:
        self.app_queue.put_nowait({'type': 'http.disconnect'})
        self.keep_alive_timeout_handle.cancel()
        super().close()

    def handle_events(self) -> None:
        while True:
            if self.connection.they_are_waiting_for_100_continue:
                self.send(
                    h11.InformationalResponse(status_code=100, headers=self.response_headers()),
                )
            try:
                event = self.connection.next_event()
            except h11.RemoteProtocolError:
                self.handle_error()
                self.close()
                break
            else:
                if isinstance(event, h11.Request):
                    self.maybe_upgrade_request(event)  # Raises on upgrade
                    self.handle_request(event)
                elif isinstance(event, h11.EndOfMessage):
                    self.app_queue.put_nowait({
                        'type': 'http.request',
                        'body': b'',
                        'more_body': False,
                    })
                elif isinstance(event, h11.Data):
                    self.app_queue.put_nowait({
                        'type': 'http.request',
                        'body': event.data,
                        'more_body': True,
                    })
                elif (
                        isinstance(event, h11.ConnectionClosed)
                        or event is h11.NEED_DATA
                        or event is h11.PAUSED
                ):
                    break
        if self.connection.our_state is h11.MUST_CLOSE:
            self.close()

    def handle_error(self) -> None:
        self.send(
            h11.Response(
                status_code=400, headers=chain(
                    [(b'content-length', b'0'), (b'connection', b'close')],
                    self.response_headers(),
                ),
            ),
        )
        self.send(h11.EndOfMessage())

    def maybe_upgrade_request(self, event: h11.Request) -> None:
        upgrade_value = ''
        connection_value = ''
        has_body = False
        for name, value in event.headers:
            sanitised_name = name.decode().lower()
            if sanitised_name == 'upgrade':
                upgrade_value = value.decode().strip()
            elif sanitised_name == 'connection':
                connection_value = value.decode().strip()
            elif sanitised_name == 'content-length':
                has_body = True
            elif sanitised_name == 'transfer-encoding':
                has_body = True

        connection_tokens = connection_value.lower().split(',')
        if (
                any(token.strip() == 'upgrade' for token in connection_tokens) and
                upgrade_value.lower() == 'websocket' and
                event.method.decode().upper() == 'GET'
        ):
            self.keep_alive_timeout_handle.cancel()
            raise WebsocketProtocolRequired(event)
        # h2c Upgrade requests with a body are a pain as the body must
        # be fully recieved in HTTP/1.1 before the upgrade response
        # and HTTP/2 takes over, so Hypercorn ignores the upgrade and
        # responds in HTTP/1.1. Use a preflight OPTIONS request to
        # initiate the upgrade if really required (or just use h2).
        elif upgrade_value.lower() == 'h2c' and not has_body:
            self.keep_alive_timeout_handle.cancel()
            self.send(
                h11.InformationalResponse(
                    status_code=101, headers=[(b'upgrade', b'h2c')] + self.response_headers(),
                ),
            )
            raise H2CProtocolRequired(event)

    def handle_request(self, event: h11.Request) -> None:
        self.keep_alive_timeout_handle.cancel()
        scheme = 'https' if self.ssl_info is not None else 'http'
        parsed_path = urlparse(event.target)
        self.scope = {
            'type': 'http',
            'http_version': event.http_version.decode(),
            'method': event.method.decode().upper(),
            'scheme': scheme,
            'path': unquote(parsed_path.path.decode()),
            'query_string': parsed_path.query,
            'root_path': '',
            'headers': event.headers,
            'client': self.transport.get_extra_info('sockname'),
            'server': self.transport.get_extra_info('peername'),
        }
        self.task = self.loop.create_task(self.handle_asgi_app())
        self.task.add_done_callback(self.after_request)

    async def handle_asgi_app(self) -> None:
        start_time = time()
        try:
            asgi_instance = self.app(self.scope)
            await asgi_instance(self.asgi_receive, self.asgi_send)
        except Exception as error:
            if self.config.error_logger is not None:
                self.config.error_logger.exception('Error in ASGI Framework')
            self.close()
        if self.response is not None and self.config.access_logger is not None:
            self.config.access_logger.info(
                self.config.access_log_format,
                AccessLogAtoms(self.scope, self.response, time() - start_time),
            )

    def after_request(self, future: asyncio.Future) -> None:
        if self.connection.our_state is h11.DONE:
            self.recycle()
        self.handle_events()

    def recycle(self) -> None:
        """Recycle the state in order to accept a new request.

        This is vital if this connection can be re-used.
        """
        self.connection.start_next_cycle()
        self.app_queue = asyncio.Queue(loop=self.loop)
        self.response = None
        self.scope = None
        self.state = ASGIState.REQUEST
        self.handle_keep_alive_timeout()

    def send(
            self,
            event: Union[h11.Data, h11.EndOfMessage, h11.InformationalResponse, h11.Response],
    ) -> None:
        self.last_activity = time()
        self.write(self.connection.send(event))  # type: ignore

    def handle_keep_alive_timeout(self) -> None:
        if time() - self.last_activity > self.config.keep_alive_timeout:
            self.close()
        else:
            self.keep_alive_timeout_handle = self.loop.call_later(
                self.config.keep_alive_timeout, self.handle_keep_alive_timeout,
            )

    async def asgi_receive(self) -> dict:
        """Called by the ASGI instance to receive a message."""
        return await self.app_queue.get()

    async def asgi_send(self, message: dict) -> None:
        """Called by the ASGI instance to send a message."""
        if message['type'] == 'http.response.start' and self.state == ASGIState.REQUEST:
            self.response = message
        elif (
                message['type'] == 'http.response.body'
                and self.state in {ASGIState.REQUEST, ASGIState.RESPONSE}
        ):
            if self.state == ASGIState.REQUEST:
                headers = chain(
                    ((key.strip(), value.strip()) for key, value in self.response['headers']),
                    self.response_headers(),
                )
                self.send(h11.Response(status_code=self.response['status'], headers=headers))
                self.state = ASGIState.RESPONSE
            if not suppress_body(self.scope['method'], self.response['status']):
                self.send(h11.Data(data=message.get('body', b'')))
                await self.drain()
            if not message.get('more_body', False):
                if self.state != ASGIState.CLOSED:
                    self.send(h11.EndOfMessage())
                    self.app_queue.put_nowait({'type': 'http.disconnect'})
                self.state = ASGIState.CLOSED
        else:
            raise Exception(
                f"Unexpected message type, {message['type']} given the state {self.state}",
            )
