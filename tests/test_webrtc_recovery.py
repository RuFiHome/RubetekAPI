import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from SAARubetekAPI.webrtc import WebRTCViewer


class Peer:
    instances = []

    def __init__(self, configuration):
        self.configuration = configuration
        self.connectionState = 'new'
        self.remoteDescription = None
        self.localDescription = SimpleNamespace(sdp='answer')
        self.candidates = []
        self.closed = False
        self.instances.append(self)

    def on(self, event):
        return lambda callback: callback

    async def setRemoteDescription(self, description):
        self.remoteDescription = description

    async def createAnswer(self):
        return self.localDescription

    async def setLocalDescription(self, description):
        self.connectionState = 'connected'

    async def addIceCandidate(self, candidate):
        assert self.remoteDescription is not None
        self.candidates.append(candidate)

    async def close(self):
        self.closed = True
        self.connectionState = 'closed'


class Socket:
    def __init__(self, offer=True):
        self.messages = []
        self.queue = asyncio.Queue()
        self.closed = False
        if offer:
            self.queue.put_nowait(json.dumps({'method': 'webrtc_sdp', 'params': {'type': 'offer', 'sdp': 'offer'}}))

    async def send(self, message):
        self.messages.append(json.loads(message))

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.queue.get()
        if item is None:
            raise StopAsyncIteration
        return item


class Recovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        Peer.instances = []
        self.sockets = []
        self.offer = True
        self.api = SimpleNamespace(get_webrtc_token=AsyncMock(side_effect=lambda *a: 'token-' + str(len(self.sockets))))

        @asynccontextmanager
        async def connect(*args, **kwargs):
            socket = Socket(self.offer)
            self.sockets.append(socket)
            try:
                yield socket
            finally:
                socket.closed = True
        self.peer_patch = patch('aiortc.RTCPeerConnection', Peer)
        self.ws_patch = patch('websockets.connect', connect)
        self.peer_patch.start()
        self.ws_patch.start()
        self.viewers = []

    async def asyncTearDown(self):
        for viewer in self.viewers:
            await viewer.close()
        self.ws_patch.stop()
        self.peer_patch.stop()
        self.assertTrue(all(p.closed for p in Peer.instances))
        self.assertTrue(all(s.closed for s in self.sockets))

    def viewer(self, **options):
        viewer = WebRTCViewer(self.api, 1, retry_initial=.01, retry_max=.04,
                              retry_jitter=0, offer_timeout=1, **options)
        self.viewers.append(viewer)
        return viewer

    async def until(self, condition):
        async def wait():
            while not condition():
                await asyncio.sleep(.005)
        await asyncio.wait_for(wait(), 2)

    async def test_disconnect_fetches_new_token_and_closes_old_peer(self):
        viewer = self.viewer(webrtc_token='initial-token')
        task = asyncio.create_task(viewer.run())
        await viewer.wait_until_open(1)
        self.sockets[0].queue.put_nowait(None)
        await self.until(lambda: len(self.sockets) == 2)
        tokens = [s.messages[0]['params']['token'] for s in self.sockets]
        self.assertEqual(tokens, ['initial-token', 'token-1'])
        self.assertEqual(self.api.get_webrtc_token.await_count, 1)
        self.assertTrue(Peer.instances[0].closed)
        await viewer.close()
        await asyncio.wait_for(task, .5)
        await asyncio.sleep(.05)
        self.assertEqual(len(self.sockets), 2)

    async def test_close_interrupts_pending_token_request(self):
        started = asyncio.Event()
        async def token(*args):
            started.set()
            await asyncio.Event().wait()
        self.api.get_webrtc_token.side_effect = token
        viewer = self.viewer()
        task = asyncio.create_task(viewer.run())
        await started.wait()
        await asyncio.wait_for(viewer.close(), .5)
        await asyncio.wait_for(task, .5)
        self.assertEqual(self.sockets, [])

    async def test_close_interrupts_pending_turn_provider(self):
        started = asyncio.Event()
        async def provider():
            started.set()
            await asyncio.Event().wait()
        viewer = self.viewer(ice_servers_provider=provider)
        runner = asyncio.create_task(viewer.run())
        await started.wait()
        self.api.get_webrtc_token.assert_not_awaited()
        await asyncio.wait_for(viewer.close(), .5)
        await runner
        self.assertEqual(Peer.instances, [])

    async def test_close_interrupts_backoff_and_redacts_errors(self):
        viewer = self.viewer()
        viewer.retry_initial = viewer.retry_max = 100
        viewer._attempt = AsyncMock(side_effect=RuntimeError('SECRET-CREDENTIAL'))
        with self.assertLogs('SAARubetekAPI._webrtc_viewer', level='WARNING') as logs:
            task = asyncio.create_task(viewer.run())
            await self.until(lambda: bool(logs.output))
            await asyncio.wait_for(viewer.close(), .5)
            await task
        self.assertNotIn('SECRET-CREDENTIAL', str(logs.output))
        self.assertEqual(viewer._attempt.await_count, 1)

    async def test_peer_failure_reconnects_and_refreshes_provider(self):
        provider = AsyncMock(return_value=[{'urls': 'stun:example.invalid'}])
        viewer = self.viewer(ice_servers_provider=provider)
        task = asyncio.create_task(viewer.run())
        await viewer.wait_until_open(1)
        Peer.instances[0].connectionState = 'failed'
        await self.until(lambda: len(self.sockets) >= 2)
        self.assertEqual(provider.await_count, 2)
        self.assertEqual(self.api.get_webrtc_token.await_count, 2)
        await viewer.close()
        await task

    async def test_offer_timeout_retries(self):
        self.offer = False
        viewer = self.viewer()
        viewer.offer_timeout = .03
        task = asyncio.create_task(viewer.run())
        await self.until(lambda: len(self.sockets) >= 2)
        await viewer.close()
        await task

    async def test_explicit_empty_ice_overrides_provider(self):
        provider = AsyncMock(side_effect=AssertionError('must not call'))
        viewer = self.viewer(ice_servers=[], ice_servers_provider=provider)
        task = asyncio.create_task(viewer.open())
        await viewer.wait_until_open(1)
        self.assertEqual(Peer.instances[0].configuration.iceServers, [])
        provider.assert_not_awaited()
        await viewer.close()
        await task

    async def test_external_cancellation_releases_resources_without_retry(self):
        viewer = self.viewer()
        task = asyncio.create_task(viewer.run())
        await viewer.wait_until_open(1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.sockets), 1)

    async def test_close_is_terminal_and_wakes_readiness_waiters(self):
        viewer = self.viewer()
        waiter = asyncio.create_task(viewer.wait_until_open())
        await viewer.close()
        with self.assertRaises(ConnectionError):
            await asyncio.wait_for(waiter, .5)
        with self.assertRaises(RuntimeError):
            await viewer.run()
        with self.assertRaises(RuntimeError):
            await viewer.open()

    async def test_cancelled_close_does_not_cancel_peer_cleanup(self):
        original = Peer.close
        closing = asyncio.Event()
        release = asyncio.Event()
        close_calls = 0
        async def slow_close(peer):
            nonlocal close_calls
            close_calls += 1
            closing.set()
            await release.wait()
            await original(peer)
        with patch.object(Peer, 'close', slow_close):
            viewer = self.viewer()
            runner = asyncio.create_task(viewer.run())
            await viewer.wait_until_open(1)
            closer = asyncio.create_task(viewer.close())
            await closing.wait()
            closer.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await closer
            self.assertFalse(Peer.instances[0].closed)
            release.set()
            await asyncio.wait_for(viewer.close(), .5)
            await runner
        self.assertEqual(close_calls, 1)

    async def test_socket_connect_failure_closes_allocated_peer(self):
        @asynccontextmanager
        async def fail(*args, **kwargs):
            raise ConnectionError('offline')
            yield
        viewer = self.viewer()
        with patch('websockets.connect', fail):
            with self.assertRaises(ConnectionError):
                await viewer.open()
        self.assertTrue(Peer.instances[0].closed)

    async def test_concurrent_run_is_rejected(self):
        viewer = self.viewer()
        runner = asyncio.create_task(viewer.run())
        await viewer.wait_until_open(1)
        with self.assertRaises(RuntimeError):
            await viewer.run()
        await viewer.close()
        await runner

    async def test_early_ice_is_buffered_until_offer(self):
        viewer = self.viewer()
        socket = Socket(False)
        socket.queue.put_nowait(json.dumps({'method': 'webrtc_ice_candidate', 'params': {
            'candidate': 'candidate:1 1 udp 1 192.0.2.1 1234 typ host', 'sdpMid': '0', 'sdpMLineIndex': 0}}))
        socket.queue.put_nowait(json.dumps({'method': 'webrtc_sdp', 'params': {'type': 'offer', 'sdp': 'offer'}}))
        socket.queue.put_nowait(None)
        peer = Peer(SimpleNamespace())
        with self.assertRaises(ConnectionError):
            await viewer._recv_loop(peer, socket)
        self.assertEqual(len(peer.candidates), 1)
        await peer.close()

    async def test_signaling_error_does_not_expose_response_body(self):
        viewer = self.viewer()
        socket = Socket(False)
        socket.queue.put_nowait(json.dumps({'id': 1, 'error': {'message': 'SECRET-TOKEN'}}))
        peer = Peer(SimpleNamespace())
        with self.assertRaises(ConnectionError) as error:
            await viewer._recv_loop(peer, socket)
        self.assertNotIn('SECRET-TOKEN', str(error.exception))
        await peer.close()

    async def test_public_factories_forward_turn_and_retry_options_without_starting(self):
        from SAARubetekAPI import SAARubetekAPI
        client = object.__new__(SAARubetekAPI)
        provider = AsyncMock()
        for factory in (client.create_web_rtc_viewer, client.watch_video):
            viewer = await factory(1, ice_servers_provider=provider, retry_max=42)
            self.viewers.append(viewer)
            self.assertIs(viewer.ice_servers_provider, provider)
            self.assertEqual(viewer.retry_max, 42)
            self.assertIsNone(viewer._owner)
        provider.assert_not_awaited()

    async def test_api_automatically_shares_turn_provider_and_closes_it(self):
        from SAARubetekAPI import SAARubetekAPI
        client = object.__new__(SAARubetekAPI)
        client.session = None
        provider = SimpleNamespace(close=AsyncMock())
        with patch('SAARubetekAPI.turn.RuStoreTurnProvider', return_value=provider) as create:
            first = await client.create_web_rtc_viewer(1)
            second = await client.create_web_rtc_viewer(2)
            manual = await client.create_web_rtc_viewer(3, ice_servers=[])
            disabled = await client.create_web_rtc_viewer(4, auto_turn=False)
            self.assertIs(first.ice_servers_provider, provider)
            self.assertIs(second.ice_servers_provider, provider)
            self.assertIsNone(manual.ice_servers_provider)
            self.assertIsNone(disabled.ice_servers_provider)
            create.assert_called_once_with()
            await client.close()
            provider.close.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
