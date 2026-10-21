"""WebRTC video streaming for Rubetek intercoms (домофоны без RTSP).

The WebRTC protocol is reconstructed from the official Rubetek app
(``com.fsk.home``). Signaling is JSON-RPC 2.0 over a WebSocket:

    wss://webrtc-splitter.iot.rubetek.com/ws

Message flow:
    auth -> webrtc_open -> (server sends SDP offer + ICE candidates)
    -> webrtc_sdp (answer) -> media over the negotiated RTCPeerConnection;
    webrtc_ping keeps the session alive; webrtc_close ends it gracefully.

The ``WebRTCViewer`` class wraps this handshake and keeps the media session
open, exposing a small high-level API (``open``/``close``/``run``).

Required extra dependencies (installed on demand):
    pip install aiortc websockets
"""

import asyncio
import json
from typing import Any, Dict, Optional

WS_URL = "wss://webrtc-splitter.iot.rubetek.com/ws"
CHANNEL_ID = 0
PING_INTERVAL = 8      # seconds between webrtc_ping messages
OFFER_TIMEOUT = 20     # seconds to wait for the server's SDP offer


class Rpc:
    """Builds JSON-RPC 2.0 signaling messages (exact format used by the app)."""

    def __init__(self):
        self._id = 0

    def _msg(self, method: str, params: Optional[Dict[str, Any]] = None) -> str:
        self._id += 1
        m: Dict[str, Any] = {"jsonrpc": "2.0", "method": method, "id": self._id}
        if params:
            m["params"] = params
        return json.dumps(m)

    def auth(self, token: str) -> str:
        return self._msg("auth", {"token": token})

    def open(self, channel_id: int) -> str:
        return self._msg("webrtc_open", {"mode": 1, "channel_id": channel_id})

    def sdp_answer(self, sdp: str) -> str:
        return self._msg("webrtc_sdp", {"type": "answer", "sdp": sdp})

    def ice(self, candidate: str, sdp_mid: str, sdp_mline_index: int) -> str:
        return self._msg("webrtc_ice_candidate", {
            "candidate": candidate,
            "sdpMid": sdp_mid,
            "sdpMLineIndex": sdp_mline_index,
        })

    def ping(self) -> str:
        return self._msg("webrtc_ping")

    def close(self) -> str:
        return self._msg("webrtc_close")


def parse_ice(params: Dict[str, Any]):
    """Build an :class:`RTCIceCandidate` from the server's ICE candidate message."""
    cand_str = params.get("candidate")
    if not cand_str:
        return None
    from aiortc.sdp import candidate_from_sdp

    payload = cand_str.split(":", 1)[1] if cand_str.startswith("candidate:") else cand_str
    c = candidate_from_sdp(payload)
    c.sdpMid = params.get("sdpMid")
    c.sdpMLineIndex = params.get("sdpMLineIndex")
    return c


class WebRTCViewer:
    """Establishes and holds open a WebRTC media session with a Rubetek intercom.

    The viewer owns its own :class:`RTCPeerConnection` and a WebSocket signaling
    channel. Once :meth:`open` completes the handshake, the connection is kept
    alive with periodic ``webrtc_ping`` messages until :meth:`close` is called.

    Media tracks received from the intercom are exposed via the ``on_track``
    callback (which may be set before calling :meth:`open`).
    """

    def __init__(self, api, relay_id: int, webrtc_token: str = None,
                 ws_url: str = WS_URL, channel_id: int = CHANNEL_ID,
                 ping_interval: int = PING_INTERVAL,
                 offer_timeout: int = OFFER_TIMEOUT):
        """Initialize the viewer.

        Args:
            api: The :class:`SAARubetekAPI` instance (used to fetch config).
            relay_id: Numeric id of the intercom (relay) device.
            webrtc_token: Optional pre-fetched WebRTC token. If omitted, it is
                requested automatically from the API.
            ws_url: WebSocket signaling endpoint.
            channel_id: Intercom channel to open.
            ping_interval: Seconds between keep-alive ``webrtc_ping`` messages.
            offer_timeout: Seconds to wait for the server's SDP offer.
        """
        self.api = api
        self.relay_id = relay_id
        self.webrtc_token = webrtc_token
        self.ws_url = ws_url
        self.channel_id = channel_id
        self.ping_interval = ping_interval
        self.offer_timeout = offer_timeout

        self._pc = None
        self._ws = None
        self._rpc = Rpc()
        self._open_event = asyncio.Event()
        self.on_track = None
        self.opened = False

    async def open(self):
        """Establish the WebRTC session and hold it open.

        Fetches the WebRTC token (if needed), connects to the signaling WebSocket,
        performs the auth/open/answer handshake, and starts the keep-alive loop.
        The coroutine returns once the media session is active; the connection
        stays open until :meth:`close` is called.
        """
        if not self.webrtc_token:
            self.webrtc_token = await self.api.get_webrtc_token(self.relay_id)

        from aiortc import RTCPeerConnection
        from aiortc import RTCConfiguration, RTCIceServer

        pc = RTCPeerConnection(RTCConfiguration(iceServers=[
            RTCIceServer(urls="stun:stun.l.google.com:19302"),
        ]))
        self._pc = pc
        # Событие переиспользуется на весь срок жизни вьювера (создаётся в __init__),
        # здесь только сбрасываем его перед новым сеансом, чтобы wait_until_open()
        # всегда ожидал актуальное событие текущего open().
        self._open_event.clear()

        @pc.on("track")
        def _on_track(track):
            if self.on_track is not None:
                self.on_track(track)

        import websockets
        async with websockets.connect(self.ws_url, max_size=None,
                                      ping_interval=None, ping_timeout=None) as ws:
            self._ws = ws

            @pc.on("icecandidate")
            async def _on_icecandidate(candidate):
                if candidate is not None:
                    try:
                        await ws.send(self._rpc.ice(candidate.candidate,
                                                    candidate.sdpMid,
                                                    candidate.sdpMLineIndex))
                    except Exception:
                        pass

            await ws.send(self._rpc.auth(self.webrtc_token))
            await ws.send(self._rpc.open(self.channel_id))
            await ws.send(self._rpc.ping())

            keepalive = asyncio.create_task(self._keepalive())
            try:
                await self._recv_loop(pc, ws)

                # session ended cleanly (server sent webrtc_close)
                self._open_event.set()
            except asyncio.CancelledError:
                raise
            finally:
                keepalive.cancel()
                # Закрываем ЗАХВАЧЕННУЮ локальную pc/ws, а не self._pc/self._ws,
                # чтобы внешний close() (обнуляющий self-атрибуты) не сломал cleanup.
                if self._ws is ws:
                    self._ws = None
                if self._pc is pc:
                    self._pc = None
                try:
                    await ws.send(self._rpc.close())
                except Exception:
                    pass
                await pc.close()

    async def _recv_loop(self, pc, ws):
        """Process incoming signaling messages: offer, ICE candidates, close.

        Args:
            pc: The local :class:`RTCPeerConnection` to drive.
            ws: The open signaling WebSocket to read from.
        """
        from aiortc import RTCSessionDescription

        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            method = msg.get("method")
            params = msg.get("params") or {}

            if method == "webrtc_sdp" and params.get("type") == "offer":
                await pc.setRemoteDescription(
                    RTCSessionDescription(sdp=params["sdp"], type="offer"))
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send(self._rpc.sdp_answer(pc.localDescription.sdp))
                self.opened = True
                self._open_event.set()

            elif method == "webrtc_ice_candidate":
                try:
                    cand = parse_ice(params)
                    if cand:
                        await pc.addIceCandidate(cand)
                except Exception:
                    pass

            elif method in ("webrtc_close", "error"):
                return

    async def _keepalive(self):
        """Periodically send ``webrtc_ping`` to keep the session alive."""
        while True:
            await asyncio.sleep(self.ping_interval)
            try:
                await self._ws.send(self._rpc.ping())
            except Exception:
                return

    async def wait_until_open(self, timeout: Optional[float] = None):
        """Block until the media session is up, or fail on timeout."""
        await asyncio.wait_for(self._open_event.wait(), timeout=timeout)

    async def close(self):
        """Gracefully end the WebRTC session and release resources."""
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.send(self._rpc.close())
            except Exception:
                pass
        pc, self._pc = self._pc, None
        if pc is not None:
            await pc.close()
        self.opened = False

    async def run(self):
        """Reconnect loop: keep re-establishing the session until closed."""
        while True:
            try:
                await self.open()
            except Exception:
                pass
            await asyncio.sleep(1)