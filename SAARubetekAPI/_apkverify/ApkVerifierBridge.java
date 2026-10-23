// SPDX-License-Identifier: MIT
// Rebuild: javac --release 11 -g:none -cp apksig-8.13.0.jar ApkVerifierBridge.java
// Only the official apksig verifier is executed; APK bytecode is never loaded.
import com.android.apksig.ApkVerifier;
import java.io.File;
import java.io.RandomAccessFile;
import java.nio.ByteBuffer;
import com.android.apksig.apk.ApkUtils;
import com.android.apksig.util.DataSources;
import java.security.MessageDigest;
import java.security.cert.X509Certificate;

public final class ApkVerifierBridge {
    public static void main(String[] args) {
        try {
            if (args.length != 1) System.exit(2);
            ApkVerifier.Result result = new ApkVerifier.Builder(new File(args[0]))
                .setMinCheckedPlatformVersion(24).build().verify();
            if (!result.isVerified() || !(result.isVerifiedUsingV2Scheme()
                    || result.isVerifiedUsingV3Scheme() || result.isVerifiedUsingV31Scheme())) {
                System.exit(2);
            }
            if (result.getSignerCertificates().isEmpty()) System.exit(2);
            try (RandomAccessFile file = new RandomAccessFile(args[0], "r")) {
                ByteBuffer manifest = ApkUtils.getAndroidManifest(DataSources.asDataSource(file.getChannel()));
                System.out.println("package\t" + ApkUtils.getPackageNameFromBinaryAndroidManifest(manifest.duplicate()));
                System.out.println("version\t" + ApkUtils.getLongVersionCodeFromBinaryAndroidManifest(manifest.duplicate()));
            }
            for (X509Certificate cert : result.getSignerCertificates()) {
                byte[] digest = MessageDigest.getInstance("SHA-256").digest(cert.getEncoded());
                StringBuilder hex = new StringBuilder();
                for (byte value : digest) hex.append(String.format("%02x", value & 255));
                System.out.println("cert\t" + hex.toString());
            }
            System.exit(0);
        } catch (Exception error) {
            // Untrusted APK metadata and verifier diagnostics must not leak to logs.
            System.exit(2);
        }
    }
}
