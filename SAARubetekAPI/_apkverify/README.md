# Android APK verifier bridge

`ApkVerifierBridge.java` is MIT-licensed project code. Its packaged `.class` is
compiled reproducibly with JDK 21 and `--release 11 -g:none`. APK classes are never
loaded. Google apksig performs Android signature/content verification; Python
enforces the reviewed publisher certificate and signed package/version.

The upstream Apache-2.0 apksig JAR is fetched from official Google Maven at the
exact URL and SHA256 pinned in `../apk_signature.py`; it is not vendored here.
Source/license: https://android.googlesource.com/platform/tools/apksig/

From repository root, with the checksum-verified JAR in `/tmp/apksig-8.13.0.jar`:

```sh
javac --release 11 -g:none -cp /tmp/apksig-8.13.0.jar SAARubetekAPI/_apkverify/ApkVerifierBridge.java
git diff --exit-code -- SAARubetekAPI/_apkverify/ApkVerifierBridge.class
```

CI rebuilds the bridge and checks equality. Publisher key rotation requires a
reviewed pin update; neither store metadata nor an APK can introduce a trusted key.
