"""Verify ICE configuration using real aiortc objects, without cloud access."""
from contextlib import asynccontextmanager
import json
import unittest
from unittest.mock import patch

from aiortc import RTCPeerConnection

from SAARubetekAPI.webrtc import CAMERA_WS_URL, WS_URL, WebRTCViewer


class SignalingSocket:
    def __init__(self):
        self.sent = []

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def __aiter__(self):
        # Finish normally after auth/open, without exchanging network media.
        for message in ():
            yield message


class WebRTCICEConfigurationTests(unittest.IsolatedAsyncioTestCase):
    async def open_viewer(self, **kwargs):
        socket = SignalingSocket()

        @asynccontextmanager
        async def connect(*args, **kwargs):
            yield socket

        viewer = WebRTCViewer(api=None, relay_id=123,
                              webrtc_token="test-view-token", **kwargs)
        with patch("websockets.connect", side_effect=connect) as signaling:
            with patch("aiortc.RTCPeerConnection", wraps=RTCPeerConnection) as peer:
                await viewer.open()
        return peer.call_args.args[0].iceServers, socket.sent, signaling.call_args.args[0]

    async def test_default_intercom_keeps_google_stun_and_channel(self):
        servers, sent, url = await self.open_viewer()
        self.assertEqual([server.urls for server in servers],
                         ["stun:stun.l.google.com:19302"])
        self.assertIsNone(servers[0].username)
        self.assertEqual(url, WS_URL)
        opened = next(msg for msg in sent if msg["method"] == "webrtc_open")
        self.assertEqual(opened["params"]["channelId"], 0)

    async def test_camera_receives_turn_credentials_and_url_list(self):
        config = [{"urls": ["turn:turn.example.com:3478",
                            "turn:turn.example.com:3478?transport=tcp"],
                   "username": "test-user", "credential": "test-password"}]
        servers, sent, url = await self.open_viewer(type_device="cameras", ice_servers=config)
        self.assertEqual(len(servers), 1)
        self.assertEqual(servers[0].urls, config[0]["urls"])
        self.assertEqual(servers[0].username, "test-user")
        self.assertEqual(servers[0].credential, "test-password")
        self.assertEqual(url, CAMERA_WS_URL)
        opened = next(msg for msg in sent if msg["method"] == "webrtc_open")
        self.assertEqual(opened["params"]["channelId"], 2)
        # TURN credentials configure ICE, never Rubetek JSON-RPC signaling.
        self.assertNotIn("test-password", json.dumps(sent))
        self.assertNotIn("test-user", json.dumps(sent))

    async def test_stun_and_turn_can_be_combined_for_intercom(self):
        servers, _, _ = await self.open_viewer(ice_servers=[
            {"urls": "stun:stun.example.com:3478"},
            {"urls": "turns:turn.example.com:5349", "username": "test-user",
             "credential": "test-password"},
        ])
        self.assertEqual([server.urls for server in servers],
                         ["stun:stun.example.com:3478", "turns:turn.example.com:5349"])
        self.assertEqual(servers[1].credential, "test-password")

    async def test_empty_list_disables_stun_and_turn(self):
        servers, _, _ = await self.open_viewer(ice_servers=[])
        self.assertEqual(servers, [])


if __name__ == "__main__":
    unittest.main()
