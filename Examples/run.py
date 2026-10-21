import asyncio
from SAARubetekAPI import SAARubetekAPI, WebRTCViewer


from SAARubetekAPI.exceptions import AuthorizationRequiredRubetekAPIError

async def authorize(api):
    """Авторизует API-клиента, при необходимости запрашивая код подтверждения."""
    try:
        await api.refresh_tokens()
    except AuthorizationRequiredRubetekAPIError as error:
        print(error)
        phone = input("Введите номер телефона (c +7): ")
        await api.authentication(phone=phone)
        code = input("Введите последние 4 цифры номера звонившего телефона: ")
        await api.authorization(phone=phone, code=code)
    print("Успешно авторизовались. Получим информацию\n")


async def demo_web_rtc(api):
    """Демонстрирует просмотр видеопотока домофона через WebRTC."""
    intercoms = await api.get_intercoms()
    if not intercoms:
        print("Нет доступных домофонов (intercom).")
        return

    # Будем использовать первый домофон из списка
    intercom = intercoms[0]
    print(f"Домофон id={intercom.id} name={intercom.name!r} "
          f"webrtc_supported={intercom.webrtc_supported}")

    # Создаём вьювер (движок WebRTC-сессии) для выбранного домофона
    viewer = WebRTCViewer(api=api, relay_id=intercom.id, webrtc_token=None)

    def on_track(track):
        # Вызывается, когда сервер начинает присылать медиа-треки
        print(f"[webrtc] Получен трек: {track.kind}")

    viewer.on_track = on_track

    # Устанавливаем соединение и держим поток открытым
    # (open() сам выполняет auth -> open -> SDP handshake + keep-alive)
    print("\nСоединяемся с домофоном по WebRTC...")
    try:
        # Запускаем сессию в отдельной задаче, чтобы можно было прервать её
        session = asyncio.create_task(viewer.open())

        # Ждём, пока медиа-сессия будет установлена
        await viewer.wait_until_open(timeout=30)
        print("[webrtc] Видеопоток активен. Оставляем соединение открытым на 15 сек...")

        await asyncio.sleep(15)

        print("[webrtc] Закрываем сессию.")
        session.cancel()
    except asyncio.TimeoutError:
        print("[webrtc] Не удалось установить соединение за отведённое время.")
    finally:
        await viewer.close()


async def test_api():
    api = SAARubetekAPI(device_id="YRuGDRZE5KfoI7jl")
    try:
        await authorize(api)
        user = await api.get_user()
        print("User=(%s)" % str(user))
        house = await api.get_homes()
        print("Homes=(%s)" % house)
        print("Нам нужен только 1 дом")
        api.house_id = house[0].id if house else None
        print("Установили дом: %s\n" % api.house_id)

        intercoms = await api.get_intercoms()
        print("INTERCOMS=(%s)" % intercoms)
        devices = await api.get_devices()
        print("Devices=(%s)" % devices)
        cameras = await api.get_cameras()
        print("Cameras=(%s)\n" % cameras)

        await demo_web_rtc(api)
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(test_api())