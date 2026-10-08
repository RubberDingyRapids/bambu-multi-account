# /// script
# requires-python = ">=3.12"
# dependencies = []
#
# [tool.orcaslicer.plugin]
# name = "Bambu Multi Account"
# description = "Use more than one Bambu Lab account in OrcaSlicer at once, e.g. work and home: printers from every account show up together in the Device tab and the print dialog. Needs the Open Bambu Networking plugin. On an Open Bambu Networking build without multi-account support it can still swap which account is the main one (takes effect after a restart)."
# author = "Elliott (Trevor Bolton Engineering Services)"
# version = "0.2.0"
# ///
"""Bambu Multi Account for OrcaSlicer.

A pages plugin (a tab next to Prepare / Preview / Device) that manages extra
Bambu Lab cloud accounts next to the one OrcaSlicer is logged in to.

How it works
------------
OrcaSlicer only knows one Bambu account. The Open Bambu Networking (OBN)
library that talks to Bambu's cloud keeps that session in
``<data_dir>/obn.auth.json``. This plugin logs further accounts in itself
(email code or password, Bambu's own login API), keeps them in
``<data_dir>/obn.accounts.json`` and refreshes their tokens.

OBN builds with multi-account support (they export
``obn_extra_accounts_api``) read that file, open one cloud connection per
account and add those printers to the list OrcaSlicer sees. Each printer's
commands, prints and camera then go through the account that owns it. After
any change the plugin calls ``obn_extra_accounts_reload`` so it applies at
once, without a restart.

On older OBN builds the plugin can still swap which account is the main one:
it rewrites obn.auth.json and the change applies when OrcaSlicer restarts.

The plugin never touches OrcaSlicer's own config or the main account's
token beyond that swap, and never sends tokens anywhere but Bambu's API.
"""
import ctypes
import datetime as _dt
import glob
import hashlib
import http.client
import json
import os
import re
import shutil
import ssl
import sys
import time

try:
    import orca
except ImportError:          # imported by the offline tests
    orca = None

PLUGIN_VERSION = "0.2.0"
HERE = os.path.dirname(os.path.abspath(__file__))

ACCOUNTS_FILE = "obn.accounts.json"
AUTH_FILE = "obn.auth.json"
HTTP_TIMEOUT_S = 20
# Bambu tokens last months; refresh well before the end so a long weekend
# with OrcaSlicer closed never strands an account.
REFRESH_MARGIN_S = 14 * 24 * 3600
DEFAULT_LIFETIME_S = 90 * 24 * 3600
USER_AGENT = "BBL-Slicer/v02.08.01.51 (OrcaSlicer; bambu_multi_account/%s)" % PLUGIN_VERSION


def _log(msg):
    print("[multi_account] %s" % msg, file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def data_dir():
    """OrcaSlicer's data directory, derived from where the plugin was installed.

    Local installs land in <data_dir>/orca_plugins/<name>/, cloud installs in
    <data_dir>/orca_plugins/_subscribed/<uuid>/<uuid>/. Walk up to the
    ``orca_plugins`` folder and take its parent. Falls back to %APPDATA%.
    """
    p = HERE
    for _ in range(9):
        if os.path.basename(p).lower() == "orca_plugins":
            return os.path.dirname(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/OrcaSlicer")
    return os.path.expanduser("~/.config/OrcaSlicer")


# ---------------------------------------------------------------------------
# Session records (obn.auth.json layout)
# ---------------------------------------------------------------------------

SESSION_KEYS = ("region", "account", "access_token", "refresh_token", "expires_at",
                "user_id", "user_name", "nick_name", "avatar")


def iso_in(seconds, now=None):
    now = time.time() if now is None else now
    return _dt.datetime.fromtimestamp(int(now + seconds), _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value):
    """Epoch seconds for an obn.auth.json expires_at, or None."""
    if not value:
        return None
    try:
        return _dt.datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=_dt.timezone.utc).timestamp()
    except ValueError:
        return None


def display_name(rec):
    return rec.get("nick_name") or rec.get("user_name") or rec.get("account") or rec.get("user_id") or "?"


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None


def write_json_atomic(path, data):
    """Write through a temp file and a rename, so the networking library never
    reads half a file."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")
    os.replace(tmp, path)


class AccountStore:
    """obn.accounts.json: the extra accounts. This plugin is its only writer."""

    def __init__(self, base_dir):
        self.path = os.path.join(base_dir, ACCOUNTS_FILE)
        self.auth_path = os.path.join(base_dir, AUTH_FILE)
        self.accounts = []

    def load(self):
        try:
            data = read_json(self.path) or {}
        except (OSError, ValueError) as e:
            _log("could not read %s: %r" % (ACCOUNTS_FILE, e))
            data = {}
        out = []
        for a in data.get("accounts") or []:
            if isinstance(a, dict) and a.get("user_id") and a.get("access_token"):
                a.setdefault("enabled", True)
                a.setdefault("label", "")
                out.append(a)
        self.accounts = out
        return self

    def save(self):
        write_json_atomic(self.path, {"version": 1, "accounts": self.accounts})

    def find(self, user_id):
        for a in self.accounts:
            if str(a.get("user_id")) == str(user_id):
                return a
        return None

    def upsert(self, rec):
        """Add or update by user_id. Keeps label/enabled of an existing entry
        unless the new record sets them."""
        old = self.find(rec["user_id"])
        if old is None:
            rec.setdefault("enabled", True)
            rec.setdefault("label", "")
            self.accounts.append(rec)
            return rec
        for k, v in rec.items():
            old[k] = v
        return old

    def remove(self, user_id):
        before = len(self.accounts)
        self.accounts = [a for a in self.accounts if str(a.get("user_id")) != str(user_id)]
        return len(self.accounts) != before

    def primary(self):
        """The account OrcaSlicer is logged in to, from obn.auth.json (read only)."""
        try:
            return read_json(self.auth_path) or {}
        except (OSError, ValueError) as e:
            _log("could not read %s: %r" % (AUTH_FILE, e))
            return {}

    def make_primary(self, user_id):
        """Swap an extra account with the main one. The networking library reads
        obn.auth.json once at startup, so this applies after a restart. The old
        main account stays in the list so it keeps working as an extra one."""
        rec = self.find(user_id)
        if rec is None:
            raise KeyError(user_id)
        old = self.primary()
        new_auth = {k: rec.get(k, "") for k in SESSION_KEYS}
        new_auth["region"] = new_auth["region"] or old.get("region") or "GLOBAL"
        new_auth["firmware_beta_open"] = bool(rec.get("firmware_beta_open", False))
        self.remove(user_id)
        if old.get("user_id") and old.get("access_token"):
            demoted = {k: old.get(k, "") for k in SESSION_KEYS}
            demoted["enabled"] = True
            demoted["label"] = ""
            self.upsert(demoted)
        write_json_atomic(self.auth_path, new_auth)
        self.save()


# ---------------------------------------------------------------------------
# Bambu cloud API (login, refresh, profile, printers)
# ---------------------------------------------------------------------------

class CloudError(Exception):
    def __init__(self, status, message, code=None):
        super().__init__(message)
        self.status = status
        self.code = code


def api_host(region):
    return "api.bambulab.cn" if str(region or "").lower() in ("china", "cn") else "api.bambulab.com"


class BambuApi:
    """Plain http.client, so the plugin audit hook asks once for the host
    rather than for every URL."""

    def __init__(self, region="GLOBAL"):
        self.host = api_host(region)

    def request(self, method, path, body=None, token=None):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
            "X-BBL-Client-Type": "slicer",
            "X-BBL-Client-Name": "BambuStudio",
            "X-BBL-Language": "en-US",
        }
        if token:
            headers["Authorization"] = "Bearer " + token
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        conn = http.client.HTTPSConnection(self.host, 443, timeout=HTTP_TIMEOUT_S,
                                           context=ssl.create_default_context())
        try:
            conn.request(method, path, body=payload, headers=headers)
            resp = conn.getresponse()
            text = resp.read().decode("utf-8", "replace")
            status = resp.status
        except OSError as e:
            raise CloudError(0, "Connection to %s failed: %s" % (self.host, e))
        finally:
            conn.close()
        try:
            data = json.loads(text) if text.strip() else {}
        except ValueError:
            data = {}
        if status < 200 or status >= 300:
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message") or data.get("error") or ""
            if not msg and "cloudflare" in text.lower():
                msg = "blocked by Bambu's web firewall, try again in a few minutes"
            raise CloudError(status, "%s -> HTTP %d%s" % (path.split("?")[0], status, (": " + msg) if msg else ""),
                             code=data.get("code") if isinstance(data, dict) else None)
        return data if isinstance(data, dict) else {}

    # -- login ----------------------------------------------------------------
    def send_email_code(self, email):
        self.request("POST", "/v1/user-service/user/sendemail/code", {"email": email, "type": "codeLogin"})

    def login_with_code(self, email, code):
        try:
            return self.request("POST", "/v1/user-service/user/login", {"account": email, "code": code})
        except CloudError as e:
            if e.status == 400 and e.code == 1:
                raise CloudError(400, "That code has expired. Send a new one.")
            if e.status == 400 and e.code == 2:
                raise CloudError(400, "That code is not right. Check the email and try again.")
            raise

    def login_with_password(self, email, password):
        """Returns the token reply, or {"loginType": "verifyCode"} / {"loginType": "tfa"}
        when Bambu wants more."""
        return self.request("POST", "/v1/user-service/user/login",
                            {"account": email, "password": password, "apiError": ""})

    def refresh(self, access_token, refresh_token):
        return self.request("POST", "/v1/user-service/user/refreshtoken",
                            {"refreshToken": refresh_token}, token=access_token)

    def profile(self, token):
        return self.request("GET", "/v1/user-service/my/profile", token=token)

    def printers(self, token):
        data = self.request("GET", "/v1/iot-service/api/user/bind", token=token)
        return [{"dev_id": d.get("dev_id", ""), "name": d.get("name") or d.get("dev_name") or d.get("dev_id", ""),
                 "online": bool(d.get("online", d.get("dev_online", False))),
                 "model": d.get("dev_product_name") or d.get("dev_model_name") or ""}
                for d in (data.get("devices") or []) if isinstance(d, dict)]


def session_from_login(reply, profile, region, now=None):
    """obn.auth.json-shaped record from a login/refresh reply plus /my/profile."""
    token = reply.get("accessToken") or ""
    if not token:
        raise CloudError(0, "Bambu did not return a token")
    uid = str(profile.get("uidStr") or profile.get("uid") or "")
    if not uid or uid == "0":
        raise CloudError(0, "Bambu did not return the account id")
    lifetime = int(reply.get("expiresIn") or 0) or DEFAULT_LIFETIME_S
    return {
        "region": region or "GLOBAL",
        "account": profile.get("account") or "",
        "access_token": token,
        "refresh_token": reply.get("refreshToken") or "",
        "expires_at": iso_in(lifetime, now),
        "user_id": uid,
        "user_name": profile.get("name") or "",
        "nick_name": profile.get("nickname") or profile.get("nickName") or "",
        "avatar": profile.get("avatar") or "",
    }


def needs_refresh(rec, now=None):
    now = time.time() if now is None else now
    exp = parse_iso(rec.get("expires_at"))
    return exp is None or exp - now < REFRESH_MARGIN_S


# ---------------------------------------------------------------------------
# Open Bambu Networking bridge (ctypes into the library OrcaSlicer loaded)
# ---------------------------------------------------------------------------

class ObnBridge:
    """Finds the networking library already loaded into this process and the
    multi-account exports on it. Never loads a second copy: a fresh copy would
    have no agent and no connections."""

    def __init__(self, base_dir):
        self.base_dir = base_dir
        self.lib = None
        self.api = 0
        self.error = ""
        self.loaded = False            # some networking library is loaded, with or without the exports
        self._find()

    def _candidates(self):
        names = []
        pdir = os.path.join(self.base_dir, "plugins")
        try:
            files = sorted(os.listdir(pdir))
        except OSError:
            files = []
        if sys.platform == "win32":
            names.append("bambu_networking.dll")
            names += [f for f in files if f.startswith("bambu_networking_") and f.endswith(".dll")]
        else:
            ext = ".dylib" if sys.platform == "darwin" else ".so"
            names += [os.path.join(pdir, f) for f in files
                      if f.startswith("libbambu_networking") and f.endswith(ext)]
        return names

    def _find(self):
        for name in self._candidates():
            lib = None
            try:
                if sys.platform == "win32":
                    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
                    k32.GetModuleHandleW.restype = ctypes.c_void_p
                    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
                    handle = k32.GetModuleHandleW(name)
                    if not handle:
                        continue
                    lib = ctypes.CDLL(name, handle=handle)
                else:
                    mode = getattr(os, "RTLD_NOLOAD", 0) | getattr(os, "RTLD_LAZY", 1)
                    lib = ctypes.CDLL(name, mode=mode)
            except OSError:
                continue
            self.loaded = True
            try:
                fn = lib.obn_extra_accounts_api
            except AttributeError:
                self.lib = None
                self.error = ("The Open Bambu Networking library that's loaded doesn't have multi "
                              "account in it. Install the multi account build below, or you can still "
                              "swap the main account (needs a restart).")
                return
            fn.restype = ctypes.c_int
            fn.argtypes = []
            self.api = int(fn())
            lib.obn_extra_accounts_reload.restype = ctypes.c_int
            lib.obn_extra_accounts_reload.argtypes = []
            lib.obn_extra_accounts_status.restype = ctypes.c_char_p
            lib.obn_extra_accounts_status.argtypes = []
            self.lib = lib
            self.error = ""
            return
        self.error = ("The Open Bambu Networking library is not loaded. Install it from its tab "
                      "and log in to Bambu Cloud, then restart OrcaSlicer.")

    @property
    def live(self):
        return self.lib is not None and self.api >= 1

    def reload(self):
        if not self.live:
            return None
        return int(self.lib.obn_extra_accounts_reload())

    def status(self):
        if not self.live:
            return {}
        raw = self.lib.obn_extra_accounts_status()
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            return {}


# ---------------------------------------------------------------------------
# Bundled multi-account library (wheel builds ship one per platform)
# ---------------------------------------------------------------------------

def platform_lib():
    """(bin folder in the wheel, library file name OrcaSlicer loads)."""
    if sys.platform == "win32":
        return "win_x64", "bambu_networking.dll"
    if sys.platform == "darwin":
        return "macos_arm64", "libbambu_networking.dylib"
    return "linux_x64", "libbambu_networking.so"


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v or ""))[:4]) or (0,)


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def obn_plugin_version(base_dir):
    """Version of the Open Bambu Networking plugin installed in OrcaSlicer, or ""."""
    root = os.path.join(base_dir, "orca_plugins")
    best = ""
    for meta in glob.glob(os.path.join(root, "**", "open_bambu_networking-*.dist-info", "METADATA"), recursive=True):
        try:
            with open(meta, "r", encoding="utf-8") as fh:
                m = re.search(r"^Version:\s*(\S+)", fh.read(), re.M)
        except OSError:
            continue
        if m and version_tuple(m.group(1)) > version_tuple(best):
            best = m.group(1)
    return best


class LibraryInstaller:
    """Swaps the networking library OrcaSlicer loads for the multi-account build
    shipped in this plugin's wheel, and back.

    The bundled build is persano's open-bamboo-networking at a given commit plus
    the multi-account patch; ``bin/build.json`` says which Open Bambu Networking
    plugin version that commit belongs to. It is only offered when it is at
    least as new as the installed one, so installing it never rolls back a fix.
    The library in use can't be overwritten on Windows, but it can be renamed,
    so the old file is moved aside and the new one copied in; OrcaSlicer picks
    it up on the next start. The first non-multi-account copy of each file is
    kept in ``plugins/multi_account_backup`` for "put the original back".
    """

    def __init__(self, base_dir, bundle_dir=None):
        self.base_dir = base_dir
        self.plugins_dir = os.path.join(base_dir, "plugins")
        self.backup_dir = os.path.join(self.plugins_dir, "multi_account_backup")
        self.bundle_dir = os.path.join(bundle_dir or HERE, "bin")
        self.plat, self.lib_name = platform_lib()

    def bundled(self):
        """{path, sha256, obn_version, upstream, built} or None when this install has no bundle."""
        path = os.path.join(self.bundle_dir, self.plat, self.lib_name)
        if not os.path.isfile(path):
            return None
        info = {}
        try:
            info = read_json(os.path.join(self.bundle_dir, "build.json")) or {}
        except (OSError, ValueError):
            pass
        return {"path": path, "sha256": file_sha256(path), "obn_version": info.get("obn_version", ""),
                "upstream": info.get("upstream", ""), "built": info.get("built", "")}

    def targets(self):
        """Every copy of the networking library in plugins/ (OBN writes a versioned twin on Windows)."""
        stem, ext = os.path.splitext(self.lib_name)
        try:
            files = os.listdir(self.plugins_dir)
        except OSError:
            return []
        out = []
        for f in files:
            if f == self.lib_name or (f.startswith(stem + "_") and f.endswith(ext)):
                out.append(os.path.join(self.plugins_dir, f))
        return sorted(out)

    def state(self, bridge_loaded, bridge_live):
        b = self.bundled()
        obn = obn_plugin_version(self.base_dir)
        targets = self.targets()
        installed = bool(b) and bool(targets) and all(file_sha256(t) == b["sha256"] for t in targets)
        has_backup = os.path.isdir(self.backup_dir) and bool(os.listdir(self.backup_dir))
        st = {"bundle": bool(b), "bundle_obn": (b or {}).get("obn_version", ""), "bundle_upstream": (b or {}).get("upstream", ""),
              "obn_version": obn, "installed": installed, "can_undo": has_backup and installed}
        if bridge_live:
            st["state"] = "live"
        elif not targets and not bridge_loaded:
            st["state"] = "no_obn"
        elif installed:
            st["state"] = "restart"
        elif not b:
            st["state"] = "no_bundle"
        elif obn and version_tuple(b["obn_version"]) < version_tuple(obn):
            st["state"] = "outdated"
        else:
            st["state"] = "can_install"
        return st

    def _place(self, src, dst):
        """Copy src over dst, moving a locked (loaded) dst aside first."""
        try:
            shutil.copyfile(src, dst)
            return
        except PermissionError:
            pass
        aside = "%s.old-%d" % (dst, int(time.time()))
        os.replace(dst, aside)
        shutil.copyfile(src, dst)

    def cleanup(self):
        """Delete files moved aside by an earlier swap; they unlock once OrcaSlicer restarts."""
        stem, ext = os.path.splitext(self.lib_name)
        for old in glob.glob(os.path.join(self.plugins_dir, stem + "*" + ext + ".old-*")):
            try:
                os.remove(old)
            except OSError:
                pass

    def install(self):
        b = self.bundled()
        if not b:
            raise RuntimeError("This copy of the plugin has no multi account library in it.")
        targets = self.targets()
        if not targets:
            raise RuntimeError("No Open Bambu Networking library found. Install it from its own tab first.")
        os.makedirs(self.backup_dir, exist_ok=True)
        for t in targets:
            if file_sha256(t) == b["sha256"]:
                continue
            shutil.copyfile(t, os.path.join(self.backup_dir, os.path.basename(t)))
            self._place(b["path"], t)
        return len(targets)

    def undo(self):
        try:
            names = sorted(os.listdir(self.backup_dir))
        except OSError:
            names = []
        if not names:
            raise RuntimeError("No backup to put back.")
        for n in names:
            self._place(os.path.join(self.backup_dir, n), os.path.join(self.plugins_dir, n))
        shutil.rmtree(self.backup_dir, ignore_errors=True)
        return len(names)


# ---------------------------------------------------------------------------
# Core (independent of the orca module so the tests can drive it)
# ---------------------------------------------------------------------------

class MultiAccountCore:
    def __init__(self, base_dir=None, api_factory=None, bridge=None, installer=None):
        self.base_dir = base_dir or data_dir()
        self.store = AccountStore(self.base_dir).load()
        self.api_factory = api_factory or BambuApi
        self.bridge = bridge if bridge is not None else ObnBridge(self.base_dir)
        self.installer = installer if installer is not None else LibraryInstaller(self.base_dir)
        self.pending_login = None     # {"email": ...} while waiting for a code
        self.printers = {}            # user_id -> [printer] from the last fetch
        self.notice = ""
        self.restart_needed = False

    def _region(self):
        return self.store.primary().get("region") or "GLOBAL"

    def _api(self):
        return self.api_factory(self._region())

    def _apply(self):
        """Hand the current file to the networking library."""
        n = self.bridge.reload()
        if n is not None and n < 0:
            _log("reload returned %d" % n)
        return n

    # -- token upkeep -------------------------------------------------------------
    def refresh_tokens(self, force=False, now=None):
        """Refresh every extra account close to expiry. Returns names that failed."""
        failed, changed = [], False
        for rec in self.store.accounts:
            if not force and not needs_refresh(rec, now):
                continue
            if not rec.get("refresh_token"):
                failed.append(display_name(rec))
                continue
            try:
                reply = self._api().refresh(rec.get("access_token"), rec.get("refresh_token"))
                if not reply.get("accessToken"):
                    raise CloudError(0, "no token in refresh reply")
                rec["access_token"] = reply["accessToken"]
                if reply.get("refreshToken"):
                    rec["refresh_token"] = reply["refreshToken"]
                rec["expires_at"] = iso_in(int(reply.get("expiresIn") or 0) or DEFAULT_LIFETIME_S, now)
                rec.pop("error", None)
                changed = True
            except CloudError as e:
                rec["error"] = "Token refresh failed: %s. Log this account in again." % e
                failed.append(display_name(rec))
                changed = True
                _log("refresh %s: %s" % (rec.get("user_id"), e))
        if changed:
            self.store.save()
        return failed

    # -- login --------------------------------------------------------------------
    def _finish_login(self, reply, label=""):
        api = self._api()
        prof = api.profile(reply.get("accessToken"))
        rec = session_from_login(reply, prof, self._region())
        if rec["user_id"] == str(self.store.primary().get("user_id") or ""):
            raise CloudError(0, "%s is already the account OrcaSlicer is logged in to." % display_name(rec))
        if label:
            rec["label"] = label
        rec["enabled"] = True
        rec.pop("error", None)
        self.store.upsert(rec)
        self.store.save()
        self.pending_login = None
        self._apply()
        self.load_printers(rec["user_id"])
        return "Added %s" % display_name(rec)

    def send_code(self, email, label=""):
        email = (email or "").strip()
        if "@" not in email:
            raise CloudError(0, "Enter the account's email address.")
        self._api().send_email_code(email)
        self.pending_login = {"email": email, "label": (label or "").strip()}
        return "Code sent to %s" % email

    def login_code(self, code):
        if not self.pending_login:
            raise CloudError(0, "Send a code first.")
        code = (code or "").strip()
        if not code:
            raise CloudError(0, "Enter the code from the email.")
        reply = self._api().login_with_code(self.pending_login["email"], code)
        return self._finish_login(reply, self.pending_login.get("label", ""))

    def login_password(self, email, password, label=""):
        email = (email or "").strip()
        reply = self._api().login_with_password(email, password or "")
        if reply.get("accessToken"):
            return self._finish_login(reply, (label or "").strip())
        kind = reply.get("loginType") or ""
        if kind == "verifyCode":
            return self.send_code(email, label) + ". Bambu wants an email code for this login."
        if kind == "tfa":
            raise CloudError(0, "This account uses two-factor login. Use 'Email code' instead.")
        raise CloudError(0, "Bambu did not accept the login (%s)." % (kind or "no token"))

    # -- edits ----------------------------------------------------------------------
    def set_enabled(self, user_id, enabled):
        rec = self.store.find(user_id)
        if rec is None:
            raise CloudError(0, "Unknown account")
        rec["enabled"] = bool(enabled)
        self.store.save()
        self._apply()
        return "%s %s" % (display_name(rec), "on" if enabled else "off")

    def set_label(self, user_id, label):
        rec = self.store.find(user_id)
        if rec is None:
            raise CloudError(0, "Unknown account")
        rec["label"] = (label or "").strip()[:24]
        self.store.save()
        self._apply()
        return "Label saved"

    def remove(self, user_id):
        rec = self.store.find(user_id)
        if rec is None:
            raise CloudError(0, "Unknown account")
        self.store.remove(user_id)
        self.store.save()
        self.printers.pop(str(user_id), None)
        self._apply()
        return "Removed %s" % display_name(rec)

    def make_primary(self, user_id):
        rec = self.store.find(user_id)
        if rec is None:
            raise CloudError(0, "Unknown account")
        self.store.make_primary(user_id)
        self.restart_needed = True
        self._apply()
        return ("%s is now the main account. Restart OrcaSlicer to finish the switch."
                % display_name(rec))

    def load_printers(self, user_id=None):
        ids = [str(user_id)] if user_id else [str(a["user_id"]) for a in self.store.accounts]
        for uid in ids:
            rec = self.store.find(uid)
            if rec is None:
                continue
            try:
                self.printers[uid] = self._api().printers(rec.get("access_token"))
            except CloudError as e:
                _log("printers %s: %s" % (uid, e))
                if e.status == 401:
                    rec["error"] = "Bambu refused this account's token. Log it in again."

    # -- page state -------------------------------------------------------------------
    def page_state(self):
        primary = self.store.primary()
        status = self.bridge.status() if self.bridge.live else {}
        live = {str(a.get("user_id")): a for a in (status.get("accounts") or [])}
        rows = []
        for rec in self.store.accounts:
            uid = str(rec.get("user_id"))
            st = live.get(uid, {})
            exp = parse_iso(rec.get("expires_at"))
            rows.append({
                "user_id": uid,
                "name": display_name(rec),
                "account": rec.get("account") or "",
                "label": rec.get("label") or "",
                "enabled": bool(rec.get("enabled", True)),
                "expires": rec.get("expires_at") or "",
                "expires_days": int((exp - time.time()) // 86400) if exp else None,
                "error": rec.get("error") or "",
                "connected": bool(st.get("connected")),
                "started": bool(st.get("started")),
                "printers": self.printers.get(uid, []),
                "same_as_main": uid == str(primary.get("user_id") or ""),
            })
        return {
            "version": PLUGIN_VERSION,
            "live": self.bridge.live,
            "bridge_error": self.bridge.error,
            "primary": {"name": display_name(primary) if primary.get("user_id") else "",
                        "account": primary.get("account") or "",
                        "user_id": str(primary.get("user_id") or "")},
            "accounts": rows,
            "pending_email": (self.pending_login or {}).get("email", ""),
            "restart_needed": self.restart_needed,
            "notice": self.notice,
            "library": self.library_state(),
        }

    def library_state(self):
        try:
            return self.installer.state(getattr(self.bridge, "loaded", self.bridge.live), self.bridge.live)
        except Exception as e:  # noqa: BLE001
            _log("library state: %r" % e)
            return {"state": "error", "error": str(e)}

    # -- message dispatch ---------------------------------------------------------------
    def startup(self):
        """on_load: refresh tokens that are due and hand the file to the library.
        The library also reads it when the cloud connection comes up, so this is
        only needed for tokens that were refreshed."""
        try:
            self.installer.cleanup()
        except Exception as e:  # noqa: BLE001
            _log("cleanup: %r" % e)
        if not self.store.accounts:
            return
        try:
            failed = self.refresh_tokens()
            if failed:
                self.notice = "Could not refresh: %s" % ", ".join(failed)
        except Exception as e:  # noqa: BLE001
            _log("startup refresh: %r" % e)
        self._apply()

    def handle(self, data):
        action = str(data.get("action") or "")
        reply = {"action": action, "ok": True}
        try:
            if action in ("ready", "state"):
                if action == "ready" and self.store.accounts and not self.printers:
                    self.load_printers()
            elif action == "send_code":
                reply["message"] = self.send_code(data.get("email"), data.get("label"))
            elif action == "login_code":
                reply["message"] = self.login_code(data.get("code"))
            elif action == "login_password":
                reply["message"] = self.login_password(data.get("email"), data.get("password"), data.get("label"))
            elif action == "cancel_login":
                self.pending_login = None
            elif action == "enable":
                reply["message"] = self.set_enabled(data.get("user_id"), data.get("enabled"))
            elif action == "label":
                reply["message"] = self.set_label(data.get("user_id"), data.get("label"))
            elif action == "remove":
                reply["message"] = self.remove(data.get("user_id"))
            elif action == "make_primary":
                reply["message"] = self.make_primary(data.get("user_id"))
            elif action == "install_library":
                n = self.installer.install()
                reply["message"] = "Multi account library in place (%d file%s). Restart OrcaSlicer to use it." % (n, "" if n == 1 else "s")
            elif action == "undo_library":
                n = self.installer.undo()
                reply["message"] = "Original library put back (%d file%s). Restart OrcaSlicer." % (n, "" if n == 1 else "s")
            elif action == "refresh":
                self.store.load()
                failed = self.refresh_tokens(force=bool(data.get("force")))
                self.load_printers()
                self._apply()
                reply["message"] = ("Could not refresh: %s" % ", ".join(failed)) if failed else "Accounts refreshed"
            else:
                reply["ok"] = False
                reply["error"] = "Unknown action %r" % action
        except CloudError as e:
            reply["ok"] = False
            reply["error"] = str(e)
            _log("%s: %s" % (action, e))
        except Exception as e:  # noqa: BLE001
            reply["ok"] = False
            reply["error"] = "%s: %s" % (type(e).__name__, e)
            _log("%s failed: %r" % (action, e))
        reply["state"] = self.page_state()
        return reply


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bambu Accounts</title>
<style>
:root{--bg:var(--orca-bg,#f7f7f7);--fg:var(--orca-fg,#1f1f1f);--card:var(--orca-card,#ffffff);--muted:#6b6b6b;--line:#dcdcdc;--accent:var(--orca-accent,#009688);--accent-fg:#fff;--warn:#c77700;--bad:#c62828;--ok:#2e7d32;--chip:#eeeeee}
@media (prefers-color-scheme: dark){:root{--bg:var(--orca-bg,#1e1e1e);--fg:var(--orca-fg,#e6e6e6);--card:var(--orca-card,#2a2a2a);--muted:#9a9a9a;--line:#3a3a3a;--chip:#383838}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;padding:0;background:var(--bg);color:var(--fg);font:13px/1.45 "Segoe UI",system-ui,sans-serif}
header{position:sticky;top:0;z-index:5;background:var(--bg);border-bottom:1px solid var(--line);padding:10px 16px;display:flex;gap:12px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0;font-weight:600}
main{padding:14px 16px;max-width:900px}
.grow{flex:1}
.pill{display:inline-flex;align-items:center;gap:6px;padding:2px 9px;border-radius:999px;background:var(--chip);font-size:12px;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--muted)}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.bad{background:var(--bad)}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:0 0 12px}
.card h2{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 8px;font-weight:600}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.acct{border-top:1px solid var(--line);padding:10px 0}
.acct:first-of-type{border-top:0;padding-top:2px}
.name{font-weight:600}
.muted{color:var(--muted)}
.small{font-size:12px}
.err{color:var(--bad)}
.banner{border-left:3px solid var(--warn);padding:8px 12px;background:var(--card);border-radius:6px;margin:0 0 12px}
.banner.info{border-left-color:var(--accent)}
button{font:inherit;padding:5px 11px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--accent-fg)}
button.danger{color:var(--bad)}
button:disabled{opacity:.55;cursor:default}
input[type=text],input[type=email],input[type=password]{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);min-width:0}
input.label{width:110px}
.printers{margin:6px 0 0;padding:0;list-style:none;display:flex;flex-wrap:wrap;gap:6px}
.switch{display:inline-flex;align-items:center;gap:6px;cursor:pointer;user-select:none}
.tabs{display:inline-flex;margin:0 0 12px;border:1px solid var(--line);border-radius:8px;overflow:hidden}
.tab{padding:4px 14px;cursor:pointer;user-select:none;background:var(--card);color:var(--muted)}
.tab+.tab{border-left:1px solid var(--line)}
.tab.on{background:var(--accent);color:var(--accent-fg)}
.form{display:grid;grid-template-columns:110px minmax(0,1fr);gap:8px 10px;align-items:center;max-width:520px}
.form label{color:var(--muted)}
#toast{position:fixed;left:50%;bottom:16px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:8px;opacity:0;transition:opacity .2s;pointer-events:none;max-width:90vw}
#toast.show{opacity:.92}
@media (max-width:560px){.form{grid-template-columns:1fr}}
</style></head>
<body>
<header><h1>Bambu Accounts</h1><span class="grow"></span><span id="mode" class="pill"><span class="dot"></span>…</span><button id="refresh">Refresh</button></header>
<main>
<div id="banners"></div>
<div class="card"><h2>Main account</h2><div id="primary" class="muted">…</div></div>
<div class="card"><h2>Extra accounts</h2><div id="list"></div></div>
<div class="card"><h2>Add an account</h2>
  <div class="tabs" role="tablist"><div id="t-code" class="tab on" role="tab" tabindex="0" aria-selected="true">Email code</div><div id="t-pw" class="tab" role="tab" tabindex="0" aria-selected="false">Password</div></div>
  <div id="f-code" class="form">
    <label for="c-email">Email</label><input id="c-email" type="email" placeholder="you@work.example">
    <label for="c-label">Label</label><input id="c-label" type="text" placeholder="Work (optional)" maxlength="24">
    <span></span><div class="row"><button id="send" class="primary">Send code</button></div>
    <label for="c-code" class="step2">Code</label><input id="c-code" class="step2" type="text" inputmode="numeric" autocomplete="one-time-code" placeholder="6-digit code">
    <span class="step2"></span><div class="row step2"><button id="login" class="primary">Add account</button><button id="cancel">Cancel</button></div>
  </div>
  <div id="f-pw" class="form" hidden>
    <label for="p-email">Email</label><input id="p-email" type="email">
    <label for="p-pass">Password</label><input id="p-pass" type="password" autocomplete="current-password">
    <label for="p-label">Label</label><input id="p-label" type="text" placeholder="Work (optional)" maxlength="24">
    <span></span><div class="row"><button id="pwlogin" class="primary">Add account</button></div>
  </div>
  <p class="muted small">The plugin talks to Bambu's login API directly and keeps the account's token in OrcaSlicer's data folder next to the main one (obn.accounts.json). Your password is never stored.</p>
</div>
</main>
<div id="toast"></div>
<script>
(function(){
var S={state:null,busy:false};
function $(id){return document.getElementById(id);}
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function post(msg){ try{ if(window.orca && window.orca.postMessage) window.orca.postMessage(msg); }catch(e){ console.error(e); } }
function send(msg){ S.busy=true; setBusy(true); post(msg); }
function setBusy(b){ S.busy=b; document.querySelectorAll('button').forEach(function(x){ x.disabled=b; }); }
var tt=null;
function toast(t,ms){ var el=$('toast'); el.textContent=t; el.classList.add('show'); clearTimeout(tt); tt=setTimeout(function(){el.classList.remove('show');}, ms||3000); }

function render(){
  var st=S.state; if(!st) return;
  var mode=$('mode');
  mode.innerHTML = st.live ? '<span class="dot ok"></span>All accounts live' : '<span class="dot warn"></span>Main account only';
  mode.title = st.live ? 'Open Bambu Networking runs every enabled account side by side.' : (st.bridge_error||'');
  var b='';
  var L = st.library||{};
  if(L.state==='can_install') b+='<div class="banner info"><b>Turn on multi account</b><br>The Open Bambu Networking library you have doesnt run more than one account. This plugin comes with one that does: OBN '+esc(L.bundle_obn||'?')+' plus multi account. Your current one gets backed up so you can go back.<div class="row" style="margin-top:8px"><button class="primary" data-lib="install_library">Install multi account library</button><span class="muted small">needs an OrcaSlicer restart</span></div></div>';
  else if(L.state==='restart') b+='<div class="banner info">Multi account library is in. Restart OrcaSlicer to start using it.'+(L.can_undo?' <button data-lib="undo_library">Put the original back</button>':'')+'</div>';
  else if(L.state==='outdated') b+='<div class="banner">Your Open Bambu Networking is '+esc(L.obn_version)+' but the multi account build in this plugin is for '+esc(L.bundle_obn)+'. A new build normally lands within a day, update this plugin then. Installing the old one would undo the newer fixes, so its not offered.</div>';
  else if(L.state==='no_obn') b+='<div class="banner">Open Bambu Networking isnt installed. Subscribe to it on the plugin hub, install it from its tab, restart and log in to Bambu, then come back here.</div>';
  else if(!st.live && st.bridge_error) b+='<div class="banner">'+esc(st.bridge_error)+'</div>';
  if(st.restart_needed) b+='<div class="banner">Restart OrcaSlicer to finish switching the main account.</div>';
  if(st.notice) b+='<div class="banner">'+esc(st.notice)+'</div>';
  if(st.live && L.can_undo) b+='<div class="muted small" style="margin:-4px 0 10px">Running the multi account build of Open Bambu Networking. <a href="#" data-lib="undo_library">Put the original back</a></div>';
  $('banners').innerHTML=b;
  var p=st.primary;
  $('primary').innerHTML = p.user_id
    ? '<span class="name">'+esc(p.name)+'</span> <span class="muted">'+esc(p.account)+'</span><div class="muted small">The account OrcaSlicer is logged in to. Presets, MakerWorld and the filament library use this one.</div>'
    : '<span class="err">Not logged in to Bambu Cloud in OrcaSlicer.</span> <span class="muted">Log in there first; extra accounts run next to it.</span>';
  var h='';
  if(!st.accounts.length) h='<div class="muted">None yet. Add one below.</div>';
  st.accounts.forEach(function(a){
    var dot='', txt='';
    if(!a.enabled){ dot=''; txt='Off'; }
    else if(a.same_as_main){ dot='warn'; txt='Same as main account'; }
    else if(!st.live){ dot='warn'; txt='Not running (library has no multi-account support)'; }
    else if(a.connected){ dot='ok'; txt='Connected'; }
    else if(a.started){ dot='warn'; txt='Connecting…'; }
    else { dot='bad'; txt='Not connected'; }
    var pr='';
    (a.printers||[]).forEach(function(x){
      var live = x.online ? 'ok' : '';
      pr+='<li class="pill" title="'+esc(x.dev_id)+'"><span class="dot '+live+'"></span>'+esc(x.name)+(x.model?' <span class="muted">'+esc(x.model)+'</span>':'')+'</li>';
    });
    var exp = a.expires_days==null ? '' : (a.expires_days<0 ? 'token expired' : 'token good for '+a.expires_days+' days');
    h+='<div class="acct"><div class="row">'
      +'<label class="switch"><input type="checkbox" data-en="'+esc(a.user_id)+'"'+(a.enabled?' checked':'')+'> <span class="name">'+esc(a.name)+'</span></label>'
      +'<span class="muted">'+esc(a.account)+'</span><span class="grow"></span>'
      +'<span class="pill"><span class="dot '+dot+'"></span>'+esc(txt)+'</span></div>'
      +'<div class="row" style="margin-top:6px"><input class="label" type="text" maxlength="24" placeholder="Label" value="'+esc(a.label)+'" data-label="'+esc(a.user_id)+'">'
      +'<span class="muted small">'+(a.label?'Printers show as ['+esc(a.label)+'] name':'Add a label to tell the printers apart')+'</span><span class="grow"></span>'
      +'<button data-main="'+esc(a.user_id)+'" title="Swap with the main account (applies after a restart)">Make main</button>'
      +'<button class="danger" data-rm="'+esc(a.user_id)+'">Remove</button></div>'
      +(pr?'<ul class="printers">'+pr+'</ul>':'')
      +'<div class="small '+(a.error?'err':'muted')+'">'+esc(a.error||exp)+'</div>'
      +'</div>';
  });
  $('list').innerHTML=h;
  var waiting=!!st.pending_email;
  document.querySelectorAll('.step2').forEach(function(el){ el.style.display = waiting ? '' : 'none'; });
  $('send').textContent = waiting ? 'Send again' : 'Send code';
}

document.addEventListener('change', function(ev){
  var t=ev.target;
  if(t.dataset.en) send({action:'enable', user_id:t.dataset.en, enabled:t.checked});
  else if(t.dataset.label) send({action:'label', user_id:t.dataset.label, label:t.value});
});
document.addEventListener('click', function(ev){
  var t=ev.target;
  if(t.dataset && t.dataset.lib){ ev.preventDefault(); send({action:t.dataset.lib}); return; }
  if(t.dataset.rm){ if(t.dataset.confirm){ send({action:'remove', user_id:t.dataset.rm}); } else { t.dataset.confirm='1'; t.textContent='Click again to remove'; } }
  else if(t.dataset.main){ if(t.dataset.confirm){ send({action:'make_primary', user_id:t.dataset.main}); } else { t.dataset.confirm='1'; t.textContent='Click again: swap and restart later'; } }
});
function tab(code){
  $('t-code').classList.toggle('on',code); $('t-pw').classList.toggle('on',!code);
  $('t-code').setAttribute('aria-selected',code); $('t-pw').setAttribute('aria-selected',!code);
  $('f-code').hidden=!code; $('f-pw').hidden=code;
}
$('t-code').onclick=function(){tab(true);};
$('t-pw').onclick=function(){tab(false);};
$('send').onclick=function(){ send({action:'send_code', email:$('c-email').value, label:$('c-label').value}); };
$('login').onclick=function(){ send({action:'login_code', code:$('c-code').value}); };
$('cancel').onclick=function(){ send({action:'cancel_login'}); };
$('pwlogin').onclick=function(){ var pw=$('p-pass').value; $('p-pass').value=''; send({action:'login_password', email:$('p-email').value, password:pw, label:$('p-label').value}); };
$('refresh').onclick=function(){ send({action:'refresh'}); };

function onMessage(data){
  if(!data || typeof data!=='object') return;
  if(data.state){ S.state=data.state; }
  if(data.action && data.action!=='state'){ setBusy(false); }
  render();
  if(data.ok===false && data.error){ toast('Error: '+data.error, 6000); }
  else if(data.message){ toast(data.message); }
  if(data.ok!==false && (data.action==='login_code' || data.action==='login_password')){
    ['c-email','c-label','c-code','p-email','p-label'].forEach(function(id){ $(id).value=''; });
  }
}
if(window.orca && window.orca.onMessage){ window.orca.onMessage(onMessage); }
window.__ma={onMessage:onMessage};
post({action:'ready'});
setInterval(function(){ if(!S.busy) post({action:'state'}); }, 4000);
})();
</script>
</body></html>
"""


def icon_png_bytes(size=64):
    """Two overlapping person glyphs, drawn as a small PNG without any image library."""
    import struct
    import zlib

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    def person(x, y, cx, cy, r):
        head = (x - cx) ** 2 + (y - (cy - r * 0.55)) ** 2 <= (r * 0.42) ** 2
        body = (x - cx) ** 2 / (r * 0.85) ** 2 + (y - (cy + r * 0.75)) ** 2 / (r * 0.75) ** 2 <= 1 and y <= cy + r * 0.95
        return head or body

    rows = []
    s = size / 64.0
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            back = person(x, y, 40 * s, 26 * s, 17 * s)
            front = person(x, y, 25 * s, 34 * s, 19 * s)
            if front:
                row += bytes((0, 150, 136, 255))
            elif back:
                row += bytes((0, 150, 136, 140))
            else:
                row += bytes((0, 0, 0, 0))
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


# ---------------------------------------------------------------------------
# OrcaSlicer bindings
# ---------------------------------------------------------------------------

if orca is not None:

    class MultiAccountPage(orca.pages.PagesPluginCapabilityBase):
        core = None

        def get_name(self):
            return "Bambu Accounts"

        def get_icon(self):
            path = os.path.join(HERE, "multi_account_icon.png")
            try:
                if not os.path.isfile(path):
                    with open(path, "wb") as fh:
                        fh.write(icon_png_bytes())
                return path
            except Exception as e:  # noqa: BLE001
                _log("icon: %r" % e)
                return ""

        def _core(self):
            if self.core is None:
                self.core = MultiAccountCore()
                _log("library: %s" % ("multi-account api %d" % self.core.bridge.api
                                      if self.core.bridge.live else self.core.bridge.error))
            return self.core

        def on_load(self):
            try:
                self._core().startup()
            except Exception as e:  # noqa: BLE001
                _log("on_load: %r" % e)

        def get_ui(self):
            self._core()
            return PAGE_HTML

        def on_message(self, message):
            try:
                data = message if isinstance(message, dict) else json.loads(message)
            except Exception:  # noqa: BLE001
                data = {"action": str(message)}
            if not isinstance(data, dict):
                data = {"action": str(data)}
            reply = self._core().handle(data)
            try:
                self.post_message(reply)
            except Exception as e:  # noqa: BLE001
                _log("post_message: %r" % e)

    @orca.plugin
    class MultiAccountPlugin(orca.base):
        def register_capabilities(self):
            orca.register_capability(MultiAccountPage)
