"""Extract TURN configuration only from a publisher-authenticated official APK.

The reviewed Android client uses static SDK constants, not an observed TURN
credential issuance endpoint. No vendor password is embedded in this module.
RuStore is an external service; unsupported app formats fail closed.
"""
import asyncio
from copy import deepcopy
import re
import struct
import tempfile
import time
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit
import zipfile
from .apk_signature import verify_apk, default_cache_dir, ApkVerificationError

logger = logging.getLogger(__name__)

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
    A signed APK is cached on disk and reverified before reuse. A background task
    checks the version every refresh_seconds (24h default), downloading only changes.
    The first download is about 200 MB. Local apk_path also requires verification;
    the pinned verifier JAR may need a first download. close() stops periodic checks.
    Invalid updates retain the last authenticated configuration. With no trusted
    configuration, failures never silently downgrade to public STUN.
    """

    def __init__(self, *, apk_path=None, package_name='com.fsk.home', refresh_seconds=86400,
                 failure_retry_seconds=300, cache_dir=None, trusted_certificates=None):
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
        self.cache_dir = Path(cache_dir) if cache_dir is not None else default_cache_dir()
        self.trusted_certificates = trusted_certificates
        self._cache_checked = False
        self._watch_task = None
        self._closed = False
        self.last_update_error = None

    async def __call__(self):
        if self._closed:
            raise TurnConfigurationError('TURN provider is closed')
        async with self._lock:
            if self._closed:
                raise TurnConfigurationError('TURN provider is closed')
            if self.apk_path is None and not self._cache_checked:
                self._cache_checked = True
                cached = self.cache_dir / (self.package_name + '.apk')
                if cached.exists():
                    try:
                        self._servers = await self._read_verified(cached)
                    except (TurnConfigurationError, ApkVerificationError):
                        # A corrupt or untrusted cache is never a credential source.
                        pass
            if self._servers is None or time.monotonic() >= self._valid_until:
                if time.monotonic() < self._retry_after:
                    if self._servers is None:
                        raise TurnConfigurationError('TURN configuration temporarily unavailable; retry later')
                else:
                    try:
                        if self.apk_path is not None:
                            servers = await self._read_verified(self.apk_path)
                        else:
                            servers = await self._download()
                    except (TurnConfigurationError, ApkVerificationError) as error:
                        cooldown = max(self.failure_retry_seconds, self.refresh_seconds) if isinstance(error, ApkVerificationError) else self.failure_retry_seconds
                        self._retry_after = time.monotonic() + cooldown
                        self.last_update_error = type(error).__name__
                        if self._servers is None:
                            raise
                        logger.warning('TURN update rejected/unavailable; keeping last verified configuration (%s)', type(error).__name__)
                    else:
                        self._servers = servers
                        self._valid_until = time.monotonic() + self.refresh_seconds
                        self.last_update_error = None
            if self._closed:
                raise TurnConfigurationError('TURN provider is closed')
            if self._watch_task is None:
                self._watch_task = asyncio.create_task(self._watch_updates())
            return deepcopy(self._servers)

    async def _watch_updates(self):
        try:
            while True:
                await asyncio.sleep(min(self.refresh_seconds, self.failure_retry_seconds))
                try:
                    await self()
                except (TurnConfigurationError, ApkVerificationError):
                    # First retrieval reports the error to the caller; later checks
                    # preserve only previously authenticated configuration.
                    pass
        except asyncio.CancelledError:
            raise

    async def close(self):
        """Stop periodic checks; the owning API closes its default provider."""
        self._closed = True
        if self._watch_task is not None:
            self._watch_task.cancel()
            await asyncio.gather(self._watch_task, return_exceptions=True)
            self._watch_task = None

    async def _read_verified(self, apk, expected_version=None, save=False):
        try:
            return await self._read_verified_impl(apk, expected_version, save)
        except asyncio.CancelledError:
            raise
        except (TurnConfigurationError, ApkVerificationError):
            raise
        except Exception:
            raise ApkVerificationError('Invalid or unsupported signed APK') from None

    async def _read_verified_impl(self, apk, expected_version=None, save=False):
        # Copy the selected base APK into a private immutable-for-this-operation
        # staging file. Verify and parse that exact same file (no TOCTOU reopen).
        with tempfile.TemporaryDirectory(prefix='rubetek-signed-apk-') as directory:
            target = Path(directory) / 'base.apk'
            with zipfile.ZipFile(apk) as archive:
                nested = [i for i in archive.infolist() if i.filename == 'base.apk' or i.filename.endswith('/base.apk')]
                if not nested:
                    apks = [i for i in archive.infolist() if i.filename.endswith('.apk')]
                    nested = apks if len(apks) == 1 else []
                if nested:
                    if len(nested) != 1 or nested[0].file_size > MAX_APK:
                        raise ApkVerificationError('Oversized or ambiguous base APK')
                    with archive.open(nested[0]) as source, target.open('wb') as out:
                        while chunk := source.read(1024**2):
                            out.write(chunk)
                            if out.tell() > MAX_APK:
                                raise ApkVerificationError('Oversized base APK')
                else:
                    if hasattr(apk, 'read'):
                        apk.seek(0)
                        with target.open('wb') as out:
                            while chunk := apk.read(1024**2):
                                out.write(chunk)
                                if out.tell() > MAX_APK:
                                    raise ApkVerificationError('Oversized APK')
                    else:
                        import shutil
                        if Path(apk).stat().st_size > MAX_APK:
                            raise ApkVerificationError('Oversized APK')
                        shutil.copyfile(apk, target)
            identity = await verify_apk(target, package_name=self.package_name,
                version_code=expected_version, trusted_certificates=self.trusted_certificates,
                cache_dir=self.cache_dir)
            version = identity['version']
            if self._version_code is not None and version < int(self._version_code):
                raise ApkVerificationError('APK version rollback rejected')
            servers = await asyncio.to_thread(extract_turn_servers, target)
            if save:
                self.cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, name = tempfile.mkstemp(prefix='.verified-apk-', dir=self.cache_dir)
                try:
                    with os.fdopen(fd, 'wb') as out, target.open('rb') as source:
                        while chunk := source.read(1024**2):
                            out.write(chunk)
                    Path(name).replace(self.cache_dir / (self.package_name + '.apk'))
                finally:
                    Path(name).unlink(missing_ok=True)
            self._version_code = str(version)
            return servers

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
                if self._version_code is not None and int(version) < int(self._version_code):
                    raise ApkVerificationError('APK version rollback rejected')
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
                    return await self._read_verified(apk, expected_version=version, save=True)
        except asyncio.CancelledError:
            raise
        except ApkVerificationError:
            raise
        except Exception:
            raise TurnConfigurationError('Could not obtain TURN configuration from the official RuStore app') from None
