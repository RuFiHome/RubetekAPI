"""HLS/TS медиа-мост: WebRTC-поток домофона Rubetek -> HLS по HTTP.

Позволяет отдавать видео+аудио с домофона в формате HLS (m3u8 + ts-сегменты
в памяти) через локальный HTTP-сервер, чтобы go2rtc / Home Assistant могли
получать поток по URL.

Зависимости: aiohttp (базовая), av (PyAV), aiortc+websockets (WebRTC).
Все тяжёлые импорты выполняются внутри методов, чтобы пакет импортировался
без необходимости устанавливать av.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

try:  # доступны только в Python 3.10+
    from typing import Dict, List
except ImportError:  # pragma: no cover
    pass


def _frame_seconds(frame) -> Optional[float]:
    """Возвращает время кадра в секундах (по frame.pts/time_base)."""
    import fractions

    pts = getattr(frame, "pts", None)
    if pts is None:
        return None
    tb = getattr(frame, "time_base", None)
    if tb is None:
        return None
    if not isinstance(tb, fractions.Fraction):
        try:
            tb = fractions.Fraction(tb)
        except Exception:
            tb = fractions.Fraction(1, 90000)
    return float(pts * tb)


@dataclass
class _Segment:
    """Один завершённый TS-сегмент."""
    id: int
    data: bytes
    duration: float  # секунды


class _SegmentBuffer:
    """Кольцевой буфер завершённых сегментов в памяти."""

    def __init__(self, window_size: int = 6):
        self._window_size = max(1, window_size)
        self._segments: "Dict[int, _Segment]" = {}
        self._order: "List[int]" = []
        self._next_id = 0

    def add(self, segment: _Segment) -> None:
        self._segments[segment.id] = segment
        self._order.append(segment.id)
        if len(self._order) > self._window_size:
            old = self._order.pop(0)
            self._segments.pop(old, None)

    def get(self, seg_id: int) -> Optional[_Segment]:
        return self._segments.get(seg_id)

    def items(self) -> "List[_Segment]":
        return [self._segments[i] for i in self._order]

    @property
    def first_id(self) -> Optional[int]:
        return self._order[0] if self._order else None

    @property
    def last_id(self) -> Optional[int]:
        return self._order[-1] if self._order else None

    @property
    def max_duration(self) -> float:
        return max((s.duration for s in self._segments.values()), default=1.0)

    def next_id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def clear(self) -> None:
        self._segments.clear()
        self._order.clear()


class HlsStream:
    """Собирает WebRTC-поток домофона Rubetek в HLS и отдаёт его по HTTP.

    Типовое использование::

        bridge = HlsStream(api=api, relay_id=relay_id)
        await bridge.start()
        print(bridge.base_url)  # http://127.0.0.1:{port}/
        # go2rtc: http://127.0.0.1:{port}/index.m3u8
        ...
        await bridge.stop()
    """

    def __init__(
        self,
        api,
        relay_id: int,
        host: str = "0.0.0.0",
        port: int = 0,
        segment_duration: float = 4.0,
        window_size: int = 6,
        video_codec: Optional[str] = None,
        audio_codec: Optional[str] = None,
        timeout: float = 30.0,
    ):
        self._api = api
        self._relay_id = relay_id
        self._host = host
        self._port = port
        self._segment_duration = max(0.5, segment_duration)
        self._window_size = window_size
        self._video_codec = video_codec
        self._audio_codec = audio_codec
        self._timeout = timeout

        self._viewer = None
        self._viewer_task: Optional[asyncio.Task] = None
        self._server = None
        self._runner = None
        self._buffer = _SegmentBuffer(window_size)
        self._stopped = False

        self._video_frames: asyncio.Queue = asyncio.Queue()
        self._audio_frames: asyncio.Queue = asyncio.Queue()
        self._consume_tasks: list = []
        # сколько активных треков (video/audio) мы читаем; когда оба завершились,
        # считаем сессию умершей и останавливаем запись.
        self._tracks_active = 0

        self._base_url: Optional[str] = None

    # ------------------------------------------------------------------ public
    @property
    def base_url(self) -> Optional[str]:
        """URL до HTTP-эндпоинта HLS (доступен после start())."""
        return self._base_url

    async def start(self) -> None:
        """Поднимает HTTP-сервер, открывает WebRTC-сессию и начинает сбор HLS."""
        if self._server is not None:
            return

        import aiohttp.web as web

        # --- HTTP-сервер ---
        app = web.Application()
        app.router.add_get("/index.m3u8", self._handle_m3u8)
        app.router.add_get("/segment/{seg_id}.ts", self._handle_segment)
        app.router.add_get("/", self._handle_playlist)

        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, self._host, self._port)
        await site.start()

        # фактический порт
        real_port = self._port
        if real_port == 0:
            for socket in site._server.sockets:  # noqa: SLF001
                real_port = socket.getsockname()[1]
                break
        host_disp = "127.0.0.1" if self._host in ("0.0.0.0", "::") else self._host
        self._base_url = f"http://{host_disp}:{real_port}"
        self._server = site
        self._runner = runner

        # --- WebRTC ---
        from .webrtc import WebRTCViewer

        viewer = WebRTCViewer(api=self._api, relay_id=self._relay_id)
        viewer.on_track = self._on_track
        self._viewer = viewer

        self._stopped = False
        self._viewer_task = asyncio.create_task(viewer.open())
        try:
            await viewer.wait_until_open(timeout=self._timeout)
        except asyncio.TimeoutError:
            await self._cleanup_viewer()
            await self._shutdown_http()
            raise RuntimeError("Не удалось установить WebRTC-сессию за отведённое время")

        # --- сборщик сегментов ---
        self._record_task = asyncio.create_task(self._record_loop())

    async def stop(self) -> None:
        """Останавливает WebRTC, сборщик и HTTP-сервер."""
        self._stopped = True

        for task in self._consume_tasks:
            task.cancel()
        self._consume_tasks.clear()

        if self._record_task is not None:
            self._record_task.cancel()
            try:
                await self._record_task
            except (asyncio.CancelledError, Exception):
                pass
            self._record_task = None

        await self._cleanup_viewer()
        await self._shutdown_http()
        self._buffer.clear()
        self._base_url = None

    # ------------------------------------------------------------- http handlers
    async def _handle_playlist(self, request) -> "Any":
        return await self._handle_m3u8(request)

    async def _handle_m3u8(self, request):
        import aiohttp.web as web

        segments = self._buffer.items()
        if not segments:
            return web.Response(text="#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n", content_type="application/vnd.apple.mpegurl")

        first = self._buffer.first_id
        target = int(self._buffer.max_duration) + 1
        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{target}",
            f"#EXT-X-MEDIA-SEQUENCE:{first}",
        ]
        for seg in segments:
            lines.append(f"#EXTINF:{seg.duration:.3f},")
            lines.append(f"segment/{seg.id}.ts")
        if self._stopped:
            lines.append("#EXT-X-ENDLIST")

        return web.Response(text="\n".join(lines) + "\n", content_type="application/vnd.apple.mpegurl")

    async def _handle_segment(self, request):
        import aiohttp.web as web

        seg_id = int(request.match_info["seg_id"])
        seg = self._buffer.get(seg_id)
        if seg is None:
            return web.Response(status=404, text="not found")
        return web.Response(body=seg.data, content_type="video/mp2t")

    # --------------------------------------------------------------- webRTC glue
    def _on_track(self, track) -> None:
        """Подключается к viewer.on_track и стартует чтение трека."""
        kind = getattr(track, "kind", "unknown")
        self._tracks_active += 1
        task = asyncio.create_task(self._consume_track(track, kind))
        self._consume_tasks.append(task)

    async def _consume_track(self, track, kind: str) -> None:
        """Читает кадры из трека и кладёт в очередь."""
        queue = self._video_frames if kind == "video" else self._audio_frames
        try:
            while not self._stopped:
                try:
                    frame = await track.recv()
                except Exception:
                    break
                queue.put_nowait(frame)
        finally:
            self._tracks_active -= 1

    # --------------------------------------------------------------- mux / HLS
    async def _record_loop(self) -> None:
        """Воркер: собирает сегменты mpegts из кадров, пишет их в буфер."""
        import av

        active = None  # _ActiveSegment | None
        seg_start_avtime: Optional[float] = None

        while not self._stopped:
            video, audio = None, None

            # 1) ждём хотя бы один кадр
            try:
                item = await asyncio.wait_for(
                    asyncio.gather(
                        self._video_frames.get(),
                        self._audio_frames.get(),
                        return_exceptions=True,
                    ),
                    timeout=0.5,
                )
            except asyncio.TimeoutError:
                # треки завершились (viewer умер/отключился) и кадров нет -> выходим,
                # чтобы не крутить пустой цикл и не блокировать Ctrl+C.
                if self._tracks_active <= 0 and not self._stopped:
                    logging.warning("HLS: все треки WebRTC завершились, запись остановлена.")
                    break
                continue

            # видео-keyframe просто перерабатываем: если сегмент уже идёт и пришёл
            # keyframe и колличество времени > segment_duration -> закрыть сегмент.
            v_item = item[0]
            a_item = item[1]

            v_is_key = getattr(v_item, "key_frame", False) if v_item is not None else False
            v_time = _frame_seconds(v_item) if v_item is not None else None
            a_time = _frame_seconds(a_item) if a_item is not None else None

            # 2) старт первого сегмента на video-keyframe
            if active is None:
                if v_item is None or not v_is_key:
                    # ждём первый video-keyframe; аудио до него просто отбрасываем
                    continue
                # первый кадр - видео keyframe
                active = await asyncio.to_thread(self._make_container, v_item)
                seg_start_avtime = v_time
                if active is None:
                    continue

            # 3) определить, пора ли закрыть сегмент (по next video-keyframe)
            if v_is_key and seg_start_avtime is not None and v_time is not None:
                if (v_time - seg_start_avtime) >= self._segment_duration:
                    # закрыть текущий и открыть новый
                    seg = await asyncio.to_thread(
                        self._close_container, active, seg_start_avtime, v_time
                    )
                    if seg is not None:
                        self._buffer.add(seg)
                    active = await asyncio.to_thread(self._make_container, v_item)
                    seg_start_avtime = v_time

            # 4) записать кадры в active
            if active is not None and seg_start_avtime is not None:
                # видео и аудио лишь из очереди, кладём по одному
                if v_item is not None:
                    await asyncio.to_thread(
                        self._mux_frame, active, v_item, seg_start_avtime
                    )
                if a_item is not None and a_time is not None:
                    await asyncio.to_thread(
                        self._mux_frame, active, a_item, seg_start_avtime
                    )

        # конец: закрыть остаток
        if active is not None and seg_start_avtime is not None:
            seg = await asyncio.to_thread(
                self._close_container, active, seg_start_avtime, None
            )
            if seg is not None:
                self._buffer.add(seg)

    def _make_container(self, first_video):
        """Создаёт новый mpegts-контейнер с видео/аудио стримами."""
        import av, io, fractions

        buf = io.BytesIO()
        cont = av.open(buf, "w", format="mpegts")

        vcodec = self._video_codec or "h264"
        # подбираем pix_fmt под h264/h265
        vstream = cont.add_stream(vcodec, rate=30)
        if hasattr(first_video, "width") and first_video.width:
            vstream.width = first_video.width
        if hasattr(first_video, "height") and first_video.height:
            vstream.height = first_video.height
        vstream.pix_fmt = "yuv420p"
        vstream.time_base = fractions.Fraction(1, 90000)
        try:
            vstream.options = {"preset": "ultrafast", "tune": "zerolatency"}
        except Exception:
            pass

        acodec = self._audio_codec or "aac"
        try:
            astream = cont.add_stream(acodec, rate=48000)
            astream.layout = "stereo"
            astream.format = "fltp"
            astream.time_base = fractions.Fraction(1, 48000)
        except Exception:
            astream = None

        return {"buf": buf, "cont": cont, "vstream": vstream, "astream": astream}

    def _mux_frame(self, c, frame, seg_start_avtime: float) -> None:
        """Ресемплит/реформатит кадр и пишет его в контейнер сегмента."""
        import av, fractions

        is_video = isinstance(frame, av.VideoFrame)
        cont = c["cont"]
        seconds = _frame_seconds(frame)
        if seconds is None:
            return
        rel = seconds - seg_start_avtime
        if rel < 0:
            rel = 0.0

        if is_video:
            fs = cont.streams.video[0] if cont.streams.video else None
            if fs is None:
                return
            try:
                frame = frame.reformat(width=fs.width or frame.width,
                                       height=fs.height or frame.height,
                                       format="yuv420p")
            except Exception:
                pass
            frame.pts = int(round(rel * 90000))
            frame.time_base = fractions.Fraction(1, 90000)
            for packet in fs.encode(frame):
                cont.mux(packet)
        elif isinstance(frame, av.AudioFrame):  # аудио
            as_ = c.get("astream")
            if as_ is None:
                return
            try:
                # ресемплинг в fltp/stereo/48000
                if (
                    frame.format is None
                    or frame.format.name not in ("fltp",)
                    or frame.layout.name != "stereo"
                    or frame.sample_rate != 48000
                ):
                    frame = frame.reformat(
                        format="fltp", layout="stereo", sample_rate=48000
                    )
                frame.pts = int(round(rel * 48000))
                frame.time_base = fractions.Fraction(1, 48000)
                for packet in as_.encode(frame):
                    cont.mux(packet)
            except Exception:
                pass

    def _close_container(self, c, seg_start_avtime: float, end_avtime: Optional[float]) -> Optional[_Segment]:
        """Финализирует контейнер и возвращает готовый TS-сегмент."""
        cont = c["cont"]
        try:
            for s in cont.streams:
                s.close()
        except Exception:
            pass
        cont.close()
        data = c["buf"].getvalue()
        if not data:
            return None
        duration = (end_avtime - seg_start_avtime) if end_avtime else self._segment_duration
        if duration <= 0:
            duration = self._segment_duration
        return _Segment(id=self._buffer.next_id(), data=data, duration=duration)

    # --------------------------------------------------------------- cleanup
    async def _cleanup_viewer(self) -> None:
        # ВАЖНО: WebRTC/декодирование может «залипнуть» (например, шум H264Decoder),
        # поэтому все ожидания оборачиваем в таймаут, чтобы stop() не висел.
        if self._viewer_task is not None:
            self._viewer_task.cancel()
            try:
                await asyncio.wait_for(self._viewer_task, timeout=2.0)
            except (asyncio.CancelledError, Exception):
                pass
            self._viewer_task = None
        if self._viewer is not None:
            try:
                await asyncio.wait_for(self._viewer.close(), timeout=2.0)
            except Exception:
                pass
            self._viewer = None

    async def _shutdown_http(self) -> None:
        if self._runner is not None:
            try:
                await asyncio.wait_for(self._runner.cleanup(), timeout=2.0)
            except Exception:
                pass
            self._runner = None
        self._server = None