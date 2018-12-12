import asyncio
import aiohttp

from . import register_command
from .pretty import print_info, print_error
from ..request import Request
from ..session import AsyncSession
from ..compat import current_loop


class WSProxy:
    __slots__ = (
        'api_session', 'path',
        'down_task',
        'reader', 'writer',
    )
    BUFFER_SIZE = 8192

    def __init__(self, api_session: AsyncSession,
                 session_id: str,
                 protocol: str,
                 reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter):
        self.api_session = api_session
        self.path = f"/stream/kernel/{session_id}/{protocol}proxy"
        self.reader = reader
        self.writer = writer
        self.down_task = None

    async def run(self):
        api_rqst = Request(
            self.api_session, "GET", self.path, b'',
            content_type="application/json")
        async with api_rqst.connect_websocket() as ws:

            async def downstream():
                try:
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.ERROR:
                            await self.write_error(msg)
                            break
                        elif msg.type == aiohttp.WSMsgType.CLOSE:
                            if msg.data != aiohttp.WSCloseCode.OK:
                                await self.write_error(msg)
                            break
                        elif msg.type == aiohttp.WSMsgType.BINARY:
                            self.writer.write(msg.data)
                            await self.writer.drain()
                except asyncio.CancelledError:
                    pass
                finally:
                    self.writer.close()
                    if hasattr(self.writer, 'wait_closed'):  # Python 3.7+
                        await self.writer.wait_closed()

            self.down_task = asyncio.ensure_future(downstream())
            try:
                while True:
                    chunk = await self.reader.read(self.BUFFER_SIZE)
                    if not chunk:
                        break
                    await ws.send_bytes(chunk)
            except asyncio.CancelledError:
                pass
            finally:
                if not self.down_task.done():
                    await self.down_task
                    self.down_task = None

    async def write_error(self, msg):
        rsp = 'HTTP/1.1 503 Service Unavailable\r\n' \
            'Connection: Closed\r\n\r\n' \
            'WebSocket reply: {}'.format(msg.data.decode('utf8'))
        self.writer.write(rsp.encode())
        await self.writer.drain()


class ProxyRunner:
    __slots__ = (
        'session_id', 'app_name',
        'protocol', 'host', 'port',
        'api_session', 'local_server', 'loop',
    )

    def __init__(self, session_id, name, protocol, host, port, *, loop=None):
        self.session_id = session_id
        self.app_name = name
        self.protocol = protocol
        self.host = host
        self.port = port
        self.api_session = None
        self.local_server = None
        self.loop = loop if loop else current_loop()

    async def handle_connection(self, reader, writer):
        p = WSProxy(self.api_session, self.session_id,
                    self.protocol, reader, writer)
        try:
            await p.run()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            print_error(e)

    async def ready(self):
        self.api_session = AsyncSession()

        self.local_server = await asyncio.start_server(
            self.handle_connection, self.host, self.port,
            loop=self.loop)

        print_info(
            "A local proxy to the application \"{0}\" ".format(self.app_name) +
            "provided by the session \"{0}\" ".format(self.session_id) +
            "is available at: {0}://{1}:{2}"
            .format(self.protocol, self.host, self.port)
        )

    async def close(self):
        self.local_server.close()
        await self.local_server.wait_closed()
        await self.api_session.close()
        # Let asyncio-internal socket cleanups finish.
        await asyncio.sleep(0.1)


@register_command
def app(args):
    """
    Run a local proxy to a service provided by Backend.AI
    compute sessions.

    The type of proxy depends on the app definition: plain TCP or HTTP.
    """

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    # TODO: generalize protocol using service ports metadata
    runner = ProxyRunner(args.session_id, args.app,
                         'http', args.bind, args.port,
                         loop=loop)
    loop.run_until_complete(runner.ready())
    try:
        loop.run_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        print_info("Shutting down....")
        try:
            loop.run_until_complete(runner.close())
        finally:
            print_info("Done")
            loop.close()


app.add_argument('session_id', type=str, metavar='SESSID',
                 help='The compute session ID.')
app.add_argument('app', type=str,
                 help='The name of service provided by the given session.')
app.add_argument('--bind', type=str, default='127.0.0.1',
                   help='The IP/host address to bind this proxy.')
app.add_argument('-p', '--port', type=int, default=8080,
                   help='The port number to listen user connections.')
