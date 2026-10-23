"""Runtime discovery must not install or require a bundled JRE for manual ICE."""
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from SAARubetekAPI.apk_signature import java_executable, verify_apk, ApkVerificationError


class RuntimeDiscovery(unittest.TestCase):
    def test_java_home_precedes_path(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'bin' / ('java.exe' if os.name == 'nt' else 'java')
            executable.parent.mkdir()
            executable.touch()
            with patch.dict(os.environ, {'JAVA_HOME': directory}), \
                 patch('shutil.which', return_value='/other/java'):
                self.assertEqual(java_executable(), str(executable.resolve()))

    def test_system_java_does_not_need_optional_package(self):
        with patch.dict(os.environ, {'JAVA_HOME': ''}), \
             patch('shutil.which', return_value='/system/java'), \
             patch.dict(sys.modules, {'jdk4py': None}):
            self.assertEqual(java_executable(), '/system/java')

    def test_optional_runtime_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / 'java'
            executable.touch()
            with patch.dict(os.environ, {'JAVA_HOME': ''}), \
                 patch('shutil.which', return_value=None), \
                 patch.dict(sys.modules, {'jdk4py': SimpleNamespace(JAVA=executable)}):
                self.assertEqual(java_executable(), str(executable))


class MissingRuntime(unittest.IsolatedAsyncioTestCase):
    async def test_missing_runtime_fails_before_verifier_download(self):
        with patch.dict(os.environ, {'JAVA_HOME': ''}), \
             patch('shutil.which', return_value=None), \
             patch.dict(sys.modules, {'jdk4py': None}), \
             patch('SAARubetekAPI.apk_signature.verifier_jar', new_callable=AsyncMock) as download:
            with self.assertRaisesRegex(ApkVerificationError, 'Java 11'):
                await verify_apk('unused.apk', package_name='com.fsk.home')
            download.assert_not_awaited()
