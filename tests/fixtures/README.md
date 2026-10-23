These public Android apksig regression fixtures come from the Android Open
Source Project, licensed under Apache-2.0 (see LICENSE-AOSP.txt).
They are test applications, not Rubetek APKs and contain no Rubetek credentials.

Source: https://android.googlesource.com/platform/tools/apksig/+/refs/heads/main/src/test/resources/com/android/apksig/

* aosp-valid.apk: v2-only-with-rsa-pkcs1-sha256-2048.apk,
  SHA256 b11e85df088c3474b2d0f266f1a4863744162fc44b4b53a87d3432f03dce25c6,
  Git blob 51ed3ff384ad6738760c635905d4aff6f2465b51.
* aosp-invalid.apk: v2-only-with-ecdsa-sha256-p256-sig-does-not-verify.apk,
  SHA256 9177864a130ab30a067e22bc63fb71d8f84fee41e0c546b3a14b99cb205cd8c7,
  Git blob 0339d397ef0fb42da818ce85f2cccc8b09ad8f73.

The fixtures exercise Google's real verifier, including cryptographic failure;
publisher pins used in these tests are deliberately different from the Rubetek pin.
