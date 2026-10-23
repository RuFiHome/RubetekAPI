"""Opt-in extraction of TURN configuration from the official app, never executed.

The reviewed Android client uses static SDK constants, not an observed TURN
credential issuance endpoint. No vendor password is embedded in this module.
RuStore is an external service: extraction is opt-in and can break on app changes.
"""
import asyncio
from copy import deepcopy
import re
import struct
import tempfile
import time
from urllib.parse import urlsplit
import zipfile

MAX_APK = 512 * 1024**2
MAX_DEX = 64 * 1024**2
OWNER = 'Lcom/rubetek/client/repositories/api/CloudIotConstantProvider;'
METHODS = ('provideCOTURN', 'provideCOTURN_USS', 'provideCOTURN_PASS')


class TurnConfigurationError(RuntimeError):
    """Configuration could not be obtained; errors never contain credentials."""


def _dex_constants(data):
    """Read only const-string[/jumbo] + return-object provider methods, no VM."""
    def u32(offset):
        return struct.unpack_from('<I', data, offset)[0]

    def leb(offset):
        value = 0
        for shift in range(0, 35, 7):
            byte = data[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value, offset
        raise ValueError('Invalid LEB128')

    if len(data) < 112 or not data.startswith(b'dex\n') or u32(32) != len(data) or u32(40) != 0x12345678:
        raise ValueError('Invalid DEX header')
    strings = []
    count, offset = u32(56), u32(60)
    if count > min(1000000, len(data) // 4) or offset + 4 * count > len(data):
        raise ValueError('Invalid string table')
    for i in range(count):
        _, start = leb(u32(offset + 4 * i))
        end = data.index(b'\0', start)
        # Only identifiers and returned constants need to be ASCII/UTF-8. DEX
        # metadata may use modified UTF-8; replace it rather than executing code.
        strings.append(data[start:end].decode('utf-8', errors='replace'))
    for count_offset, table_offset, stride in ((64, 68, 4), (88, 92, 8), (96, 100, 32)):
        if u32(count_offset) > 65536 or u32(table_offset) + u32(count_offset) * stride > len(data):
            raise ValueError('Invalid DEX table')
    types = [strings[u32(u32(68) + 4 * i)] for i in range(u32(64))]
    methods = [struct.unpack_from('<HHI', data, u32(92) + 8 * i) for i in range(u32(88))]
    result = {}
    for i in range(u32(96)):
        fields = struct.unpack_from('<8I', data, u32(100) + 32 * i)
        if types[fields[0]] != OWNER or not fields[6]:
            continue
        pos, counts = fields[6], []
        for _ in range(4):
            count, pos = leb(pos)
            counts.append(count)
        for _ in range(counts[0] + counts[1]):
            _, pos = leb(pos)
            _, pos = leb(pos)
        for count in counts[2:]:
            method_index = 0
            for _ in range(count):
                delta, pos = leb(pos)
                method_index += delta
                _, pos = leb(pos)
                code, pos = leb(pos)
                method = methods[method_index]
                name = strings[method[2]]
                if name not in METHODS or not code:
                    continue
                size = u32(code + 12)
                if size not in (3, 4):
                    raise ValueError('Provider method no longer returns a literal')
                units = struct.unpack_from('<' + 'H' * size, data, code + 16)
                opcode, register = units[0] & 255, units[0] >> 8
                if (opcode, size) not in ((0x1a, 3), (0x1b, 4)) or units[-1] != (register << 8 | 0x11):
                    raise ValueError('Unexpected provider instructions')
                index = units[1] if opcode == 0x1a else units[1] | units[2] << 16
                result[name] = strings[index]
    return result


def extract_turn_servers(apk):
    """Extract SDK constants from an APK/file object or a RuStore base-APK bundle.

    All failures use a fixed message: no APK strings, passwords or URLs escape.
    Archives are read in place; nothing is extracted onto the filesystem.
    """
    try:
        with zipfile.ZipFile(apk) as archive:
            nested = [i for i in archive.infolist() if i.filename == 'base.apk' or i.filename.endswith('/base.apk')]
            if not nested:
                apks = [i for i in archive.infolist() if i.filename.endswith('.apk')]
                nested = apks if len(apks) == 1 else []
            if nested:
                if len(nested) != 1 or nested[0].file_size > MAX_APK:
                    raise ValueError('Oversized or ambiguous base APK')
                with tempfile.SpooledTemporaryFile(max_size=8 * 1024**2) as base:
                    with archive.open(nested[0]) as stream:
                        while chunk := stream.read(1024**2):
                            base.write(chunk)
                            if base.tell() > MAX_APK:
                                raise ValueError('Oversized APK')
                    base.seek(0)
                    return _extract_apk(base)
        return _extract_apk(apk)
    except Exception:
        raise TurnConfigurationError('Unsupported or invalid vendor APK TURN configuration') from None


def _extract_apk(apk):
    found = []
    budget = 256 * 1024**2
    with zipfile.ZipFile(apk) as archive:
        for info in archive.infolist():
            if not re.fullmatch(r'classes\d*\.dex', info.filename):
                continue
            budget -= info.file_size
            if info.file_size > MAX_DEX or budget < 0:
                raise ValueError('Oversized DEX')
            values = _dex_constants(archive.read(info))
            if values:
                found.append(values)
    if len(found) != 1 or not all(found[0].get(k) for k in METHODS):
        raise ValueError('TURN provider missing or ambiguous')
    values = found[0]
    url = values[METHODS[0]]
    if not url.startswith(('turn:', 'turns:')):
        url = 'turn:' + url
    # Reject a changed/unsupported format rather than guessing credentials.
    if any(c.isspace() for c in url) or '/' in url or '@' in url or not urlsplit(url).path:
        raise ValueError('Invalid TURN URL')
    return [{'urls': url, 'username': values[METHODS[1]], 'credential': values[METHODS[2]]}]


class RuStoreTurnProvider:
    """Async ICE provider downloading the public com.fsk.home app from RuStore.

    Credentials are static SDK constants, not short-lived account credentials.
    Cache is memory-only. The app version is checked after refresh_seconds (24h
    default), and the APK is fetched only if changed. The first download is about 200 MB.
    Supply apk_path to extract from a trusted local APK without any network use.
    No APK signature verification is implemented: HTTPS RuStore distribution is
    trusted. Applications requiring signature pinning should provide a verified
    local APK instead. Failures do not silently downgrade to public STUN.
    """

    def __init__(self, *, apk_path=None, package_name='com.fsk.home', refresh_seconds=86400,
                 failure_retry_seconds=300):
        if not re.fullmatch(r'[a-zA-Z][\w]*(?:\.[a-zA-Z][\w]*)+', package_name):
            raise ValueError('Invalid app package')
        if refresh_seconds <= 0 or not refresh_seconds < float('inf'):
            raise ValueError('refresh_seconds must be positive and finite')
        if failure_retry_seconds <= 0 or not failure_retry_seconds < float('inf'):
            raise ValueError('failure_retry_seconds must be positive and finite')
        self.apk_path, self.package_name = apk_path, package_name
        self.refresh_seconds = refresh_seconds
        self._servers = None
        self._valid_until = 0
        self._retry_after = 0
        self._version_code = None
        self.failure_retry_seconds = failure_retry_seconds
        self._lock = asyncio.Lock()

    async def __call__(self):
        async with self._lock:
            if self._servers is None or time.monotonic() >= self._valid_until:
                if time.monotonic() < self._retry_after:
                    raise TurnConfigurationError('TURN configuration temporarily unavailable; retry later')
                try:
                    if self.apk_path is not None:
                        servers = await asyncio.to_thread(extract_turn_servers, self.apk_path)
                    else:
                        servers = await self._download()
                except TurnConfigurationError:
                    self._retry_after = time.monotonic() + self.failure_retry_seconds
                    raise
                self._servers = servers
                self._valid_until = time.monotonic() + self.refresh_seconds
            return deepcopy(self._servers)

    async def _download(self):
        import aiohttp
        headers = {'ruStoreVerCode': '247', 'Content-Type': 'application/json; charset=utf-8'}
        root = 'https://backapi.rustore.ru/applicationData/'
        try:
            # Separate session: Rubetek account Authorization headers/cookies must
            # never be sent to the app store or CDN.
            async with aiohttp.ClientSession(headers=headers, timeout=aiohttp.ClientTimeout(total=300)) as session:
                async with session.get(root + 'overallInfo/' + self.package_name, allow_redirects=False) as response:
                    response.raise_for_status()
                    meta = (await response.json())['body']
                if meta['packageName'] != self.package_name or int(meta['fileSize']) > MAX_APK:
                    raise ValueError('Unexpected package metadata')
                version = str(meta['versionCode'])
                if self._servers is not None and self._version_code == version:
                    return deepcopy(self._servers)
                async with session.post(root + 'download-link', json={'appId': meta['appId'], 'firstInstall': True},
                                        allow_redirects=False) as response:
                    response.raise_for_status()
                    link = (await response.json())['body']
                if str(link['appId']) != str(meta['appId']) or str(link['versionCode']) != version:
                    raise ValueError('Unexpected app id')
                url = link['apkUrl']
                parsed = urlsplit(url)
                if parsed.scheme != 'https' or parsed.hostname != 'static.rustore.ru' or parsed.username is not None or parsed.port not in (None, 443):
                    raise ValueError('Unexpected APK origin')
                with tempfile.TemporaryFile() as apk:
                    async with session.get(url, allow_redirects=False) as response:
                        response.raise_for_status()
                        if response.status != 200:
                            raise ValueError('Unexpected APK response')
                        async for chunk in response.content.iter_chunked(1024**2):
                            apk.write(chunk)
                            if apk.tell() > MAX_APK:
                                raise ValueError('Oversized APK')
                    apk.seek(0)
                    servers = await asyncio.to_thread(extract_turn_servers, apk)
                    self._version_code = version
                    return servers
        except asyncio.CancelledError:
            raise
        except Exception:
            raise TurnConfigurationError('Could not obtain TURN configuration from the official RuStore app') from None
