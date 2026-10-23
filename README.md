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

WebRTC-видео требует `aiortc`, `websockets` и Java runtime из `jdk4py` для проверки APK:

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
# api уже авторизован; для камеры также задан api.house_id.
# TURN подхватывается автоматически, отдельно настраивать provider не нужно.
viewer = await api.create_web_rtc_viewer(
    relay_id=CAMERA_ID,
    type_device="cameras",  # Для домофона: type_device="relays".
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
    await api.close()  # Закрывает остальные viewer и фоновую проверку APK.
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

#### Автоматический TURN и проверка APK

После обычной авторизации `create_web_rtc_viewer()` и `watch_video()` используют
один общий `RuStoreTurnProvider`, принадлежащий клиенту API. Первый запуск видео
загружает публичный APK официального приложения `com.fsk.home` из RuStore
(около **200 МБ**, максимум 512 МиБ). Аккаунтные токены Rubetek в магазин и CDN
не передаются: загрузчик использует отдельную HTTP-сессию, запрещает redirects
и принимает APK только с `https://static.rustore.ru`.

До чтения TURN библиотека проверяет **подпись v2/v3, целостность APK, закреплённый
сертификат издателя, имя пакета и подписанный номер версии**. Используется
официальная библиотека Android `apksig 8.13.0`: JAR загружается с Google Maven
и проверяется по закреплённому SHA256 перед каждым использованием. Небольшой
проверяемый Java bridge поставляется с исходником; Java runtime устанавливается
через extra `[webrtc]` (`jdk4py`, поддерживаемые Windows/macOS/Linux wheels).
Системную Java устанавливать не требуется. Первый запуск также скачивает
verifier JAR (~500 КБ). Если verifier недоступен, проверки не отключаются.

Сертификат издателя закреплён в `apk_signature.py`; новый сертификат из APK или
метаданных магазина автоматически доверенным не становится. Ротация ключа
потребует проверенного обновления библиотеки. Подпись подтверждает происхождение
и целостность относительно закреплённого ключа; код APK не устанавливается,
не загружается в JVM и не исполняется. Из проверенного файла читаются только
literal-return методы `CloudIotConstantProvider.provideCOTURN*` с ограничением DEX.
Это статические SDK credentials, а не временные TURN-токены из API Rubetek.
Рабочих username/password в исходниках библиотеки нет.

Подписанный APK сохраняется в `%LOCALAPPDATA%/saarubetek-api` на Windows или
`~/.cache/saarubetek-api` на остальных платформах и **повторно проверяется перед
использованием после перезапуска**. На POSIX новые каталоги создаются с правами
0700, файлы кэша — 0600; доступ Windows определяется ACL пользователя.
Отдельного файла с извлечёнными credentials нет, но сам APK содержит эти
константы: кэш следует защищать как конфигурацию подключения.

Пока клиент открыт, фоновая задача проверяет версию каждые **24 часа**, даже
если видео работает без переподключений. При неизменной версии APK повторно не
скачивается. Обновление принимается только после полной проверки; откат версии
относительно уже проверенного APK отклоняется. Новые настройки применяются
при следующих соединениях, без прерывания текущего видео. При недоступности
источника или неверной подписи сохраняется последняя проверенная конфигурация;
ошибка отражается в `provider.last_update_error` и безопасном предупреждении
журнала. Без проверенной конфигурации запуск завершается ошибкой.

Обычные ошибки сети повторяются не чаще раза в 5 минут; ошибка подписи/версии —
не чаще основного интервала (по умолчанию 24 часа). Интервалы настраиваются через
`RuStoreTurnProvider(refresh_seconds=..., failure_retry_seconds=...)`.
`await api.close()` останавливает viewer и фоновую проверку; самостоятельно
созданный provider нужно закрывать через `await provider.close()`.

Для ручного управления передайте `ice_servers=[...]` из приватной конфигурации
или собственный async `ice_servers_provider`, возвращающий словари `urls`,
`username`, `credential`. Эти настройки отключают автоматический provider;
явный список имеет приоритет, включая `ice_servers=[]`. `auto_turn=False`
сохраняет прежний Google STUN без скачивания APK. Локальный
`RuStoreTurnProvider(apk_path="/private/official.apk")` также обязательно проверяет
подпись. Другой пакет требует явно заданных проверенных `trusted_certificates`.
Не публикуйте credentials в Git, SDP или журнале.

Проверки: `python -m unittest discover -s tests -v`. Учётная запись Rubetek не
нужна; криптографические тесты используют публичные AOSP APK и закреплённый
verifier из Google Maven. Сборка bridge описана в `_apkverify/README.md`.

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
