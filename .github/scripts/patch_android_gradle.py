# -*- coding: utf-8 -*-
"""给 Capacitor 生成的 android 工程注入 release 签名（CI 用，幂等）：
① keystore：环境变量 ML_KS_B64（base64 的 jks）解码到 android/ml.keystore；
   没有就 keytool 现场生成（首次构建；记得把 artifact 里的 jks 存成 secret 固定签名）。
② build.gradle：android{} 注入 signingConfigs.mlrelease，release buildType 指向它。
密码走环境变量 ML_KS_PASS（无则用默认）。输出全 ASCII（runner 控制台编码坑）。"""
import base64
import io
import os
import re
import subprocess
import sys

DEFAULT_PASS = "modelookup-mobile-2026"
ALIAS = "modelookup"


def ensure_keystore(adir, store_pass):
    ks = os.path.join(adir, "ml.keystore")
    b64 = (os.environ.get("ML_KS_B64") or "").strip()
    if b64:
        with open(ks, "wb") as f:
            f.write(base64.b64decode(b64))
        print("keystore: from ML_KS_B64 secret (%d bytes)" % os.path.getsize(ks))
        return
    if os.path.isfile(ks):
        print("keystore: already exists, reuse")
        return
    print("keystore: no secret found, generating a NEW one (save it later!)")
    subprocess.run([
        "keytool", "-genkeypair", "-v", "-keystore", ks, "-alias", ALIAS,
        "-keyalg", "RSA", "-keysize", "2048", "-validity", "10950",
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
    io.open(gp, "w", encoding="utf-8", newline="\n").write(src)
    print("build.gradle patched (signingConfig -> mlrelease, replaced=%d)" % n1)


def main():
    adir = sys.argv[1] if len(sys.argv) > 1 else "android"
    store_pass = os.environ.get("ML_KS_PASS") or DEFAULT_PASS
    ensure_keystore(adir, store_pass)
    patch_gradle(adir, store_pass)


if __name__ == "__main__":
    main()
