from urllib.parse import urljoin
import asyncio
import logging
import ssl
import secrets
import string
import aiohttp

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
                 sslEnabled: Optional[bool] = True,
                 timeout: Optional[int] = 30,
                 retry_count: Optional[int] = 5,
                 retry_timeout_ms: Optional[int] = 1000,
                 logging_level: Optional[str] = "INFO",
                 device_id: Optional[str] = None):

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
        self.sslEnabled: Optional[bool] = sslEnabled
        self.timeout: Optional[int] = timeout
        self.retry_count: Optional[int] = retry_count
        self.retry_timeout_ms: Optional[int] = retry_timeout_ms
        self.device_id: Optional[str] = device_id or self.generate_device_uid()

        if self.sslEnabled:
            ssl_context = ssl.create_default_context(cafile=certifi.where())
            self.session: ClientSession = ClientSession(connector=TCPConnector(ssl=ssl_context), timeout=ClientTimeout(total=timeout))
        else:
            connector = aiohttp.TCPConnector(ssl=False)
            self.session: ClientSession = aiohttp.ClientSession(connector=connector)

        self.__logger.debug(f"Initialization done. Items=({', '.join(f'{k}={v}' for k, v in vars(self).items())})")

    async def send_request(self, url: str,
                           method: str = "GET",
                           headers: Dict[str, Any] = None,
                           params: Dict[str, Any] = None,
                           json_data: Dict[str, Any] = None,
                           name: str = "UNKNOWN",
                           request_uid: Optional[str] = None):

        headers = headers or {}
        params = params or {}
        json_data = json_data or {}

        if request_uid is None:
            request_uid = uuid4().hex

        attempt = 0

        while attempt < self.retry_count:
            _headers = self.default_headers(headers)
            self.__logger.info('[%s] Request=%s method=%s url=%s params=%s json=%s headers=%s',
                               name, request_uid, method, url, params, json_data, _headers)
            try:
                async with self.session.request(method, url, params=params, json=json_data, headers=_headers) as response:
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
                await asyncio.sleep((self.retry_timeout_ms / 1000) * attempt)
            except UnauthorizedRubetekAPIError as error:
                self.__logger.warning("[%s] The token may have expired. Get a new one")
                await self.refresh_tokens(request_uid = request_uid)
                await asyncio.sleep((self.retry_timeout_ms / 1000) * attempt)
            finally:
                attempt += 1

    @staticmethod
    def generate_device_uid() -> str:
        charset = string.ascii_letters + string.digits
        device_id = ''.join(secrets.choice(charset) for _ in range(16))
        return device_id

    def default_headers(self, new_headers: dict | None = None) -> dict:
        headers = {
            "Content-Type": "application/json; charset=UTF-8",
            "Device-Client-UID": self.device_id,
            "Device-Client-App": "Rubetek",
            "Accept-Language": "ru",
            "Device-Client-OS": "Android",
            "User-Agent": "okhttp/5.3.2"
        }
        if new_headers is not None:
            headers.update(new_headers)
        return headers

    async def authentication(self, email: str = None, phone: str = None, method: str = "flash_call"):
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
        if not ((email is not None) ^ (phone is not None)):
            raise ValueError('Either "email" or "phone" must be provided.')

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
        response = await self.send_request(url=url, method='POST', headers=headers, params=params, name="AUTHORIZATION.STEP_1")
        token = SimpleNamespace(**response)
        self.__logger.debug("IOT: %s", token)

        self.iot_access_token = getattr(token, 'access_token', None)

        url = urljoin(self.__base_url, 'v5/oauth/iot')
        payload = {
            'client_id': 'rubetek_android',
            'token': f'Bearer {self.iot_access_token}'
        }

        self.__logger.debug("Get refresh token and access token...")
        response = await self.send_request(url=url, method='POST', json_data=payload, name="AUTHORIZATION.STEP_2")
        token = SimpleNamespace(**response)
        self.access_token = getattr(token, 'access_token', None)
        self.save_refresh_token(refresh_token=getattr(token, 'refresh_token', None))
        return self.refresh_token, self.access_token, self.iot_access_token


    def save_refresh_token(self, refresh_token):
        if self.refresh_token_file and self.save_token_file:
            with open(self.refresh_token_file, 'w', encoding='utf-8') as f:
                f.write(refresh_token)
            self.__logger.info('New refresh token saved to file %s', self.refresh_token_file)
        else:
            self.__logger.warning('We do not save the token to a file')
        self.refresh_token = refresh_token

    async def refresh_tokens(self, request_uid: str=None):
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
        url = urljoin(self.__base_url, 'v5/oauth/access_token')
        payload = {
            'client_id': 'rubetek_android',
            'grant_type': 'refresh_token',
            'refresh_token': self.refresh_token
        }
        response = await self.send_request(url=url, method='POST', json_data=payload, name="REFRESH_ACCESS_TOKEN", request_uid=request_uid)
        token = SimpleNamespace(**response)
        self.__logger.debug("Access token updated")
        self.access_token = getattr(token, 'access_token', None)

        return self.access_token

    async def get_iot_access_token(self, request_uid: str = None) -> str:
        url = urljoin(self.__base_url, 'v5/iot/auth_token')
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Accept": "application/json"
        }
        payload = {
            'client_id': self.__client_id
        }
        response = await self.send_request(url=url, method='POST', json_data=payload, headers=headers, name="REFRESH_IOT_ACCESS_TOKEN", request_uid=request_uid)
        token = SimpleNamespace(**response)
        self.__logger.debug("IoT Access token updated")
        self.iot_access_token = getattr(token, 'access_token', None)
        return self.iot_access_token

    async def get_user(self) -> User:
        url = urljoin(self.__base_url, 'v5/user')
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        response = await self.send_request(url=url, headers=headers, name="GET_USER")
        return User(**response)

    async def get_homes(self):
        url = urljoin(self.__base_url, 'v6/houses')
        headers = {
            "Authorization": f"Bearer {self.access_token}"
        }
        response = await self.send_request(url=url, headers=headers, name="GET_HOUSES")
        return [House(**h) for h in response]


    async def get_devices(self):
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
        devices = await self.get_devices()
        device = next((d for d in devices if d.id == device_id), None)
        return device

    async def get_cameras(self):
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
        cameras = await self.get_cameras()
        camera = next((d for d in cameras if d.id == camera_id), None)
        return camera

    async def get_intercoms(self):
        if self.house_id is None:
            raise ValueError("House ID not set")
        url = urljoin(self.__iot_url, f'api/personal/v1/homes/{self.house_id}/relays')
        headers = {
            "Authorization": f"Bearer {self.iot_access_token}"
        }
        response = await self.send_request(method='GET', url=url, headers=headers, name="GET_INTERCOMS")
        return [Intercom(**c) for c in response]

    async def close(self):
        if not self.session.closed:
            await self.session.close()
