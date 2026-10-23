import asyncio
import io
import struct
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch
import zipfile

from SAARubetekAPI.turn import RuStoreTurnProvider, TurnConfigurationError, extract_turn_servers


def leb(value):
    result = bytearray()
    while value >= 128:
        result.append(value & 127 | 128)
        value >>= 7
    result.append(value)
    return result


def fixture_dex(bad=False):
    from SAARubetekAPI.turn import OWNER, METHODS
    strings = [OWNER, *METHODS, 'relay.example.invalid:3478', 'fixture-user', 'fixture-password']
    data = bytearray(4096 + 3 * 24)
    data[:8] = b'dex\n039\0'
    def put(offset, value):
        struct.pack_into('<I', data, offset, value)
    put(32, len(data))
    put(36, 112)
    put(40, 0x12345678)
    put(56, len(strings)); put(60, 112)
    pos = 112 + 4 * len(strings)
    put(64, 1); put(68, pos); put(pos, 0); pos += 4
    put(88, 3); put(92, pos)
    for i in range(3):
        struct.pack_into('<HHI', data, pos, 0, 0, i + 1); pos += 8
    put(96, 1); put(100, pos)
    struct.pack_into('<8I', data, pos, 0, 1, 0, 0, 0, 0, 2048, 0)
    pos += 32
    for i, value in enumerate(strings):
        put(112 + 4 * i, pos)
        encoded = leb(len(value)) + value.encode() + b'\0'
        data[pos:pos+len(encoded)] = encoded
        pos += len(encoded)
    classdata = bytearray([0, 0, 3, 0])
    for i in range(3):
        code = 4096 + i * 24
        classdata += leb(0 if i == 0 else 1) + leb(1) + leb(code)
        units = [0x1a, i + 4, 0x11] if i == 0 else [0x1b, i + 4, 0, 0x11]
        if bad:
            units[0] = 0x71
        struct.pack_into('<HHHHII', data, code, 1, 1, 0, 0, 0, len(units))
        struct.pack_into('<' + 'H' * len(units), data, code + 16, *units)
    data[2048:2048+len(classdata)] = classdata
    return bytes(data)


def fixture_apk(nested=False, bad=False):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as archive:
        archive.writestr('classes.dex', fixture_dex(bad))
    if nested:
        outer = io.BytesIO()
        with zipfile.ZipFile(outer, 'w') as archive:
            archive.writestr('base.apk', output.getvalue())
        return outer.getvalue()
    return output.getvalue()


class Extraction(unittest.TestCase):
    def test_plain_and_nested_apk_with_jumbo_constants(self):
        for nested in (False, True):
            config = extract_turn_servers(io.BytesIO(fixture_apk(nested)))
            self.assertEqual(config, [{'urls': 'turn:relay.example.invalid:3478',
                                      'username': 'fixture-user', 'credential': 'fixture-password'}])

    def test_changed_bytecode_fails_closed_without_strings(self):
        with self.assertRaises(TurnConfigurationError) as error:
            extract_turn_servers(io.BytesIO(fixture_apk(bad=True)))
        self.assertNotIn('fixture-password', str(error.exception))

    def test_invalid_and_oversized_archives(self):
        for data in (b'not an APK', fixture_apk()):
            with patch('SAARubetekAPI.turn.MAX_DEX', 1):
                with self.assertRaises(TurnConfigurationError):
                    extract_turn_servers(io.BytesIO(data))


class Provider(unittest.IsolatedAsyncioTestCase):
    async def test_local_source_has_no_network_and_returns_independent_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / 'fixture.apk'
            apk.write_bytes(fixture_apk())
            provider = RuStoreTurnProvider(apk_path=apk)
            with patch.object(provider, '_download', new_callable=AsyncMock) as download:
                values = await provider()
                values[0]['credential'] = 'changed-by-caller'
                self.assertEqual((await provider())[0]['credential'], 'fixture-password')
                download.assert_not_awaited()

    async def test_shared_cache_lock_and_refresh(self):
        provider = RuStoreTurnProvider(refresh_seconds=1)
        async def download():
            await asyncio.sleep(.01)
            return [{'urls': 'turn:example.invalid', 'credential': 'fixture-password'}]
        with patch.object(provider, '_download', new=AsyncMock(side_effect=download)) as mocked:
            await asyncio.gather(provider(), provider(), provider())
            self.assertEqual(mocked.await_count, 1)
            provider._valid_until = 0
            await provider()
            self.assertEqual(mocked.await_count, 2)

    async def test_failed_retrieval_not_cached_or_downgraded(self):
        provider = RuStoreTurnProvider()
        with patch.object(provider, '_download', new=AsyncMock(side_effect=TurnConfigurationError('unavailable'))) as download:
            with self.assertRaises(TurnConfigurationError):
                await provider()
            with self.assertRaises(TurnConfigurationError):
                await provider()
            self.assertEqual(download.await_count, 1)
            self.assertIsNone(provider._servers)

    async def test_store_download_and_origin_rejection(self):
        requests = []
        class Response:
            status = 200
            def __init__(self, value=None, content=None):
                self.value, self.raw = value, content
                self.content = self
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def raise_for_status(self):
                pass
            async def json(self):
                return self.value
            async def iter_chunked(self, size):
                yield self.raw
        class Session:
            host = 'static.rustore.ru'
            def __init__(self, **kwargs):
                self.headers = kwargs['headers']
                self.assert_no_account_headers()
            def assert_no_account_headers(self):
                assert 'Authorization' not in self.headers and 'Cookie' not in self.headers
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def get(self, url, **kwargs):
                requests.append(url)
                assert kwargs['allow_redirects'] is False
                if 'overallInfo' in url:
                    return Response({'body': {'appId': 'fixture', 'versionCode': '1', 'packageName': 'com.fsk.home', 'fileSize': 100}})
                return Response(content=fixture_apk(True))
            def post(self, url, **kwargs):
                return Response({'body': {'appId': 'fixture', 'versionCode': '1', 'apkUrl': 'https://' + self.host + '/fixture.apk'}})
        with patch('aiohttp.ClientSession', Session):
            provider = RuStoreTurnProvider()
            config = await provider()
            self.assertEqual(config[0]['credential'], 'fixture-password')
            provider._valid_until = 0
            await provider()
            Session.host = 'untrusted.example.invalid'
            with self.assertRaises(TurnConfigurationError):
                await RuStoreTurnProvider()()
            Session.host = ':fixture-password@static.rustore.ru'
            with self.assertRaises(TurnConfigurationError):
                await RuStoreTurnProvider()()
        self.assertEqual(len([u for u in requests if 'fixture.apk' in u]), 1)


if __name__ == '__main__':
    unittest.main()
