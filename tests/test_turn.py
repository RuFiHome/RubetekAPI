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
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.verify_patch = patch('SAARubetekAPI.turn.verify_apk', new=AsyncMock(return_value={'version': 1}))
        self.verify_patch.start()
        self.addCleanup(self.verify_patch.stop)
        self.cache_patch = patch('SAARubetekAPI.turn.default_cache_dir', return_value=Path(self.directory.name))
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    async def test_local_source_has_no_network_and_returns_independent_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / 'fixture.apk'
            apk.write_bytes(fixture_apk())
            provider = RuStoreTurnProvider(apk_path=apk, verify_signature=True)
            with patch.object(provider, '_download', new_callable=AsyncMock) as download:
                values = await provider()
                values[0]['credential'] = 'changed-by-caller'
                self.assertEqual((await provider())[0]['credential'], 'fixture-password')
                download.assert_not_awaited()

    async def test_shared_cache_lock_and_refresh(self):
        provider = RuStoreTurnProvider(refresh_seconds=1, verify_signature=True)
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
        provider = RuStoreTurnProvider(verify_signature=True)
        with patch.object(provider, '_download', new=AsyncMock(side_effect=TurnConfigurationError('unavailable'))) as download:
            with self.assertRaises(TurnConfigurationError):
                await provider()
            with self.assertRaises(TurnConfigurationError):
                await provider()
            self.assertEqual(download.await_count, 1)
            self.assertIsNone(provider._servers)

    async def test_missing_runtime_never_downloads_apk(self):
        from SAARubetekAPI.apk_signature import ApkVerificationError
        provider = RuStoreTurnProvider(verify_signature=True)
        with patch('SAARubetekAPI.turn.java_executable', side_effect=ApkVerificationError('Java required')), \
             patch.object(provider, '_download', new_callable=AsyncMock) as download:
            with self.assertRaises(ApkVerificationError):
                await provider()
            download.assert_not_awaited()

    async def test_default_extracts_without_java_or_verifier(self):
        with tempfile.TemporaryDirectory() as directory:
            apk = Path(directory) / 'fixture.apk'
            apk.write_bytes(fixture_apk())
            provider = RuStoreTurnProvider(apk_path=apk)
            self.assertFalse(provider.verify_signature)
            with patch('SAARubetekAPI.turn.java_executable', side_effect=AssertionError('Java must not run')), \
                 patch('SAARubetekAPI.turn.verify_apk', side_effect=AssertionError('Verifier must not run')):
                self.assertEqual((await provider())[0]['credential'], 'fixture-password')
            await provider.close()

    async def test_opt_out_cache_is_separate_persistent_and_checked_for_corruption(self):
        directory = Path(self.directory.name)
        provider = RuStoreTurnProvider(cache_dir=directory, verify_signature=False)
        self.assertEqual(provider.cache_dir, directory / 'unverified')
        with patch('SAARubetekAPI.turn.verify_apk', side_effect=AssertionError('Verifier must not run')):
            original = await provider._read_verified(io.BytesIO(fixture_apk()), expected_version='1', save=True)
            provider = RuStoreTurnProvider(cache_dir=directory, verify_signature=False)
            with patch.object(provider, '_download', new_callable=AsyncMock) as download:
                async def unchanged():
                    return provider._servers
                download.side_effect = unchanged
                self.assertEqual(await provider(), original)
                self.assertEqual(provider._version_code, '1')
            await provider.close()
            trusted = RuStoreTurnProvider(cache_dir=directory, verify_signature=True)
            with patch.object(trusted, '_download', side_effect=TurnConfigurationError('offline')):
                with self.assertRaises(TurnConfigurationError):
                    await trusted()
            cached = provider.cache_dir / 'com.fsk.home.apk'
            cached.write_bytes(b'corrupt cache')
            another = RuStoreTurnProvider(cache_dir=directory, verify_signature=False)
            with patch.object(another, '_download', side_effect=TurnConfigurationError('offline')):
                with self.assertRaises(TurnConfigurationError):
                    await another()
            self.assertIsNone(another._servers)

    async def test_signature_opt_out_requires_explicit_boolean_and_no_pins(self):
        for value in (None, 0, 'false'):
            with self.assertRaises(ValueError):
                RuStoreTurnProvider(verify_signature=value)
        with self.assertRaises(ValueError):
            RuStoreTurnProvider(verify_signature=False, trusted_certificates={'0' * 64})
        self.assertTrue(RuStoreTurnProvider(verify_signature=True).verify_signature)
        self.assertFalse(RuStoreTurnProvider().verify_signature)

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
            provider = RuStoreTurnProvider(verify_signature=True)
            config = await provider()
            self.assertEqual(config[0]['credential'], 'fixture-password')
            provider._valid_until = 0
            await provider()
            Session.host = 'untrusted.example.invalid'
            with self.assertRaises(TurnConfigurationError):
                await RuStoreTurnProvider(cache_dir=Path(self.directory.name) / 'untrusted', verify_signature=True)()
            Session.host = ':fixture-password@static.rustore.ru'
            with self.assertRaises(TurnConfigurationError):
                await RuStoreTurnProvider(cache_dir=Path(self.directory.name) / 'userinfo', verify_signature=True)()
        self.assertEqual(len([u for u in requests if 'fixture.apk' in u]), 1)

    async def test_periodic_check_without_new_viewer_and_clean_shutdown(self):
        provider = RuStoreTurnProvider(refresh_seconds=.02, failure_retry_seconds=.02, verify_signature=True)
        download = AsyncMock(side_effect=[[{'urls': 'turn:first.invalid'}], [{'urls': 'turn:second.invalid'}]])
        with patch.object(provider, '_download', download):
            await provider()
            deadline = asyncio.get_running_loop().time() + 1
            while download.await_count < 2:
                if asyncio.get_running_loop().time() > deadline:
                    self.fail('Periodic refresh did not run')
                await asyncio.sleep(.005)
            await provider.close()
            self.assertIsNone(provider._watch_task)
            await asyncio.sleep(.04)
            self.assertEqual(download.await_count, 2)

    async def test_bad_update_keeps_last_verified_configuration(self):
        from SAARubetekAPI.apk_signature import ApkVerificationError
        provider = RuStoreTurnProvider(verify_signature=True)
        with patch.object(provider, '_download', AsyncMock(return_value=[{'urls': 'turn:verified.invalid'}])):
            original = await provider()
        provider._valid_until = 0
        with patch.object(provider, '_download', AsyncMock(side_effect=ApkVerificationError('untrusted'))):
            self.assertEqual(await provider(), original)
            self.assertEqual(provider.last_update_error, 'ApkVerificationError')
        await provider.close()

    async def test_close_during_initial_fetch_cannot_restart_watcher(self):
        provider = RuStoreTurnProvider(verify_signature=True)
        entered, release = asyncio.Event(), asyncio.Event()
        async def download():
            entered.set()
            await release.wait()
            return [{'urls': 'turn:verified.invalid'}]
        with patch.object(provider, '_download', side_effect=download):
            pending = asyncio.create_task(provider())
            await entered.wait()
            await provider.close()
            release.set()
            with self.assertRaises(TurnConfigurationError):
                await pending
            self.assertIsNone(provider._watch_task)
            with self.assertRaises(TurnConfigurationError):
                await provider()

    async def test_signature_verification_precedes_dex_extraction(self):
        from SAARubetekAPI.apk_signature import ApkVerificationError
        provider = RuStoreTurnProvider(verify_signature=True)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'untrusted.apk'
            path.write_bytes(fixture_apk())
            with patch('SAARubetekAPI.turn.verify_apk', AsyncMock(side_effect=ApkVerificationError('bad'))), \
                 patch('SAARubetekAPI.turn.extract_turn_servers') as extract:
                with self.assertRaises(ApkVerificationError):
                    await provider._read_verified(path)
                extract.assert_not_called()

    async def test_signed_version_rollback_is_rejected(self):
        from SAARubetekAPI.apk_signature import ApkVerificationError
        provider = RuStoreTurnProvider(verify_signature=True)
        provider._version_code = '2'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'older.apk'
            path.write_bytes(fixture_apk())
            with self.assertRaises(ApkVerificationError):
                await provider._read_verified(path)


if __name__ == '__main__':
    unittest.main()
