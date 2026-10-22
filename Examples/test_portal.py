#!/usr/bin/env python3
"""Симуляция работы HACS-плагина для Rubetek: бэкенд-сервер.

Поднимает локальный aiohttp-сервер, который:
  * авторизуется в Rubetek (сохраняет/переиспользует refresh-токен);
  * отдаёт список домов (houses), устройств (devices), камер (cameras),
    домофонов (intercoms) через JSON API;
  * для домофона запускает WebRTC-хуб (pass-through, как в web_view.py)
    и отдаёт видео в браузер через WebSocket/WebRTC без декодирования;
  * для камеры отдаёт snapshot/RTSP-инфо (WebRTC у камер Rubetek нет).

Интерфейс (index.html) реализует frontend-часть «как в HACS»: выбор дома,
список устройств/камер/домофонов, инфо-панель и проигрывание видео.

Запуск:
    .venv/bin/python test_hacs.py --host 127.0.0.1 --port 8090

Затем откройте http://127.0.0.1:8090/
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web, WSMsgType

from SAARubetekAPI import SAARubetekAPI, WebRTCViewer
from SAARubetekAPI.exceptions import AuthorizationRequiredRubetekAPIError

HOST = "127.0.0.1"
PORT = 8090
BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"


def device_to_dict(device: Any) -> Dict[str, Any]:
    """Сериализует Device в JSON-словарь для UI."""
    st = getattr(device, "state", None)
    return {
        "id": device.id,
        "natural_id": getattr(device, "natural_id", None),
        "type": getattr(device, "type", None),
        "name": device.name,
        "room": getattr(device, "room", None),
        "updated_at": _iso(getattr(device, "updated_at", None)),
        "last_activity": _iso(getattr(device, "last_activity", None)),
        "status": getattr(st, "status", None),
        "current_value": getattr(st, "current_value", None),
        "units": getattr(st, "units", None),
        "battery": getattr(st, "battery", None),
        "error_code": getattr(st, "error_code", None),
    }


def camera_to_dict(cam: Any) -> Dict[str, Any]:
    return {
        "id": cam.id,
        "natural_id": getattr(cam, "natural_id", None),
        "type": getattr(cam, "type", None),
        "name": cam.name,
        "status": getattr(cam, "status", None),
        "live_snapshot_url": getattr(cam, "live_snapshot_url", None),
        "rtsp_url": getattr(cam, "rtsp_url", None),
    }


def intercom_to_dict(ic: Any) -> Dict[str, Any]:
    return {
        "id": ic.id,
        "intercom_id": getattr(ic, "intercom_id", None),
        "name": ic.name,
        "real_name": getattr(ic, "real_name", None),
        "status": ic.status,
        "ble_allowed": getattr(ic, "ble_allowed", None),
        "is_ble": getattr(ic, "is_ble", None),
        "webrtc_supported": getattr(ic, "webrtc_supported", None),
        "live_snapshot_url": getattr(ic, "live_snapshot_url", None),
        "sip_account_ex_user": getattr(ic, "sip_account_ex_user", None),
        "sip_account_proxy": getattr(ic, "sip_account_proxy", None),
    }


def house_to_dict(h: Any) -> Dict[str, Any]:
    return {
        "id": h.id,
        "name": h.name,
        "address": getattr(h, "address", None),
    }


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        return str(value)


class HacsApp:
    """Состояние приложения: API + активные WebRTC-хабы."""

    def __init__(self, api: SAARubetekAPI):
        self.api = api
        self.house_id: Optional[str] = None
        self.cameras: Dict[str, Any] = {}
        self.intercoms: Dict[str, Any] = {}
        # key: "intercom:<id>" -> StreamHub (переклеивает треки в браузер)
        self.hubs: Dict[str, "StreamHub"] = {}


def _make_track_classes():
    """Лениво создаёт pass-through треки как сабклассы aiortc-треков.

    aiortc нужен только для видео/аудио, поэтому импортируем его на месте.
    Два класса возвращаются: (PassThroughVideoTrack, PassThroughAudioTrack).
    """
    from aiortc import AudioStreamTrack, MediaStreamTrack

    class PassThroughVideoTrack(MediaStreamTrack):
        """Передаёт видео-кадры из исходного трека домофона без изменений."""
        kind = "video"

        def __init__(self, source):
            super().__init__()
            self._source = source

        async def recv(self):
            return await self._source.recv()

    class PassThroughAudioTrack(AudioStreamTrack):
        """Передаёт аудио-кадры из исходного трека домофона без изменений."""
        kind = "audio"

        def __init__(self, source):
            super().__init__()
            self._source = source

        async def recv(self):
            return await self._source.recv()

    return PassThroughVideoTrack, PassThroughAudioTrack


class StreamHub:
    """Держит WebRTCViewer (домофон) и переклеивает его треки в браузерные pc.

    Работает как в web_view.py: видео/аудио проходят насквозь (pass-through),
    без декодирования в av-кадры, поэтому не зависят от aiortc.H264Decoder.
    """

    def __init__(self, api, relay_id: int, type_device: str = "relays"):
        self.api = api
        self.relay_id = relay_id
        self.viewer = None
        self.video_track = None
        self.audio_track = None
        self._session = None
        self._track_classes = None
        self.type_device = type_device

    async def start(self):
        VideoTrackCls, AudioTrackCls = _make_track_classes()
        self._track_classes = (VideoTrackCls, AudioTrackCls)
        viewer = WebRTCViewer(api=self.api, relay_id=self.relay_id, type_device=self.type_device)
        tracks_ready = asyncio.Event()

        def on_track(track):
            if track.kind == "video" and self.video_track is None:
                self.video_track = VideoTrackCls(track)
            elif track.kind == "audio" and self.audio_track is None:
                self.audio_track = AudioTrackCls(track)
            if self.video_track is not None:
                tracks_ready.set()

        viewer.on_track = on_track
        self.viewer = viewer
        self._session = asyncio.create_task(viewer.open())
        # ждём первый видеотрек (и, сколько успеем, аудио)
        await asyncio.wait_for(tracks_ready.wait(), timeout=30)
        return self

    async def stop(self):
        if self._session is not None:
            self._session.cancel()
            try:
                await asyncio.wait_for(self._session, timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self._session = None
        if self.viewer is not None:
            try:
                await asyncio.wait_for(self.viewer.close(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
            self.viewer = None
        # сбрасываем треки при остановке (мост больше не активен)
        self.video_track = None
        self.audio_track = None


# ------------------------------------------------------------------- helpers
async def require_auth(app: HacsApp) -> None:
    if app.api.access_token is None:
        raise AuthorizationRequiredRubetekAPIError("Not authorized")


def _choose_house(app: HacsApp, house_id: Optional[str]) -> str:
    """Устанавливает выбранный дом и в HacsApp, и в API-клиенте.

    ВАЖНО: методы API (get_devices/get_cameras/get_intercoms) читают
    SAARubetekAPI.house_id, поэтому его тоже нужно обновлять.
    """
    if house_id:
        app.house_id = house_id
    if app.house_id is None:
        raise ValueError("No house selected")
    app.api.house_id = app.house_id
    return app.house_id


# ------------------------------------------------------------------- handlers
async def handle_index(request: web.Request) -> web.Response:
    text = INDEX_HTML.read_text(encoding="utf-8") if INDEX_HTML.exists() else "<h1>index.html not found</h1>"
    return web.Response(text=text, content_type="text/html")


async def handle_houses(request: web.Request) -> web.Response:
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    homes = await app.api.get_homes()
    return web.json_response([house_to_dict(h) for h in homes])


async def handle_devices(request: web.Request) -> web.Response:
    """Устройства выбранного дома (без камер)."""
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    # house_id передаётся query-параметром из select на странице
    app.house_id = request.query.get("house_id", app.house_id)
    _choose_house(app, None)
    devices = await app.api.get_devices()
    return web.json_response([device_to_dict(d) for d in devices])


async def handle_cameras(request: web.Request) -> web.Response:
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    app.house_id = request.query.get("house_id", app.house_id)
    _choose_house(app, None)
    cams = await app.api.get_cameras()
    app.cameras = {c.id: c for c in cams}
    return web.json_response([camera_to_dict(c) for c in cams])


async def handle_intercoms(request: web.Request) -> web.Response:
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    app.house_id = request.query.get("house_id", app.house_id)
    _choose_house(app, None)
    icoms = await app.api.get_intercoms()
    app.intercoms = {str(ic.id): ic for ic in icoms}
    return web.json_response([intercom_to_dict(ic) for ic in icoms])


async def handle_watch(request: web.Request) -> web.Response:
    """Запускает StreamHub (WebRTC pass-through) для домофона и WebRTC-камер.

    Тело: {"kind": "intercom"|"camera", "id": <id>, "name": "..."}.
    Для домофона и WebRTC-камер возвращает {"stream": "webrtc", "key": ...} —
    фронтенд затем открывает /ws и через WebRTC показывает видео (как web_view.py).
    Для RTSP-камер возвращает {"stream": "rtsp", "rtsp_url": ..., "snapshot_url": ...}
    без запуска видеопотока (видео в браузер не транслируется).
    """
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    data = await request.json()
    kind = data.get("kind")
    ident = str(data.get("id"))
    name = data.get("name") or ident

    key = f"{kind}:{ident}"
    hub = app.hubs.get(key)

    if kind == "intercom":
        ic = app.intercoms.get(ident)
        if ic is None:
            return _err(f"Домофон {ident} не найден. Сначала выберите дом.")
        if not getattr(ic, "webrtc_supported", False):
            return _err(f"Домофон «{name}» не поддерживает WebRTC.")
        logging.getLogger("Intercom").info(ic)
        relay_id = int(ic.id)
        if hub is None:
            hub = StreamHub(api=app.api, relay_id=relay_id)
            try:
                await asyncio.wait_for(hub.start(), timeout=35.0)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("test_hacs").error("hub start failed: %s", e)
                return _err(f"Не удалось запустить поток: {e}")
            app.hubs[key] = hub
        return web.json_response({"stream": "webrtc", "key": key})

    elif kind == "camera":
        # Камеры: если есть rtsp_url — отдаём только информацию о RTSP-потоке
        # (видео в браузер не транслируем); если rtsp_url отсутствует (None) —
        # это WebRTC-камера (менеджер по типу устройства "devices").
        cam = app.cameras.get(ident)
        if cam is None:
            cam = await app.api.get_camera(ident)
        if cam is None:
            return _err(f"Камера {ident} не найдена. Сначала выберите дом.")
        logging.getLogger("Camera").info(cam)

        rtsp_url = getattr(cam, "rtsp_url", None)
        if rtsp_url:
            # RTSP-камера: не запускаем видеопоток, отдаём только ссылку/снимок.
            return web.json_response({
                "stream": "rtsp",
                "rtsp_url": rtsp_url,
                "snapshot_url": getattr(cam, "live_snapshot_url", None),
                "key": key,
            })

        # rtsp_url отсутствует -> WebRTC-камера (менеджер по типу устройства "devices").
        relay_id = cam.id
        if hub is None:
            hub = StreamHub(api=app.api, relay_id=relay_id, type_device="devices")
            try:
                await asyncio.wait_for(hub.start(), timeout=35.0)
            except Exception as e:  # noqa: BLE001
                logging.getLogger("test_hacs").error("hub start failed: %s", e)
                return _err(f"Не удалось запустить поток: {e}")
            app.hubs[key] = hub
        return web.json_response({"stream": "webrtc", "key": key})
    else:
        return _err("Неизвестный kind (нужен intercom или camera).")


async def handle_stop(request: web.Request) -> web.Response:
    """Останавливает StreamHub по ключу вида intercom:<id>."""
    app: HacsApp = request.app["hacs"]
    data = await request.json()
    key = data.get("key")
    hub = app.hubs.pop(key, None)
    if hub is not None:
        try:
            await hub.stop()
        except Exception:  # noqa: BLE001
            pass
    return web.json_response({"stopped": key})


async def handle_door_open(request: web.Request) -> web.Response:
    """Открывает дверь по домофону: вызывает door_open в библиотеке."""
    app: HacsApp = request.app["hacs"]
    await require_auth(app)
    data = await request.json()
    ident = data.get("id")
    if ident is None:
        return _err("Не указан id домофона.")
    try:
        await app.api.door_open(int(ident))
    except Exception as e:  # noqa: BLE001
        logging.getLogger("test_hacs").error("door_open failed: %s", e)
        return _err(f"Не удалось открыть дверь: {e}")
    return web.json_response({"ok": True, "id": ident})


async def handle_ws(request: web.Request) -> web.WebSocketResponse:
    """WebSocket для просмотра видео домофона (pass-through WebRTC).

    Браузер присылает offer, сервер отвечает answer и «переклеивает» треки
    от активного StreamHub в браузерную RTCPeerConnection (как в web_view.py).
    Клиент указывает домофон в query-параметре key=intercom:<id>.
    """
    from aiortc import RTCPeerConnection, RTCSessionDescription
    from aiortc.sdp import candidate_from_sdp

    app: HacsApp = request.app["hacs"]
    key = request.query.get("key")
    hub = app.hubs.get(key) if key else None

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    pc = None
    try:
        if hub is None:
            await ws.send_json({"type": "error", "message": "Нет активного потока. Сначала выберите домофон."})
            return ws
        if hub.video_track is None:
            await ws.send_json({"type": "error", "message": "Нет видеотрека от домофона."})
            return ws

        pc = RTCPeerConnection()
        if hub.video_track is not None:
            pc.addTrack(hub.video_track)
        if hub.audio_track is not None:
            pc.addTrack(hub.audio_track)

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
            except Exception:  # noqa: BLE001
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
                payload = str(cand).split(":", 1)[1] if str(cand).startswith("candidate:") else str(cand)
                candidate = candidate_from_sdp(payload)
                candidate.sdpMid = data.get("sdpMid")
                candidate.sdpMLineIndex = data.get("sdpMLineIndex")
                await pc.addIceCandidate(candidate)
    except Exception as e:  # noqa: BLE001
        try:
            await ws.send_json({"type": "error", "message": str(e)})
        except Exception:  # noqa: BLE001
            pass
    finally:
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass
        if not ws.closed:
            await ws.close()
    return ws


def _err(message: str) -> web.Response:
    return web.json_response({"error": message}, status=400)


# ------------------------------------------------------------------- auth
async def ensure_authorized(api: SAARubetekAPI) -> None:
    """Пытается использовать сохранённый токен, иначе запускает интерактивную авторизацию."""
    try:
        await api.refresh_tokens()
        return
    except AuthorizationRequiredRubetekAPIError:
        pass

    phone = input("Введите номер телефона (c +7, например +79991234567): ").strip()
    method = "email" if "@" in phone else "flash_call"
    if method == "email":
        email = phone
        await api.authentication(email=email)
        code = input("Введите код из письма: ").strip()
        await api.authorization(email=email, code=code)
    else:
        await api.authentication(phone=phone)
        code = input("Введите последние 4 цифры номера звонившего телефона: ").strip()
        await api.authorization(phone=phone, code=code)


# ------------------------------------------------------------------- main
async def main() -> None:
    parser = argparse.ArgumentParser(description="Rubetek HACS-like simulator")
    parser.add_argument("--host", default=HOST)
    parser.add_argument("--port", type=int, default=PORT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    api = SAARubetekAPI()
    await ensure_authorized(api)
    print(f"[auth] Авторизация успешна. access_token получен.")

    app_state = HacsApp(api)

    app = web.Application()
    app["hacs"] = app_state

    app.router.add_get("/", handle_index)
    app.router.add_get("/ws", handle_ws)
    app.router.add_get("/api/houses", handle_houses)
    app.router.add_get("/api/devices", handle_devices)
    app.router.add_get("/api/cameras", handle_cameras)
    app.router.add_get("/api/intercoms", handle_intercoms)
    app.router.add_post("/api/watch", handle_watch)
    app.router.add_post("/api/stop", handle_stop)
    app.router.add_post("/api/door_open", handle_door_open)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, args.host, args.port)
    try:
        await site.start()
    except OSError as e:  # например, порт уже занят (Errno 48)
        await runner.cleanup()
        await api.close()
        if e.errno == 48:  # address already in use
            print(f"[error] Порт {args.port} уже занят. ",
                  "Найдите процесс:`lsof -nP -iTCP:{0}` / kill его, ".format(args.port),
                  "или укажите другой: --port 8091")
        else:
            print(f"[error] Не удалось поднять HTTP-сервер: {e}")
        sys.exit(1)

    print(f"[web] Откройте http://{args.host}:{args.port}/ (Ctrl+C для выхода)")

    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        pass
    finally:
        # останавливаем все активные WebRTC-хабы (с таймаутом, т.к. WebRTC может висеть)
        for key, hub in list(app_state.hubs.items()):
            try:
                await asyncio.wait_for(hub.stop(), timeout=2.0)
            except Exception:  # noqa: BLE001
                pass
        app_state.hubs.clear()
        try:
            await asyncio.wait_for(runner.cleanup(), timeout=2.0)
        except Exception:  # noqa: BLE001
            pass
        try:
            await api.close()
        except Exception:  # noqa: BLE001
            pass
        print("[web] Сервер остановлен.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass