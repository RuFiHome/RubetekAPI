"""Real Android apksig checks against independent, publicly signed AOSP fixtures."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from SAARubetekAPI.apk_signature import verify_apk, ApkVerificationError, verifier_jar

FIXTURES = Path(__file__).with_name('fixtures')
PIN = 'fb5dbd3c669af9fc236c6991e6387b7f11ff0590997f22d0f5c74ff40e04fca8'
PACKAGE = 'android.appsecurity.cts.tinyapp'


class AndroidVerification(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.cache = Path(cls.directory.name)
        # One pinned Google Maven download for the entire class, no Rubetek account.
        asyncio.run(verifier_jar(cls.cache))

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    async def check(self, path=None, **overrides):
        args = dict(package_name=PACKAGE, version_code=10, trusted_certificates={PIN}, cache_dir=self.cache)
        args.update(overrides)
        return await verify_apk(path or FIXTURES / 'aosp-valid.apk', **args)

    async def test_real_signature_and_signed_manifest(self):
        result = await self.check()
        self.assertEqual(result, {'package': PACKAGE, 'version': 10, 'certificates': [PIN]})

    async def test_valid_signature_with_unknown_publisher_rejected(self):
        with self.assertRaises(ApkVerificationError):
            await self.check(trusted_certificates={'0' * 64})

    async def test_signature_corrupted_apk_rejected(self):
        with self.assertRaises(ApkVerificationError):
            await self.check(FIXTURES / 'aosp-invalid.apk')

    async def test_tampered_content_rejected_even_with_original_certificate(self):
        data = bytearray((FIXTURES / 'aosp-valid.apk').read_bytes())
        data[100] ^= 1
        path = self.cache / 'tampered.apk'
        path.write_bytes(data)
        with self.assertRaises(ApkVerificationError):
            await self.check(path)

    async def test_signed_package_and_version_must_match(self):
        with self.assertRaises(ApkVerificationError):
            await self.check(package_name='com.attacker.substitute')
        with self.assertRaises(ApkVerificationError):
            await self.check(version_code=11)

    async def test_missing_verifier_is_not_an_unsigned_fallback(self):
        with patch('SAARubetekAPI.apk_signature.verifier_jar', side_effect=ApkVerificationError('unavailable')):
            with self.assertRaises(ApkVerificationError):
                await self.check()

    async def test_modified_cached_verifier_is_never_executed(self):
        from SAARubetekAPI.apk_signature import APKSIG_VERSION
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            (cache / ('apksig-' + APKSIG_VERSION + '.jar')).write_bytes(b'modified cached executable')
            with patch('aiohttp.ClientSession', side_effect=RuntimeError('network unavailable')), \
                 patch('asyncio.create_subprocess_exec') as execute:
                with self.assertRaises(ApkVerificationError):
                    await self.check(cache_dir=cache)
                execute.assert_not_called()
