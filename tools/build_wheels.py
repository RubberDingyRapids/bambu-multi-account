"""Pack the plugin into one Orca Cloud wheel per platform.

    python tools/build_wheels.py <version> <libs dir> <out dir>

<libs dir> holds win_x64/, linux_x64/ and macos_arm64/, each with one folder
per library series (02.08.04/, 02.08.01/) holding the patched networking
library, plus build.json (obn_version, upstream, built).
Each wheel gets the plugin code and only its own platform's library. A
platform whose library is missing is skipped, so a failed build on one OS
doesn't hold the others back.
"""
import base64
import hashlib
import os
import re
import sys
import zipfile

PKG = "bambu_multi_account"
PLUGIN = "bambu_multi_account_any.py"
PLATFORMS = {
    "win_x64": ("bambu_networking.dll", "win_x86_64"),
    "linux_x64": ("libbambu_networking.so", "linux_x86_64"),
    "macos_arm64": ("libbambu_networking.dylib", "macosx_arm64"),
}


def set_version(code, version):
    code, n = re.subn(r'^# version = ".*"$', '# version = "%s"' % version, code, count=1, flags=re.M)
    assert n == 1, "no version line in the plugin header"
    code, n = re.subn(r'^PLUGIN_VERSION = ".*"$', 'PLUGIN_VERSION = "%s"' % version, code, count=1, flags=re.M)
    assert n == 1, "no PLUGIN_VERSION"
    return code


def wheel(path, version, files):
    dist = "%s-%s.dist-info" % (PKG, version)
    files = dict(files)
    files[dist + "/METADATA"] = ("Metadata-Version: 2.1\nName: %s\nVersion: %s\n"
                                 "Summary: Several Bambu accounts at once in OrcaSlicer\n"
                                 "Requires-Python: >=3.12\n" % (PKG, version)).encode()
    files[dist + "/WHEEL"] = b"Wheel-Version: 1.0\nGenerator: bambu-multi-account\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    files[dist + "/top_level.txt"] = (PKG + "\n").encode()
    record = []
    for name, data in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
        record.append("%s,sha256=%s,%d" % (name, digest, len(data)))
    record.append(dist + "/RECORD,,")
    files[dist + "/RECORD"] = ("\n".join(record) + "\n").encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    print("built %s (%d bytes)" % (path, os.path.getsize(path)))


def main(version, libs, out):
    with open(PLUGIN, "r", encoding="utf-8") as fh:
        code = set_version(fh.read(), version).encode("utf-8")
    with open(os.path.join(libs, "build.json"), "rb") as fh:
        build_json = fh.read()
    os.makedirs(out, exist_ok=True)
    made = 0
    for plat, (lib, tag) in PLATFORMS.items():
        files = {
            PKG + "/__init__.py": code,
            PKG + "/" + PLUGIN: code,
            PKG + "/bin/build.json": build_json,
        }
        series = sorted(d for d in os.listdir(os.path.join(libs, plat))
                        if os.path.isfile(os.path.join(libs, plat, d, lib))) if os.path.isdir(os.path.join(libs, plat)) else []
        if not series:
            print("skipping %s: no %s" % (plat, lib))
            continue
        for abi in series:
            with open(os.path.join(libs, plat, abi, lib), "rb") as fh:
                files["%s/bin/%s/%s/%s" % (PKG, plat, abi, lib)] = fh.read()
        print("%s: series %s" % (plat, ", ".join(series)))
        wheel(os.path.join(out, "%s_%s.whl" % (PKG, tag)), version, files)
        made += 1
    if not made:
        sys.exit("no platform libraries found in " + libs)


if __name__ == "__main__":
    main(*sys.argv[1:4])
