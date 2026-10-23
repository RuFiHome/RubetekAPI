"""APK integrity and publisher pinning via Google's official apksig verifier.

Only our bridge and the checksum-pinned Google library execute, never APK code.
No certificate from RuStore metadata or from a new APK is trusted on first use.
"""
import asyncio
import hashlib
from importlib.resources import files
import os
from pathlib import Path
import re
import shutil
import tempfile

APKSIG_VERSION = '8.13.0'
APKSIG_SHA256 = 'c070ed1394629d74641aa0906f60b2ffa1ee77e6366a1f93437f59717b1aeb89'
APKSIG_URL = ('https://dl.google.com/dl/android/maven2/com/android/tools/build/apksig/'
              + APKSIG_VERSION + '/apksig-' + APKSIG_VERSION + '.jar')
# Reviewed com.fsk.home publisher certificate, verified using Android apksig.
# Rotation requires a reviewed code update; never learn a new pin from metadata.
FSK_CERTIFICATES = frozenset({'99b6794304ac22593c195c6024b7a192e7e20c629daeb6d7a129317747cda09d'})


class ApkVerificationError(RuntimeError):
    """APK integrity, package, version, or pinned publisher could not be verified."""


def default_cache_dir():
    base = Path(os.environ['LOCALAPPDATA']) if os.name == 'nt' and os.environ.get('LOCALAPPDATA') else Path.home() / '.cache'
    return base / 'saarubetek-api'


def java_executable():
    """Reuse installed Java; a bundled runtime is an explicit optional extra."""
    home = os.environ.get('JAVA_HOME')
    if home:
        candidate = Path(home) / 'bin' / ('java.exe' if os.name == 'nt' else 'java')
        if candidate.is_file():
            return str(candidate.resolve())
    installed = shutil.which('java')
    if installed:
        return installed
    try:
        from jdk4py import JAVA
        if Path(JAVA).is_file():
            return str(JAVA)
    except ImportError:
        pass
    raise ApkVerificationError(
        'Automatic TURN verification requires Java 11+ in JAVA_HOME/PATH '
        'or the optional saarubetek-api[apk-runtime] extra')


async def verifier_jar(cache_dir):
    """Fetch only the pinned official verifier artifact, recheck disk cache every use."""
    import aiohttp
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache_dir / ('apksig-' + APKSIG_VERSION + '.jar')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == APKSIG_SHA256:
        return target
    try:
        async with aiohttp.ClientSession(trust_env=True, timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.get(APKSIG_URL, allow_redirects=False) as response:
                if response.status != 200:
                    raise ValueError('Verifier unavailable')
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > 2 * 1024**2:
                        raise ValueError('Oversized verifier')
                    chunks.append(chunk)
                data = b''.join(chunks)
        if len(data) > 2 * 1024**2 or hashlib.sha256(data).hexdigest() != APKSIG_SHA256:
            raise ValueError('Verifier checksum mismatch')
        fd, name = tempfile.mkstemp(prefix='.apksig-', dir=cache_dir)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            Path(name).replace(target)
        finally:
            Path(name).unlink(missing_ok=True)
        return target
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ApkVerificationError('Trusted Android APK verifier unavailable') from None


async def verify_apk(path, *, package_name, version_code=None, trusted_certificates=None, cache_dir=None):
    """Check v2/v3 integrity, pinned signer set and signed manifest identity/version."""
    if trusted_certificates is None:
        if package_name != 'com.fsk.home':
            raise ApkVerificationError('No reviewed publisher certificate for this package')
        trusted_certificates = FSK_CERTIFICATES
    pins = frozenset(str(p).lower() for p in trusted_certificates)
    if not pins or any(not re.fullmatch('[0-9a-f]{64}', p) for p in pins):
        raise ApkVerificationError('Invalid publisher certificate pins')
    java = java_executable()
    jar = await verifier_jar(cache_dir or default_cache_dir())
    process = None
    try:
        with tempfile.TemporaryDirectory(prefix='rubetek-apk-verify-') as directory:
            bridge = Path(directory) / 'ApkVerifierBridge.class'
            bridge.write_bytes(files('SAARubetekAPI').joinpath('_apkverify/ApkVerifierBridge.class').read_bytes())
            # Prevent inherited Java agent/classpath options from changing the verifier.
            env = {k: v for k, v in os.environ.items() if k not in
                   ('JAVA_TOOL_OPTIONS', 'JDK_JAVA_OPTIONS', '_JAVA_OPTIONS', 'CLASSPATH')}
            process = await asyncio.create_subprocess_exec(
                java, '-Xmx256m', '-cp', os.pathsep.join((str(jar.resolve()), directory)),
                'ApkVerifierBridge', str(Path(path).resolve()), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            output, _ = await asyncio.wait_for(process.communicate(), 90)
            if process.returncode or len(output) > 16384:
                raise ValueError('Android verifier rejected APK')
            records = [line.split('\t', 1) for line in output.decode('ascii').splitlines()]
            packages = [v for k, v in records if k == 'package']
            versions = [int(v) for k, v in records if k == 'version']
            signers = [v for k, v in records if k == 'cert']
            if packages != [package_name] or len(versions) != 1 or versions[0] < 0:
                raise ValueError('Signed manifest mismatch')
            if version_code is not None and versions != [int(version_code)]:
                raise ValueError('Signed version does not match advertised version')
            # Every current signer must be pinned, not merely any matching signer.
            if not signers or len(set(signers)) != len(signers) or not set(signers) <= pins:
                raise ValueError('Untrusted publisher certificate')
            return {'package': packages[0], 'version': versions[0], 'certificates': signers}
    except asyncio.CancelledError:
        raise
    except Exception:
        raise ApkVerificationError('APK signature, publisher, package or version verification failed') from None
    finally:
        if process is not None and process.returncode is None:
            process.kill()
            await process.wait()
