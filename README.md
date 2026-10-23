# Rubetek API Library

[![Python Version](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![HACS](https://img.shields.io/badge/HACS-custom-orange.svg)](https://hacs.xyz)

> Асинхронная Python-библиотека для работы с облачной платформой умного дома **Rubetek**.

## 📋 Описание

Библиотека предоставляет удобный асинхронный интерфейс для подключения к облачной платформе
Rubetek: управление сессией, получение данных об устройствах, камерах и домофонах,
а также мониторинг состояния всей экосистемы. Основана на
[RubetekSocketApi](https://github.com/regenara/rubetek_socket_api).

> **Внимание:** проект является сторонней, неофициальной библиотекой и **не имеет отношения
> к компании Rubetek** — это независимая реализация под клиентский API.

## ✨ Возможности

- 🔐 Авторизация и управление сессией подключения к Rubetek Cloud
- 🏠 Получение списка домов (homes) и устройств с текущим состоянием
- 🎥 Работа с камерами: статус, live-снимок, доступ к RTSP-потоку
- 🏢 Домофоны (intercoms): статус и поддержка WebRTC
- 📺 Просмотр видеопотока домофона через **WebRTC** (`WebRTCViewer`)
- 🔔 Подписка на события в реальном времени (WebSocket)
- 🔄 Автоматическое переподключение и обновление токенов при обрыве связи
- 🪵 Логирование с маскированием чувствительных данных и настраиваемым уровнем детализации

## 🖼️ Интерфейс

Интерфейс HACS-подобной панели (`Examples/test_portal.py`):

| Устройства | Камеры | Домофоны |
|:---:|:---:|:---:|
| ![Устройства](Resources/Devices.png) | ![Камеры](Resources/Cameras.png) | ![Домофоны](Resources/Intercoms.png) |

## 🚀 Установка

### Базовый пакет

```bash
pip install saarubetek-api
```

> Установка из исходников:

```bash
pip install git+https://github.com/RuFiHome/RubetekAPI.git
```

### Дополнительные зависимости (по функционалу)

WebRTC-видео требует `aiortc` и `websockets`:

```bash
pip install "saarubetek-api[webrtc]"
```

## 🧑‍💻 Быстрый старт

```python
import asyncio
from SAARubetekAPI import SAARubetekAPI
from SAARubetekAPI.exceptions import AuthorizationRequiredRubetekAPIError

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

        user = await api.get_user()
        print("User:", user)

        houses = await api.get_homes()
        api.house_id = houses[0].id if houses else None
        print("Homes:", houses)

        devices = await api.get_devices()
        print("Devices:", devices)

        cameras = await api.get_cameras()
        print("Cameras:", cameras)

        intercoms = await api.get_intercoms()
        print("Intercoms:", intercoms)
    finally:
        await api.close()

if __name__ == "__main__":
    asyncio.run(main())
```

Полный пример с просмотром WebRTC-потока домофона — в [`Examples/run.py`](Examples/run.py).

## Надёжное восстановление WebRTC и автоматический TURN

`open()` обслуживает одно соединение и возвращает ошибку при обрыве.
`run()` восстанавливает соединение с увеличением пауз (по умолчанию 2–30 секунд,
с небольшим случайным разбросом). После стабильного соединения в течение
30 секунд пауза сбрасывается. Для каждой новой попытки запрашивается новый
видеотокен; явно переданный `webrtc_token` используется только в первой попытке.

```python
import asyncio
from SAARubetekAPI.turn import RuStoreTurnProvider

# api уже авторизован; для камеры также задан api.house_id.
# Один provider можно разделять между несколькими viewer.
provider = RuStoreTurnProvider()
viewer = await api.create_web_rtc_viewer(
    relay_id=CAMERA_ID,
    type_device="cameras",  # Для домофона: type_device="relays".
    ice_servers_provider=provider,
    offer_timeout=30,
    retry_initial=2,
    retry_max=30,
)
viewer.on_track = on_track  # Обработчик должен читать полученные медиатреки.
runner = asyncio.create_task(viewer.run())
try:
    # Ожидает подключённого WebRTC transport, а не только обмена SDP.
    # Первый автоматический download APK может занять несколько минут.
    await viewer.wait_until_open(timeout=360)
    await application_stop_event.wait()
finally:
    await viewer.close()
    await runner
```

`close()` окончательно останавливает этот viewer, включая ожидание токена,
TURN provider и паузу между попытками. Для повторного просмотра создайте новый
viewer. Повторный или одновременный `close()` безопасен; отмена ожидающего
`close()` не отменяет освобождение ресурсов. Удаление TURN allocation средствами
`aiortc` может ждать сетевых повторов — завершение `close()` в такой ситуации
занимает больше времени, но новых подключений уже не будет. Не отменяйте
вручную внутренние задачи или `RTCPeerConnection.close()`.

`wait_until_open()` не гарантирует получение первого декодированного кадра;
проверку кадров выполняет ваш `on_track`. Timeout ожидания готовности сам по
себе не закрывает viewer: используйте `finally`, как в примере.
Метод `watch_video()` сохраняет прежнее поведение: возвращает **незапущенный**
viewer, как `create_web_rtc_viewer()`; он тоже принимает настройки TURN/повторов.

#### Источник автоматических TURN credentials

В проверенном официальном клиенте `com.fsk.home` TURN-настройки — статические
константы SDK, а не временные credentials, выдаваемые наблюдаемым API.
`RuStoreTurnProvider` получает публичный APK этого приложения через HTTPS
RuStore и читает только методы `CloudIotConstantProvider.provideCOTURN*`.
APK не устанавливается, не исполняется и не распаковывается на файловую систему.
Рабочие username/password отсутствуют в исходниках и примерах библиотеки.

- Режим **явно включается** через `ice_servers_provider`: обычный viewer не
  скачивает APK и сохраняет прежний Google STUN по умолчанию.
- Первый download — примерно **200 МБ**, максимум512МиБ. Для уже работающего
  provider версия проверяется раз в24часа, APK скачивается повторно только
  при изменении версии. Объект provider следует переиспользовать.
- Кэш credentials только в памяти, без записи секретов на диск. После ошибки
  повторный запрос к источнику откладывается на5минут, чтобы восстановление
  видео не превращалось в цикл скачиваний. Интервалы настраиваются параметрами
  `refresh_seconds` и `failure_retry_seconds`.
- Доверяется HTTPS-дистрибуции RuStore, **проверка подписи APK не реализована**.
  Для собственного контроля подписи можно передать проверенный локальный APK:
  `RuStoreTurnProvider(apk_path="/private/verified-official.apk")`.
- Формат SDK/пакета и API магазина могут измениться. Неподдерживаемый формат
  вызывает `TurnConfigurationError`, без вывода credentials и без незаметного
  перехода на STUN. Это получение текущей конфигурации приложения, а не
  выдача/продление краткоживущих TURN credentials сервером Rubetek.
- Аккаунтные токены Rubetek не передаются в RuStore/CDN: загрузчик использует
  отдельную HTTP-сессию, запрещает redirects и принимает APK только с
  `https://static.rustore.ru`. Новых зависимостей помимо существующего aiohttp нет.

Можно использовать собственный async provider, получающий временные credentials
из вашего сервиса: он вызывается перед каждой новой попыткой и возвращает список
словарей `urls`, `username`, `credential` для `aiortc.RTCIceServer`.
Для ручной настройки передайте `ice_servers=[...]` из приватной конфигурации.
Явный `ice_servers` имеет приоритет над provider; `ice_servers=[]` отключает
внешние ICE-серверы. Не публикуйте credentials в Git, SDP или журнале.

Проверки без облака: `python -m unittest discover -s tests -v`.

## 📁 Структура проекта

- `SAARubetekAPI/` — исходный код библиотеки
  - `wrapper.py` — основной клиент `SAARubetekAPI`
  - `models.py` — Pydantic-модели (User, House, Device, Camera, Intercom)
  - `webrtc.py` — просмотр WebRTC-видео домофона (`WebRTCViewer`)
  - `exceptions.py` — иерархия ошибок библиотеки
  - `logger.py` — логирование с маскированием токенов/телефонов
- `Examples/` — примеры использования

## ⚖️ Лицензия

[MIT](LICENSE)
