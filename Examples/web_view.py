#!/usr/bin/env python3
"""
Локальный вэб-просмотр WebRTC-потока с домофонов Rubetek в браузере.

Запуск:
    python web_view.py

Затем откройте в браузере http://127.0.0.1:8080/

Схема:
  домофон --(WebRTC)--> WebRTCViewer --(passthrough-треки)--> браузер (страница /)

Страница в браузере сама инициирует WebRTC-соединение (offer), сервер
отвечает answer и «переклеивает» треки от домофона в браузерное соединение.
На странице есть кнопка отключения звука (mute) и статус соединения.

Зависимости:
    pip install aiohttp aiortc websockets
"""
import asyncio
import json
import sys

from aiohttp import web, WSMsgType

from aiortc import (AudioStreamTrack, MediaStreamTrack, RTCPeerConnection,
                    RTCConfiguration, RTCIceCandidate, RTCIceServer,
                    RTCSessionDescription)
from aiortc.sdp import candidate_from_sdp

from SAARubetekAPI import SAARubetekAPI, WebRTCViewer
from SAARubetekAPI.exceptions import AuthorizationRequiredRubetekAPIError

HOST = "127.0.0.1"
PORT = 8080

# Параметр подключения к домофону
RELAY_ID = None  # Если None — берём первый домофон из списка


class PassThroughVideoTrack(MediaStreamTrack):
    """Передаёт видео-кадры из исходного трека домофона без изменений."""
    kind = "video"

    def __init__(self, source: MediaStreamTrack):
        super().__init__()
        self._source = source

    async def recv(self):
        return await self._source.recv()


class PassThroughAudioTrack(AudioStreamTrack):
    """Передаёт аудио-кадры из исходного трека домофона без изменений."""
    kind = "audio"

    def __init__(self, source: MediaStreamTrack):
        super().__init__()
        self._source = source

    async def recv(self):
        return await self._source.recv()


INDEX_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Rubetek WebRTC</title>
<style>
  body { font-family: sans-serif; background:#111; color:#eee; text-align:center; margin:0; }
  h1 { padding:16px; font-weight:400; }
  video { width: min(90vw, 900px); background:#000; border-radius:8px; max-height:70vh; }
  .controls { margin:16px; }
  button { padding:10px 20px; font-size:16px; border:none; border-radius:6px;
           cursor:pointer; background:#2b6cb0; color:#fff; }
  button.muted { background:#c53030; }
  #status { margin-top:12px; opacity:.8; }
</style>
</head>
<body>
  <h1>Rubetek &mdash; просмотр домофона</h1>
  <video id="video" autoplay muted playsinline></video>
  <div class="controls">
    <button id="toggleMute">Звук: ВКЛ</button>
  </div>
  <div id="status">Подключение...</div>

<script>
  const video = document.getElementById('video');
  const statusEl = document.getElementById('status');
  const muteBtn = document.getElementById('toggleMute');

  // Показываем видео сразу без звука, звук включаем кнопкой (одноразовый мут),
  // чтобы браузер не блокировал автовоспроизведение.
  let muted = true;
  muteBtn.classList.add('muted');

  muteBtn.addEventListener('click', () => {
    muted = !muted;
    video.muted = muted;
    muteBtn.textContent = muted ? 'Звук: ВЫКЛ' : 'Звук: ВКЛ';
    muteBtn.classList.toggle('muted', muted);
  });

  const pc = new RTCPeerConnection({
    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
  });

  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.addTransceiver('audio', { direction: 'recvonly' });

  pc.ontrack = (ev) => {
    statusEl.textContent = 'Получен трек: ' + ev.track.kind;
    if (ev.track.kind === 'audio') {
      // Звук включаем только по клику, поэтому держим muted.
      video.muted = muted;
    } else if (ev.streams?.[0]) {
      video.srcObject = ev.streams[0];
    }
  };

  pc.onconnectionstatechange = () => {
    statusEl.textContent = 'Состояние: ' + pc.connectionState;
  };

  const ws = new WebSocket(`ws://${location.host}/ws`);

  ws.onopen = async () => {
    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);
    ws.send(JSON.stringify({ type: 'offer', sdp: pc.localDescription.sdp }));
  };

  ws.onmessage = async (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'error') {
      statusEl.textContent = 'Ошибка: ' + msg.message;
      return;
    }
    if (msg.type === 'answer') {
      await pc.setRemoteDescription({ type: 'answer', sdp: msg.sdp });
      statusEl.textContent = 'Соединение установлено';
    } else if (msg.type === 'ice') {
      await pc.addIceCandidate({
        candidate: msg.candidate,
        sdpMid: msg.sdpMid,
        sdpMLineIndex: msg.sdpMLineIndex
      });
    }
  };

  pc.onicecandidate = (ev) => {
    if (ev.candidate) {
      ws.send(JSON.stringify({
        type: 'ice',
        candidate: ev.candidate.candidate,
        sdpMid: ev.candidate.sdpMid,
        sdpMLineIndex: ev.candidate.sdpMLineIndex
      }));
    }
  };
</script>
</body>
</html>
"""


class StreamHub:
    """Держит WebRTCViewer (домофон) и переклеивает его треки в браузерные pc."""

    def __init__(self, api, relay_id: int):
        self.api = api
        self.relay_id = relay_id
        self.viewer = None
        self.video_track = None
        self.audio_track = None

    async def start(self):
        viewer = WebRTCViewer(api=self.api, relay_id=self.relay_id)
        tracks_ready = asyncio.Event()

        def on_track(track):
            if track.kind == "video" and self.video_track is None:
                self.video_track = PassThroughVideoTrack(track)
            elif track.kind == "audio" and self.audio_track is None:
                self.audio_track = PassThroughAudioTrack(track)
            if self.video_track is not None:
                tracks_ready.set()

        viewer.on_track = on_track
        self.viewer = viewer
        self._session = asyncio.create_task(viewer.open())
        # Ждём первый видеотрек (и, сколько успеем, аудио)
        await asyncio.wait_for(tracks_ready.wait(), timeout=30)
        return self

    async def stop(self):
        if self.viewer is not None:
            self._session.cancel()
            try:
                await self._session
            except Exception:
                pass
            try:
                await self.viewer.close()
            except Exception:
                pass
        self.viewer = None


async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type="text/html")


async def handle_web_socket(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)

    hub = request.app["hub"]
    pc = None
    try:
        # Держим браузерную pc: она живёт на время одного WS-соединения.
        pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=[
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
        ]))

        added_track = False
        if hub.video_track is not None:
            pc.addTrack(hub.video_track)
            added_track = True
        if hub.audio_track is not None:
            pc.addTrack(hub.audio_track)
            added_track = True
        if not added_track:
            await ws.send_json({"type": "error", "message": "Нет треков от домофона."})
            return ws

        @pc.on("icecandidate")
        async def on_ice(candidate):
            if candidate is None:
                return
            try:
                await ws.send_json({
                    "type": "ice",
                    "candidate": candidate.candidate,
                    "sdpMid": candidate.sdpMid,
                    "sdpMLineIndex": candidate.sdpMLineIndex,
                })
            except Exception:
                pass

        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)

            if data.get("type") == "offer":
                await pc.setRemoteDescription(RTCSessionDescription(
                    sdp=data["sdp"], type="offer"))
                answer = await pc.createAnswer()
                await pc.setLocalDescription(answer)
                await ws.send_json({"type": "answer", "sdp": pc.localDescription.sdp})

            elif data.get("type") == "ice":
                cand = data.get("candidate")
                if not cand:
                    continue
                candidate = candidate_from_sdp(str(cand).split(":", 1)[1] if str(cand).startswith("candidate:") else str(cand))
                candidate.sdpMid = data.get("sdpMid")
                candidate.sdpMLineIndex = data.get("sdpMLineIndex")
                await pc.addIceCandidate(candidate)

    except Exception as e:
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass
    finally:
        if pc is not None:
            await pc.close()
        if not ws.closed:
            await ws.close()
    return ws


async def pick_relay_id(api) -> int:
    global RELAY_ID
    if RELAY_ID is not None:
        return RELAY_ID
    intercoms = await api.get_intercoms()
    if not intercoms:
        raise RuntimeError("Нет доступных домофонов (intercoms).")
    return int(intercoms[0].id)


async def main():
    api = SAARubetekAPI()
    try:
        try:
            await api.refresh_tokens()
        except AuthorizationRequiredRubetekAPIError as error:
            print(error)
            phone = input("Введите номер телефона (c +7): ")
            await api.authentication(phone=phone)
            code = input("Введите последние 4 цифры номера звонившего телефона: ")
            await api.authorization(phone=phone, code=code)

        # Для получения домофонов нужен установленный дом
        homes = await api.get_homes()
        if not homes:
            raise RuntimeError("В аккаунте нет ни одного дома (houses).")
        api.house_id = homes[0].id
        print(f"[web] Дом: {api.house_id}")

        relay_id = await pick_relay_id(api)
        print(f"[web] Подключаемся к домофону id={relay_id} ...")
        hub = StreamHub(api, relay_id)
        await hub.start()
        print(f"[web] Поток получен. Откройте http://{HOST}:{PORT}/ в браузере.")
        print("[web] Для остановки нажмите Ctrl+C")

        app = web.Application()
        app["hub"] = hub
        app.router.add_get("/", handle_index)
        app.router.add_get("/ws", handle_web_socket)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, HOST, PORT)
        await site.start()

        try:
            await asyncio.Event().wait()  # ждём Ctrl+C
        except asyncio.CancelledError:
            pass
        finally:
            await runner.cleanup()
    finally:
        # экземпляр hub мог не создаться при ошибке выше
        hub = locals().get("hub")
        if hub is not None:
            await hub.stop()
        await api.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)