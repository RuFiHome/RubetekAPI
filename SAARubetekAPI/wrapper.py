from urllib.parse import urljoin
import asyncio
import logging
import ssl
import secrets
import string

from types import SimpleNamespace

from .logger import SafeLogger
from json.decoder import JSONDecodeError
from pathlib import Path

from uuid import uuid4
from typing import (Any,
                    Dict,
                    Optional,
                    Union)

import certifi
from aiohttp import (ClientSession,
                     ClientTimeout,
                     TCPConnector)
from aiohttp.client_exceptions import (ClientConnectorError,
                                       ContentTypeError)

from .exceptions import (AuthorizationRequiredRubetekAPIError,
                         ClientConnectorRubetekAPIError,
                         TimeoutRubetekAPIError,
                         UnauthorizedRubetekAPIError,
                         DDosRubetekAPIError,
                         UnknownRubetekAPIError)

from contextlib import suppress

from .models import User, House, Intercom, Device, Camera


class SAARubetekAPI:
    __iot_url: str = "https://iot.rubetek.com/"
    __base_url: str = "https://ccc.rubetek.com/"
    __client_id: str = "ckvfvkClm2IdPrkSlvWSe3KiEWJOAbyKOQR5giCYYAo" # Harcode
    __cl_sec: str = "_TiXiy8xkVmVEpTBoYndqvyYbldXFs00wBtgLNmSOCE" # Hardcode
    __logger = SafeLogger('RubetekAPI')
    refresh_token: Optional[str] = None
    iot_access_token: Optional[str] = None
    access_token: Optional[str] = None
    house_id: Optional[str] = None
    personal_id: Optional[str] = None
    device_id: Optional[str] = None

    __initial_cookies: str = '_iot_rubetek_com_session=IRsv98v2DswIJJ1i2VKdWcVzOyv8%2BVlPAldUGbFqUe3eKwLT8VK%2BfuWvHFo1JJKapEAqoQOfMTBhfuQ14xgkV7TBQy2hllQTtu2R8J2oo8sgCtPQWRO9aInCxeJB4wQ7UX%2F%2FXiZafoIAjT%2BKrHWAueo6HaH232cje1h2vT4HX0vQarHhLk75SQipMrOuIhefdOQW7fzKamFavxtyquxtrBV9uEhOdQULVbbxQt3AjqGAbAY%2BsHzjgI%2FEIfw0qI8XJcjry1aD3yFex316kDABTGU4PCDJdOm1telhF8rAjpCKe2CMISv3g8okyvx9oc3ELAlbvREG%2BxVonOH2--4WBiJmIgZZT825rb--45HBPHoCrH8aa9ly%2B6cVyA%3D%3D; locale=ru'


    def __init__(self, refresh_token: Optional[str] = None,
                 refresh_token_file: Optional[Path] = Path("./rr_token"),
                 save_token_file: Optional[bool] = True,
                 enabledCaptcha: Optional[bool] = True,
                 insecure: Optional[bool] = False,
                 timeout: Optional[int] = 30,
                 retry_count: Optional[int] = 5,
                 retry_timeout_ms: Optional[int] = 1000,
                 logging_level: Optional[str] = "INFO",
                 device_id: Optional[str] = None,
                 device_id_file: Optional[Path] = None,
                 device_model: str = "Home Assistant",
                 device_os: str = "Android 16"):

        if logging_level.upper() == "INFO":
            self.__logger.setLevel(logging.INFO)
        elif logging_level.upper() == "WARNING":
            self.__logger.setLevel(logging.WARNING)
        elif logging_level.upper() == "ERROR":
            self.__logger.setLevel(logging.ERROR)
        elif logging_level.upper() == "DEBUG":
            self.__logger.setLevel(logging.DEBUG)
        else:
            # Default INFO
            self.__logger.setLevel(logging.INFO)

        self.refresh_token: Optional[str] = refresh_token
        self.refresh_token_file: Optional[Path] = refresh_token_file
        self.save_token_file: Optional[bool] = save_token_file
        self.enabledCaptcha: Optional[bool] = enabledCaptcha
        self.insecure: Optional[bool] = insecure
        self.timeout: Optional[int] = timeout
        self.retry_count: Optional[int] = retry_count
        self.retry_timeout_ms: Optional[int] = retry_timeout_ms
        # device_id по умолчанию кладём туда же, где лежит rr_token (refresh_token_file).
        if device_id_file is None or device_id_file == Path("./device_id"):
            rtf = self.refresh_token_file
            device_id_file = (Path(rtf).parent / "device_id") if rtf else Path("./device_id")
        self.device_id_file: Optional[Path] = device_id_file or self._default_device_id_file()
        self.device_model = device_model
        self.device_os = device_os
        self.device_id: Optional[str] = device_id or self._load_device_id()
        if not self.device_id:
            self.device_id = self.generate_device_uid()
            self._save_device_id(self.device_id)

        self.session = None

        self.__logger.debug(f"Initialization done. Items=({', '.join(f'{k}={v}' for k, v in vars(self).items())})")

    async def _ensure_session(self) -> ClientSession:
        if self.session and not self.session.closed:
            return self.session
        if not self.insecure:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self.session = ClientSession(connector=TCPConnector(ssl=ssl_context), timeout=ClientTimeout(total=self.timeout))
        else:
            connector = TCPConnector(ssl=False)
            self.session = ClientSession(connector=connector, timeout=ClientTimeout(total=self.timeout))

        return self.session

    async def __aenter__(self):
        await self._ensure_session()
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    async def send_request(self, url: str,
                           method: str = "GET",
                           headers: Dict[str, Any] = None,
                           params: Dict[str, Any] = None,
                           json_data: Dict[str, Any] = None,
                           name: str = "UNKNOWN",
                           request_uid: Optional[str] = None,
                           refresh_on_unauthorized: bool = True):
        """Send an HTTP request to the Rubetek API with automatic retries and error handling.

        Handles token refresh on 401, exponential backoff on 429 (rate limiting),
        and raises the appropriate RubetekAPIError subclass on failure.

        Args:
            url: Full request URL.
            method: HTTP method (default "GET").
            headers: Additional request headers merged with the defaults.
            params: URL query parameters.
            json_data: JSON payload sent in the request body.
            name: Human-readable name of the request (used in logs).
            request_uid: Unique request identifier (generated if not provided).
            refresh_on_unauthorized: Whether to refresh tokens automatically on a 401 response.

        Returns:
            Parsed JSON response body on success.

        Raises:
            TimeoutRubetekAPIError: If the request times out.
            ClientConnectorRubetekAPIError: If a connection-level error occurs.
            DDosRubetekAPIError: If the request keeps failing due to rate limiting (429).
            UnknownRubetekAPIError: For any other unsuccessful response or when retries are exhausted.
        """
        headers = headers or {}
        params = params or {}
        json_data = json_data or {}

        if request_uid is None:
            request_uid = uuid4().hex

        attempt = 0
        last_error: Optional[Exception] = None

        while attempt < self.retry_count:
            _headers = self.default_headers(headers)
            self.__logger.info('[%s] Request=%s method=%s url=%s params=%s json=%s headers=%s',
                               name, request_uid, method, url, params, json_data, _headers)
            try:
                session = await self._ensure_session()
                async with session.request(method, url, params=params, json=json_data, headers=_headers) as response:
                    try:
                        json_response = await response.json() if response.status == 200 else {}
                    except (JSONDecodeError, ContentTypeError) as e:
                        raw_response = await response.text()
                        self.__logger.error('[%s] Response=%s unsuccessful request status=%s reason=%s raw=%s error=%s',
                                            name, request_uid, response.status, response.reason, raw_response, e)
                        raise UnknownRubetekAPIError(f'Unknown error: {response.status} {response.reason}')
                    if response.status == 401:
                        self.__logger.error("[%s] Response=%s Status=%s. Body=%s", name, request_uid, response.status, str(await response.text()))
                        raise UnauthorizedRubetekAPIError(json_response)
                    if response.status == 429:
                        self.__logger.error("[%s] Response=%s Status=%s. Body=%s", name, request_uid, response.status, str(await response.text()))
                        raise DDosRubetekAPIError("429 DDoS")
                    if response.status not in (200, 201, 204):
                        self.__logger.error('[%s] Response=%s unsuccessful request json_response=%s Status=%s Reason=%s',
                                            name, request_uid, json_response, response.status, response.reason)
                        raise UnknownRubetekAPIError(json_response.get('error_description') or json_response.get('error') or json_response)

                    return json_response

            except asyncio.exceptions.TimeoutError:
                self.__logger.error('[%s] Response=%s TimeoutRubetekAPIError', name, request_uid)
                raise TimeoutRubetekAPIError('Timeout error')
            except ClientConnectorError:

                self.__logger.error('[%s] Response=%s ClientConnectorRubetekAPIError', name, request_uid)
                raise ClientConnectorRubetekAPIError('Client connector error')
            except DDosRubetekAPIError:
                last_error = DDosRubetekAPIError("429 DDoS")
                await asyncio.sleep((self.retry_timeout_ms / 1000) * attempt)
            except UnauthorizedRubetekAPIError as error:
                if not refresh_on_unauthorized:
                    raise
                last_error = error
                self.__logger.warning("[%s] The token may have expired. Get a new one", name, request_uid)
                await self.refresh_tokens(request_uid = request_uid)
                await asyncio.sleep((self.retry_timeout_ms / 1000) * attempt)
            finally:
                attempt += 1

        if last_error is not None:
            raise last_error
        raise UnknownRubetekAPIError("Request failed after all retry attempts")

    @staticmethod
    def generate_device_uid() -> str:
        """Generate a cryptographically random device UID (16 alphanumeric characters).

        Used as the "Device-Client-UID" header value to emulate a unique client device.

        Returns:
            A random 16-character string of letters and digits.
        """
        charset = string.ascii_letters + string.digits
        device_id = ''.join(secrets.choice(charset) for _ in range(16))
        return device_id

    def _load_device_id(self) -> Optional[str]:
        """Read a previously persisted device id from ``device_id_file``.

        Returns the stored id (trimmed) or ``None`` when the file is absent,
        empty, or reading fails.
        """
        if not self.device_id_file:
            return None
        try:
            with open(self.device_id_file, encoding='utf-8') as f:
                value = f.read().strip()
            return value or None
        except OSError:
            return None

    def _save_device_id(self, device_id: str) -> None:
        """Persist the given device id to ``device_id_file``."""
        if not self.device_id_file:
            return
        try:
            with open(self.device_id_file, 'w', encoding='utf-8') as f:
                f.write(device_id)
        except OSError:
            self.__logger.warning('Could not save device id to %s', self.device_id_file)

    def default_headers(self, new_headers: dict | None = None) -> dict:
        """Build the default set of HTTP headers used for API requests.

        The defaults mimic the official Rubetek Android app (User-Agent "okhttp/",
        device identity, locale). Provided headers are merged on top of the defaults.

        Args:
            new_headers: Optional extra headers to merge into the defaults.

        Returns:
            A dict of merged request headers.
        """
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Cache-Control": "no-cache",
            "Device-Client-UID": self.device_id,
            "Device-Client-App": "Rubetek",
            "Accept-Language": "ru",
            "Device-Client-OS": self.device_os,
            # Формат заголовка повторяет реальный клиент (Ruband/... (модель; ОС; id: uid)),
            # чтобы ccc.rubetek.com распознал устройство как Home Assistant на Linux.
            "User-Agent": f"Ruband/home-assistant ({self.device_model}; {self.device_os}; id: {self.device_id})"
        }
        if new_headers is not None:
            headers.update(new_headers)
        return headers

    async def authentication(self, email: str = None, phone: str = None, method: str = "flash_call"):
        """Request a verification code sent to the user's email or phone.

        Exactly one of ``email`` or ``phone`` must be provided. The code is delivered
        via email, SMS, or a flash call depending on ``method``.

        Args:
            email: User's email address.
            phone: User's phone number.
            method: Delivery method: "email", "sms", or "flash_call" (default "flash_call").
                Flash call uses a 4-digit code, other methods use a 6-digit code.

        Raises:
            ValueError: If both or neither ``email`` and ``phone`` are provided.
        """
        length_code = 4 if method == "flash_call" and email is None else 6

        if not ((email is not None) ^ (phone is not None)):
            raise ValueError('Either "email" or "phone" must be provided.')

        url = urljoin(self.__iot_url, 'api/v1/code_requests')
        payload = {
            'code_request': {
                'length': length_code
            }
        }
        if email is not None:
            payload['code_request'].update({'email': email, 'method': 'email'})
        elif method == "flash_call":
            payload['code_request'].update({'phone': phone, 'method': 'flash_call'})
        else:
            payload['code_request'].update({'phone': phone, 'method': 'sms'})
        headers = {
            'Cookie': self.__initial_cookies
        } if self.__initial_cookies else {}

        self.__logger.info("Sending a confirmation request method=%s target=%s...", method, email if email else phone)
        await self.send_request(url=url, method='POST', json_data=payload, headers=headers, name="AUTHENTICATION")

    async def authorization(self, code: Union[int, str], email: Optional[str] = None, phone: Optional[str] = None):
        """Exchange a verification code for API tokens (two-step authorization).

        Performs a full authorization flow: first obtains an IoT token, then builds
        the access token and refresh token via the CCC API. The refresh token is saved.

        Args:
            code: The verification code received via ``authentication``.
            email: User's email address (mutually exclusive with ``phone``).
            phone: User's phone number (mutually exclusive with ``email``).

        Returns:
            A tuple ``(refresh_token, access_token, iot_access_token)``.

        Raises:
            ValueError: If both or neither ``email`` and ``phone`` are provided.
        """
        if not ((email is not None) ^ (phone is not None)):
            raise ValueError('Either "email" or "phone" must be provided.')

        # Новая авторизация = новое устройство: генерируем свежий device id и сохраняем.
        self.device_id = self.generate_device_uid()
        self._save_device_id(self.device_id)

        url = urljoin(self.__iot_url, 'oauth/token')
        params = {
            'client_id': self.__client_id,
            'client_secret': self.__cl_sec,
            'grant_type': 'password',
            'code': str(code),
        }
        if email is not None:
            params['email'] = email
        else:
            params['phone'] = phone

        self.__logger.debug("Get iot token...")
        headers = {
            'Content-Type': 'application/json; charset=UTF-8',
            'User-Agent': 'okhttp/4.12.0'
        }
        response = await self.send_request(url=url, method='POST', headers=headers, params=params, name="AUTHORIZATION.STEP_1", refresh_on_unauthorized=False)
        token = SimpleNamespace(**response)
        self.__logger.debug("IOT: %s", token)

        self.iot_access_token = getattr(token, 'access_token', None)

        url = urljoin(self.__base_url, 'v5/oauth/iot')
        payload = {
            'client_id': 'rubetek_android',
            'token': f'Bearer {self.iot_access_token}'
        }

        self.__logger.debug("Get refresh token and access token...")
        response = await self.send_request(url=url, method='POST', json_data=payload, name="AUTHORIZATION.STEP_2", refresh_on_unauthorized=False)
        token = SimpleNamespace(**response)
        self.access_token = getattr(token, 'access_token', None)
        self.save_refresh_token(refresh_token=getattr(token, 'refresh_token', None))
        return self.refresh_token, self.access_token, self.iot_access_token


    def save_refresh_token(self, refresh_token):
        """Persist the refresh token to the configured file and update the in-memory value.

        Skips file writing when ``save_token_file`` is falsy or no ``refresh_token_file``
        is set.

        Args:
            refresh_token: The new refresh token string to store.
        """
        if self.refresh_token_file and self.save_token_file:
            with open(self.refresh_token_file, 'w', encoding='utf-8') as f:
                f.write(refresh_token)
            self.__logger.info('New refresh token saved to file %s', self.refresh_token_file)
        else:
            self.__logger.warning('We do not save the token to a file')
        self.refresh_token = refresh_token

    async def refresh_tokens(self, request_uid: str=None):
        """Refresh the access and IoT access tokens using the stored refresh token.

        Loads the refresh token from the configured file if it is not already in memory,
        then reissues both access tokens.

        Args:
            request_uid: Unique request identifier propagated to downstream calls (for logs).

        Returns:
            A tuple ``(refresh_token, access_token, iot_access_token)``.

        Raises:
            AuthorizationRequiredRubetekAPIError: If no refresh token is available.
        """
        if not self.refresh_token and self.refresh_token_file and self.save_token_file:
            with suppress(FileNotFoundError):
                with open(self.refresh_token_file, encoding='utf-8') as f:
                    self.refresh_token = f.read()
            if not self.refresh_token:
                self.__logger.error('Response=%s AuthorizationRequiredRubetekAPIError', request_uid)
                raise AuthorizationRequiredRubetekAPIError("Not found refresh token. Need to authorize")
        await self.get_access_token(request_uid=request_uid)
        await self.get_iot_access_token(request_uid=request_uid)
        return self.refresh_token, self.access_token, self.iot_access_token

    async def get_access_token(self, request_uid: str = None) -> str:
        """Obtain a fresh access token by exchanging the stored refresh token.

        Args:
            request_uid: Unique request identifier propagated to the downstream call (for logs).

        Returns:
            The newly issued access token.
        """
        url = urljoin(self.__base_url, 'v5/oauth/access_token')
        payload = {
            'client_id': 'rubetek_android',
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        response = await self.send_request(url=url, method='POST', json_data=payload, name="REFRESH_ACCESS_TOKEN", request_uid=request_uid, refresh_on_unauthorized=False)
        token = SimpleNamespace(**response)
        self.__logger.debug("Access token updated")
        self.access_token = getattr(token, 'access_token', None)

        return self.access_token

    async def get_iot_access_token(self, request_uid: str = None) -> str:
        """Obtain a fresh IoT access token used for the Rubetek IoT API.

        Args:
            request_uid: Unique request identifier propagated to the downstream call (for logs).

        Returns:
            The newly issued IoT access token.
        """
        url = urljoin(self.__base_url, 'v5/iot/auth_token')
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        payload = {
            'client_id': self.__client_id
        }
        response = await self.send_request(url=url, method='POST', json_data=payload, headers=headers, name="REFRESH_IOT_ACCESS_TOKEN", request_uid=request_uid, refresh_on_unauthorized=False)
        token = SimpleNamespace(**response)
        self.__logger.debug("IoT Access token updated")
        self.iot_access_token = getattr(token, 'access_token', None)
        return self.iot_access_token

    async def get_user(self) -> User:
        """Fetch the profile of the currently authorized user.

        Returns:
            A :class:`User` model describing the authenticated user.
        """
        url = urljoin(self.__base_url, 'v5/user')
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        response = await self.send_request(url=url, headers=headers, name="GET_USER")
        return User(**response)

    async def get_homes(self):
        """Fetch all houses (homes) associated with the account.

        Returns:
            A list of :class:`House` models.
        """
        url = urljoin(self.__base_url, 'v6/houses')
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        response = await self.send_request(url=url, headers=headers, name="GET_HOUSES")
        return [House(**h) for h in response]


    async def get_devices(self):
        """Fetch the devices of the current house (excluding cameras).

        Requires ``house_id`` to be set beforehand.

        Returns:
            A list of :class:`Device` models.

        Raises:
            ValueError: If ``house_id`` is not set.
        """
        if self.house_id is None:
            raise ValueError("House ID not set")
        url = urljoin(self.__iot_url, f'api/personal/v1/homes/{self.house_id}/devices')
        params = {'page': 1, 'per_page': 500, 'include_deleted': 'true', 'q[model_type_not_eq]': 'Cameras::Device'}
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(url=url, params=params, headers=headers, name="GET_DEVICES")
        return [Device(**d) for d in response]

    async def get_device(self, device_id: str) -> Optional[Device]:
        """Find a specific device in the current house by its ID.

        Args:
            device_id: The device identifier to search for.

        Returns:
            The matching :class:`Device` model or ``None`` if not found.
        """
        devices = await self.get_devices()
        device = next((d for d in devices if d.id == device_id), None)
        return device

    async def get_cameras(self):
        """Fetch the security cameras of the current house.

        Requires ``house_id`` to be set beforehand.

        Returns:
            A list of :class:`Camera` models.

        Raises:
            ValueError: If ``house_id`` is not set.
        """
        if self.house_id is None:
            raise ValueError("House ID not set")
        url = urljoin(self.__iot_url, f'api/personal/v1/homes/{self.house_id}/devices')
        params = {'page': 1, 'per_page': 500, 'include_deleted': 'true', 'q[model_type_eq]': 'Cameras::Device'}
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(url=url, params=params, headers=headers, name="GET_CAMERAS")
        return [Camera(**c) for c in response]

    async def get_camera(self, camera_id: str) -> Optional[Camera]:
        """Find a specific camera in the current house by its ID.

        Args:
            camera_id: The camera identifier to search for.

        Returns:
            The matching :class:`Camera` model or ``None`` if not found.
        """
        cameras = await self.get_cameras()
        camera = next((d for d in cameras if d.id == camera_id), None)
        return camera

    async def get_intercoms(self):
        """Fetch the intercom (relay) devices of the current house.

        Requires ``house_id`` to be set beforehand.

        Returns:
            A list of :class:`Intercom` models.

        Raises:
            ValueError: If ``house_id`` is not set.
        """
        if self.house_id is None:
            raise ValueError("House ID not set")
        url = urljoin(self.__iot_url, f'api/personal/v1/homes/{self.house_id}/relays')
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(method='GET', url=url, headers=headers, name="GET_INTERCOMS")
        return [Intercom(**c) for c in response]     

    async def door_open(self, relay_id: int):
        """Open the door via the intercom (relay) device.

        Sends an unlock command (empty body) to the relay. Requires the IoT
        access token to be obtained beforehand (via ``refresh_tokens()`` or
        authorization).

        Args:
            relay_id: Numeric id of the intercom (relay) to unlock.

        Returns:
            The JSON response of the unlock request.
        """
        url = urljoin(self.__iot_url, f'api/personal/v1/relays/{relay_id}/unlock')
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(url=url, method='POST', headers=headers,
                                           json_data={}, name="DOOR_OPEN")
        return response

    async def get_webrtc_token(self, relay_id: int, type_device: str = "relays") -> str:
        """Obtain a WebRTC signaling token for a device (intercom or camera).

        The token is required to start a WebRTC session with the device. The
        endpoint differs by device type: intercoms (``type_device="relays"``)
        use ``api/personal/v1/relays/{id}/webrtc_token``, while cameras (any
        other ``type_device``) use
        ``api/personal/v1/homes/{house_id}/devices/{id}/webrtc_token``.

        Args:
            relay_id: Numeric id of the device. For intercoms this is the relay
                id; for cameras it is the device id.
            type_device: Device type used to pick the token endpoint.
                ``"relays"`` (default) selects the intercoms endpoint; any
                other value selects the cameras endpoint (which additionally
                requires ``house_id`` to be set).

        Returns:
            The WebRTC token string.

        Raises:
            ValueError: For the cameras endpoint when ``house_id`` is not set.
            UnknownRubetekAPIError: If the response contains no token.
        """
        if type_device == "relays":
            url = urljoin(self.__iot_url, f'api/personal/v1/relays/{relay_id}/webrtc_token')
        else:
            if self.house_id is None:
                raise ValueError("House ID not set")
            url = urljoin(self.__iot_url, f'api/personal/v1/homes/{self.house_id}/devices/{relay_id}/webrtc_token')
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(url=url, headers=headers, name="GET_WEBRTC_TOKEN")
        token = response.get('token') if isinstance(response, dict) else None
        if not token:
            raise UnknownRubetekAPIError(f'Empty webrtc_token for relay {relay_id}: {response}')
        return token

    async def create_web_rtc_viewer(self, relay_id: int,
                                    webrtc_token: str = None,
                                    on_track=None,
                                    type_device: str = "relays", *,
                                    ice_servers=None, ice_servers_provider=None,
                                    **viewer_options):
        """Create a WebRTC video viewer for a device (without starting it).

        Start it with ``asyncio.create_task(viewer.run())`` for reconnection,
        or ``await viewer.open()`` for one session. ``ice_servers`` overrides
        the async ``ice_servers_provider``; extra viewer_options configure retry
        intervals and timeouts. Requires the extra dependency ``aiortc``
        (and ``websockets``) to be installed.

        Args:
            relay_id: Numeric id of the device. For intercoms this is the relay
                id; for cameras (``type_device="cameras"``) it is the device id
                and the API ``house_id`` must be set beforehand.
            webrtc_token: Optional pre-fetched WebRTC token.
            on_track: Optional callback invoked when a media track is received.
            type_device: Device type used to pick the token endpoint (see
                :meth:`get_webrtc_token`). Default ``"relays"`` for intercoms.

        Returns:
            A configured :class:`WebRTCViewer` instance.
        """
        from .webrtc import WebRTCViewer
        viewer = WebRTCViewer(api=self, relay_id=relay_id, webrtc_token=webrtc_token,
                              type_device=type_device, ice_servers=ice_servers,
                              ice_servers_provider=ice_servers_provider, **viewer_options)
        if on_track is not None:
            viewer.on_track = on_track
        return viewer

    async def watch_video(self, relay_id: int, on_track=None, type_device: str = "relays", *,
                          ice_servers=None, ice_servers_provider=None, **viewer_options):
        """Return a viewer (legacy alias of create_web_rtc_viewer).

        This method retains its existing behavior: it does not start network I/O.
        Start the returned viewer with open() or run(), then await close() in a
        finally block. The latter stops reconnecting permanently.

        Args:
            relay_id: Numeric id of the device (relay id for intercoms, device id
                for cameras).
            on_track: Optional callback invoked when a media track is received.
            type_device: Device type used to pick the token endpoint (see
                :meth:`get_webrtc_token`). Default ``"relays"`` for intercoms.

        Returns:
            An unstarted :class:`WebRTCViewer` instance.
        """
        viewer = await self.create_web_rtc_viewer(relay_id, on_track=on_track,
                                                  type_device=type_device, ice_servers=ice_servers,
                                                  ice_servers_provider=ice_servers_provider,
                                                  **viewer_options)
        return viewer

    async def close(self):
        """Close the underlying HTTP session and release its resources."""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
