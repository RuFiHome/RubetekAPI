"""Cancellation-safe lifetime and reconnect management for WebRTCViewer."""
import asyncio
from contextlib import suppress
import inspect
import logging
import math
import random
import time

logger = logging.getLogger(__name__)


class WebRTCViewer:
    """A single video viewer. open() holds one session; run() reconnects until close().

    close() is terminal: create another viewer to start again. An explicit video
    token is used once, then a fresh token is requested for every new attempt.
    ice_servers overrides ice_servers_provider, including an explicit empty list.
    The async provider is called before each attempt and must return ICE dictionaries.
    Native SAARubetekAPI clients share an automatic signed-APK TURN provider.
    Explicit ICE settings/providers or auto_turn=False override that default.
    """

    def __init__(self, api, relay_id, webrtc_token=None, ws_url=None, channel_id=None,
                 ping_interval=8, offer_timeout=20, type_device="relays",
                 ice_servers=None, ice_servers_provider=None, retry_initial=2,
                 retry_max=30, retry_jitter=0.2, disconnected_grace=5,
                 stable_reset=30, auto_turn=True):
        from .webrtc import Rpc, WS_URL, CAMERA_WS_URL, CHANNEL_ID, CAMERA_CHANNEL_ID
        for name, value in (("ping_interval", ping_interval), ("offer_timeout", offer_timeout),
                            ("retry_initial", retry_initial), ("retry_max", retry_max),
                            ("disconnected_grace", disconnected_grace), ("stable_reset", stable_reset)):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(name + " must be positive and finite")
        if retry_max < retry_initial or not 0 <= retry_jitter < 1:
            raise ValueError("Invalid retry limits")
        self.api, self.relay_id = api, relay_id
        self.webrtc_token = webrtc_token
        self.type_device = type_device
        camera = type_device != "relays"
        self.ws_url = ws_url or (CAMERA_WS_URL if camera else WS_URL)
        self.channel_id = channel_id if channel_id is not None else (CAMERA_CHANNEL_ID if camera else CHANNEL_ID)
        self.ping_interval, self.offer_timeout = ping_interval, offer_timeout
        self.ice_servers, self.ice_servers_provider = ice_servers, ice_servers_provider
        if auto_turn and ice_servers is None and ice_servers_provider is None:
            get_provider = getattr(api, 'get_turn_provider', None)
            if callable(get_provider):
                self.ice_servers_provider = get_provider()
        self.retry_initial, self.retry_max = retry_initial, retry_max
        self.retry_jitter, self.disconnected_grace = retry_jitter, disconnected_grace
        self.stable_reset = stable_reset
        self._rpc = Rpc()
        self._pc = self._ws = self._owner = self._attempt_task = None
        self._open_event, self._closed, self._finished = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self._token_used = False
        self._connected_since = None
        self._session_stable = False
        self._close_task = None
        self.opened = False
        self.on_track = None
        if hasattr(api, '_viewers'):
            api._viewers.add(self)

    async def _attempt(self):
        from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer
        import websockets
        self._open_event.clear()
        self.opened = False
        self._connected_since = None
        self._session_stable = False
        servers = self.ice_servers
        if servers is None:
            servers = (await self.ice_servers_provider() if self.ice_servers_provider else
                       [{"urls": "stun:stun.l.google.com:19302"}])
        # Resolve a potentially large first-time SDK download before obtaining
        # the short-lived signaling token, so it is fresh when authentication starts.
        token = self.webrtc_token if not self._token_used else None
        self._token_used = True
        if token is None:
            token = await self.api.get_webrtc_token(self.relay_id, self.type_device)
        self.webrtc_token = token
        pc = RTCPeerConnection(RTCConfiguration(iceServers=[RTCIceServer(**s) for s in servers]))
        self._pc = pc
        tasks = set()

        @pc.on("track")
        def on_track(track):
            if self.on_track and not self._closed.is_set():
                result = self.on_track(track)
                if inspect.isawaitable(result):
                    tasks.add(asyncio.create_task(result))

        try:
            async with websockets.connect(self.ws_url, max_size=1024 * 1024,
                                          ping_interval=None, ping_timeout=None,
                                          open_timeout=self.offer_timeout, close_timeout=2) as ws:
                self._ws = ws
                await ws.send(self._rpc.auth(token))
                await ws.send(self._rpc.open(self.channel_id))
                await ws.send(self._rpc.ping())
                receiver = asyncio.create_task(self._recv_loop(pc, ws))
                ping = asyncio.create_task(self._keepalive(ws))
                monitor = asyncio.create_task(self._monitor_pc(pc))
                ready = asyncio.create_task(self._open_event.wait())
                tasks.update((receiver, ping, monitor, ready))
                done, _ = await asyncio.wait((receiver, ping, monitor, ready),
                                             timeout=self.offer_timeout,
                                             return_when=asyncio.FIRST_COMPLETED)
                if not done:
                    raise TimeoutError("WebRTC connection timed out")
                for task in done - {ready}:
                    task.result()
                    raise ConnectionError("WebRTC session ended before connection")
                done, _ = await asyncio.wait((receiver, ping, monitor), return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
                raise ConnectionError("WebRTC session ended")
        finally:
            self.opened = False
            self._open_event.clear()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._ws is not None:
                self._ws = None
            try:
                # Cancelling aiortc's close midway can leave its internal closed
                # future unresolved. TURN deallocation may wait for STUN retries.
                cleanup = asyncio.create_task(pc.close())
                interrupted = False
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        interrupted = True
                cleanup.result()
                if interrupted:
                    raise asyncio.CancelledError()
            finally:
                if self._pc is pc:
                    self._pc = None

    async def _recv_loop(self, pc, ws):
        from aiortc import RTCSessionDescription
        from .webrtc import parse_ice
        import json
        pending = []
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, dict):
                continue
            method, params = msg.get("method"), msg.get("params") or {}
            if msg.get("error") or method == "error":
                # Server errors can contain tokens or SDP; never expose their body.
                raise ConnectionError("WebRTC signaling rejected the session")
            if method == "webrtc_close":
                raise ConnectionError("WebRTC signaling closed the session")
            if method == "webrtc_sdp" and params.get("type") == "offer":
                await pc.setRemoteDescription(RTCSessionDescription(sdp=params["sdp"], type="offer"))
                for candidate in pending:
                    await pc.addIceCandidate(candidate)
                pending.clear()
                await pc.setLocalDescription(await pc.createAnswer())
                await ws.send(self._rpc.sdp_answer(pc.localDescription.sdp))
            elif method == "webrtc_ice_candidate":
                candidate = parse_ice(params)
                if candidate is not None:
                    if pc.remoteDescription is None:
                        if len(pending) >= 256:
                            raise ConnectionError("Too many early ICE candidates")
                        pending.append(candidate)
                    else:
                        await pc.addIceCandidate(candidate)
        raise ConnectionError("WebRTC signaling disconnected")

    async def _keepalive(self, ws):
        while True:
            await asyncio.sleep(self.ping_interval)
            await ws.send(self._rpc.ping())

    async def _monitor_pc(self, pc):
        disconnected_at = None
        while True:
            state = pc.connectionState
            ice_state = getattr(pc, 'iceConnectionState', None)
            if ice_state in ('failed', 'disconnected'):
                state = ice_state
            if state == "connected":
                if self._connected_since is None:
                    self._connected_since = time.monotonic()
                if time.monotonic() - self._connected_since >= self.stable_reset:
                    self._session_stable = True
                self.opened = True
                self._open_event.set()
                disconnected_at = None
            elif state in ("failed", "closed"):
                raise ConnectionError("WebRTC transport failed")
            elif state == "disconnected":
                self.opened = False
                self._open_event.clear()
                self._connected_since = None
                disconnected_at = disconnected_at or time.monotonic()
                if time.monotonic() - disconnected_at >= self.disconnected_grace:
                    raise ConnectionError("WebRTC transport disconnected")
            await asyncio.sleep(min(0.2, self.disconnected_grace))

    async def _drive(self, reconnect):
        if self._closed.is_set():
            raise RuntimeError("Viewer is closed; create a new viewer")
        if self._owner is not None:
            raise RuntimeError("Viewer is already running")
        self._owner = asyncio.current_task()
        self._finished.clear()
        delay = self.retry_initial
        try:
            while not self._closed.is_set():
                self._attempt_task = asyncio.create_task(self._attempt())
                try:
                    await self._attempt_task
                except asyncio.CancelledError:
                    if not self._closed.is_set():
                        raise
                    return
                except Exception as error:
                    if self._closed.is_set():
                        return
                    if not reconnect:
                        raise
                    logger.warning("WebRTC attempt failed (%s); reconnecting", type(error).__name__)
                finally:
                    self._attempt_task = None
                if not reconnect:
                    return
                if self._session_stable:
                    delay = self.retry_initial
                pause = min(self.retry_max, delay * random.uniform(1-self.retry_jitter, 1+self.retry_jitter))
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._closed.wait(), pause)
                delay = min(self.retry_max, delay * 2)
        finally:
            self._owner = None
            self.opened = False
            self._open_event.clear()
            self._finished.set()

    async def open(self):
        """Hold one session until it ends; errors propagate without reconnecting."""
        await self._drive(False)

    async def run(self):
        """Reconnect with bounded exponential backoff until close() or cancellation."""
        await self._drive(True)

    async def wait_until_open(self, timeout=None):
        """Wait for an actual connected transport, or raise on closure/termination."""
        waits = [asyncio.create_task(e.wait()) for e in (self._open_event, self._closed, self._finished)]
        try:
            done, _ = await asyncio.wait(waits, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise asyncio.TimeoutError()
            if self._closed.is_set() or self._finished.is_set():
                raise ConnectionError("Viewer closed before connection")
        finally:
            for task in waits:
                task.cancel()
            await asyncio.gather(*waits, return_exceptions=True)

    async def close(self):
        """Stop permanently, interrupt token/config fetches or backoff and release resources."""
        self._closed.set()
        self.opened = False
        current = asyncio.current_task()
        if current is self._attempt_task or current is self._owner:
            # A callback running inside the session must not wait for itself.
            if self._close_task is None:
                self._close_task = asyncio.create_task(self._stop())
            return
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._stop())
        await asyncio.shield(self._close_task)

    async def _stop(self):
        owner, attempt = self._owner, self._attempt_task
        if attempt is not None:
            attempt.cancel()
            await asyncio.gather(attempt, return_exceptions=True)
        if owner is not None:
            await asyncio.gather(owner, return_exceptions=True)
