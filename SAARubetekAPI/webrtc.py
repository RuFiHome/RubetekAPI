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

import json
from typing import Any, Dict, Optional

WS_URL = "wss://webrtc-splitter.iot.rubetek.com/ws"
CHANNEL_ID = 0
CAMERA_WS_URL = "wss://webrtc-proxy.iot.rubetek.com/wsproxy"
CAMERA_CHANNEL_ID = 2
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
        return self._msg("webrtc_open", {"mode": 1, "channelId": channel_id})

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


from ._webrtc_viewer import WebRTCViewer
