# -*- coding: utf-8 -*-
"""给 Capacitor 生成的 android 工程注入 release 签名（CI 用，幂等）：
① keystore：优先用仓库 mobile/ml.keystore（PKCS12，2026-10-12 起随仓库走，签名永久固定）；
   其次环境变量 ML_KS_B64（base64）解码；都没有才 keytool 现场生成（临时签名，仅调试）。
② build.gradle：android{} 注入 signingConfigs.mlrelease（PKCS12），release buildType 指向它；
   versionName/versionCode 写入真实版本号（argv[2]，如 2.7.32 → versionCode 20732）。
密码走环境变量 ML_KS_PASS（无则用默认）。输出全 ASCII（runner 控制台编码坑）。"""
import base64
import io
import os
import re
import shutil
import subprocess
import sys

DEFAULT_PASS = "modelookup-mobile-2026"
ALIAS = "modelookup"


def ensure_keystore(adir, store_pass):
    ks = os.path.join(adir, "ml.keystore")
    # 1) 仓库里的固定签名（mobile/ml.keystore，相对 mobile/app/android 即 ../../ml.keystore）
    repo_ks = os.path.join(adir, "..", "..", "ml.keystore")
    if os.path.isfile(repo_ks):
        shutil.copyfile(repo_ks, ks)
        print("keystore: from repo mobile/ml.keystore (%d bytes, PKCS12, stable)" % os.path.getsize(ks))
        return
    b64 = (os.environ.get("ML_KS_B64") or "").strip()
    if b64:
        with open(ks, "wb") as f:
            f.write(base64.b64decode(b64))
        print("keystore: from ML_KS_B64 secret (%d bytes)" % os.path.getsize(ks))
        return
    if os.path.isfile(ks):
        print("keystore: already exists, reuse")
        return
    print("keystore: no repo/secret found, generating a NEW one (temporary!)")
    subprocess.run([
        "keytool", "-genkeypair", "-v", "-keystore", ks, "-alias", ALIAS,
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
        "-storetype", "PKCS12",
        "-storepass", store_pass, "-keypass", store_pass,
        "-dname", "CN=ModelLookup,O=LongYang,C=CN",
    ], check=True)


def patch_gradle(adir, store_pass):
    gp = os.path.join(adir, "app", "build.gradle")
    src = io.open(gp, encoding="utf-8").read()
    if "mlrelease" in src:
        print("build.gradle already patched, skip")
        return
    inject = (
        "    signingConfigs {\n"
        "        mlrelease {\n"
        '            storeFile file("../ml.keystore")\n'
        '            storeType "PKCS12"\n'
        '            storePassword System.getenv("ML_KS_PASS") ?: "%s"\n'
        '            keyAlias "%s"\n'
        '            keyPassword System.getenv("ML_KS_PASS") ?: "%s"\n'
        "        }\n"
        "    }\n" % (store_pass, ALIAS, store_pass)
    )
    m = re.search(r"android\s*\{", src)
    if not m:
        sys.exit("android{} block not found in build.gradle")
    src = src[:m.end()] + "\n" + inject + src[m.end():]

    # release buildType 指向 mlrelease：模板有 debug 指向就替换；没有就在 release { 后插入。
    # ⚠ 两个分支都必须写回 src（2026-10-11 实锤：n1==0 时 new 没赋回 src → 产物 unsigned）。
    new, n1 = re.subn(r"signingConfig\s+signingConfigs\.debug",
                      "signingConfig signingConfigs.mlrelease", src)
    src = new
    if n1 == 0:
        new, n2 = re.subn(r"(buildTypes\s*\{\s*release\s*\{)",
                          r"\1\n            signingConfig signingConfigs.mlrelease", src)
        src = new
        if n2 == 0:
            sys.exit("release buildType not found in build.gradle — Capacitor template changed?")

    # 真实版本号（argv[2]，如 2.7.32）：versionCode = 主*10000+次*100+修（单调递增）
    num = (sys.argv[2] if len(sys.argv) > 2 else "").strip()
    if num:
        try:
            parts = [int(x) for x in num.split(".")[:3]]
            while len(parts) < 3:
                parts.append(0)
            code = parts[0] * 10000 + parts[1] * 100 + parts[2]
            src, nv = re.subn(r'versionName\s+"[^"]*"', 'versionName "%s"' % num, src)
            src, nc = re.subn(r"versionCode\s+\d+", "versionCode %d" % code, src)
            print("version: %s -> versionName %s / versionCode %d (vName=%d vCode=%d)"
                  % (num, num, code, nv, nc))
        except ValueError:
            print("version num not numeric, skip version inject")
    io.open(gp, "w", encoding="utf-8", newline="\n").write(src)
    print("build.gradle patched (signingConfig -> mlrelease, replaced=%d)" % n1)


def patch_styles(adir):
    """状态栏染成商务蓝 #2e6be6（2026-10-12 用户截图反馈顶部白条违和）。
    幂等：已含 statusBarColor 就跳过。"""
    sp = os.path.join(adir, "app", "src", "main", "res", "values", "styles.xml")
    if not os.path.isfile(sp):
        print("styles.xml not found, skip statusbar patch")
        return
    src = io.open(sp, encoding="utf-8").read()
    if "statusBarColor" in src:
        print("styles.xml already patched, skip")
        return
    new, n = re.subn(r"(<style\s+[^>]*AppTheme\.NoActionBar[^>]*>)",
                     r'\1\n        <item name="android:statusBarColor">#2E6BE6</item>', src)
    if n == 0:
        print("AppTheme.NoActionBar style not found, skip statusbar patch")
        return
    io.open(sp, "w", encoding="utf-8", newline="\n").write(new)
    print("styles.xml patched (statusBarColor=#2E6BE6)")


def patch_manifest(adir):
    """真软件化（2026-10-13）：应用内下载 APK 并拉起安装需要 REQUEST_INSTALL_PACKAGES。幂等。"""
    mf = os.path.join(adir, "app", "src", "main", "AndroidManifest.xml")
    if not os.path.isfile(mf):
        print("AndroidManifest.xml not found, skip install-permission patch")
        return
    src = io.open(mf, encoding="utf-8").read()
    if "REQUEST_INSTALL_PACKAGES" in src:
        print("manifest already patched, skip")
        return
    new, n = re.subn(r"(<uses-permission[^>]*/>)",
                     r'\1\n    <uses-permission android:name="android.permission.REQUEST_INSTALL_PACKAGES" />',
                     src, count=1)
    if n == 0:
        print("no uses-permission anchor in manifest, skip")
        return
    io.open(mf, "w", encoding="utf-8", newline="\n").write(new)
    print("manifest patched (REQUEST_INSTALL_PACKAGES)")


DL_SNIPPET = """
        // 2026-10-13 真软件化：应用内更新——WebView 下载监听，APK 走 DownloadManager
        // 下载完自动拉起系统安装器（不用再跳浏览器手动点开）
        bridge.getWebView().setDownloadListener(new android.webkit.DownloadListener() {
            @Override
            public void onDownloadStart(String url, String ua, String cd, String mime, long len) {
                try {
                    android.app.DownloadManager.Request req = new android.app.DownloadManager.Request(android.net.Uri.parse(url));
                    req.setMimeType("application/vnd.android.package-archive");
                    req.setNotificationVisibility(android.app.DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
                    req.setDestinationInExternalPublicDir(android.os.Environment.DIRECTORY_DOWNLOADS, "ModelLookup-update.apk");
                    final android.app.DownloadManager dm = (android.app.DownloadManager) getSystemService(DOWNLOAD_SERVICE);
                    final long id = dm.enqueue(req);
                    android.widget.Toast.makeText(MainActivity.this, "Download started, please wait", android.widget.Toast.LENGTH_LONG).show();
                    BroadcastReceiver done = new BroadcastReceiver() {
                        @Override
                        public void onReceive(android.content.Context ctx, android.content.Intent i) {
                            long rid = i.getLongExtra(android.app.DownloadManager.EXTRA_DOWNLOAD_ID, -1);
                            if (rid != id) return;
                            android.net.Uri uri = dm.getUriForDownloadedFile(id);
                            if (uri == null) return;
                            android.content.Intent open = new android.content.Intent(android.content.Intent.ACTION_VIEW);
                            open.setDataAndType(uri, "application/vnd.android.package-archive");
                            open.addFlags(android.content.Intent.FLAG_GRANT_READ_URI_PERMISSION | android.content.Intent.FLAG_ACTIVITY_NEW_TASK);
                            try { startActivity(open); } catch (Exception ex) { }
                        }
                    };
                    android.content.IntentFilter f = new android.content.IntentFilter(android.app.DownloadManager.ACTION_DOWNLOAD_COMPLETE);
                    if (android.os.Build.VERSION.SDK_INT >= 33) {
                        registerReceiver(done, f, android.content.Context.RECEIVER_EXPORTED);
                    } else {
                        registerReceiver(done, f);
                    }
                } catch (Exception e) {
                    // 兜底：交给系统浏览器下载
                    try {
                        startActivity(new android.content.Intent(android.content.Intent.ACTION_VIEW, android.net.Uri.parse(url)));
                    } catch (Exception ex) { }
                }
            }
        });
"""


def patch_downloader(adir):
    """MainActivity.onCreate 注入 DownloadListener（应用内下载→拉起安装）。幂等。"""
    hits = []
    root = os.path.join(adir, "app", "src", "main", "java")
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if fn == "MainActivity.java":
                hits.append(os.path.join(dirpath, fn))
    if not hits:
        print("MainActivity.java not found, skip downloader patch")
        return
    mp = hits[0]
    src = io.open(mp, encoding="utf-8").read()
    if "setDownloadListener" in src:
        print("MainActivity already patched, skip")
        return
    anchor = "super.onCreate(savedInstanceState);"
    if anchor not in src:
        print("onCreate anchor not found in MainActivity, skip downloader patch")
        return
    src = src.replace(anchor, anchor + "\n" + DL_SNIPPET, 1)
    io.open(mp, "w", encoding="utf-8", newline="\n").write(src)
    print("MainActivity patched (in-app APK download listener)")


def main():
    adir = sys.argv[1] if len(sys.argv) > 1 else "android"
    store_pass = os.environ.get("ML_KS_PASS") or DEFAULT_PASS
    ensure_keystore(adir, store_pass)
    patch_gradle(adir, store_pass)
    patch_styles(adir)
    patch_manifest(adir)
    patch_downloader(adir)


if __name__ == "__main__":
    main()
