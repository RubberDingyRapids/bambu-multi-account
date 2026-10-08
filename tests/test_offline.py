"""Offline tests for bambu_multi_account_any.py (no OrcaSlicer, no network).

Run from the plugin folder:  python -m unittest tests.test_offline -v
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import glob as _glob
import importlib.util as _ilu

_plugin_file = sorted(_glob.glob(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bambu_multi_account*.py")))[0]
_spec = _ilu.spec_from_file_location("bambu_multi_account", _plugin_file)
ma = _ilu.module_from_spec(_spec)
sys.modules["bambu_multi_account"] = ma
_spec.loader.exec_module(ma)

PRIMARY = {"region": "GLOBAL", "account": "home@example.com", "access_token": "tok-home",
           "refresh_token": "ref-home", "expires_at": "2099-01-01T00:00:00Z", "user_id": "100",
           "user_name": "Home", "nick_name": "", "avatar": "", "firmware_beta_open": False}


class FakeApi:
    """Stands in for BambuApi. Records calls; replies from class-level tables."""
    calls = []
    profiles = {"tok-work": {"uidStr": "200", "name": "Work Person", "account": "me@work.example"},
                "tok-home": {"uidStr": "100", "name": "Home", "account": "home@example.com"}}
    code_reply = {"accessToken": "tok-work", "refreshToken": "ref-work", "expiresIn": 7776000}
    password_reply = {"loginType": "verifyCode"}
    refresh_reply = {"accessToken": "tok-new", "refreshToken": "ref-new", "expiresIn": 7776000}
    refresh_error = None
    devices = {"tok-work": [{"dev_id": "W1", "name": "Work P1S", "online": True, "model": "P1S"}]}

    def __init__(self, region="GLOBAL"):
        self.region = region

    def send_email_code(self, email):
        FakeApi.calls.append(("send", email))

    def login_with_code(self, email, code):
        FakeApi.calls.append(("code", email, code))
        if code != "123456":
            raise ma.CloudError(400, "That code is not right.")
        return dict(FakeApi.code_reply)

    def login_with_password(self, email, password):
        FakeApi.calls.append(("password", email))
        return dict(FakeApi.password_reply)

    def refresh(self, access, refresh):
        FakeApi.calls.append(("refresh", access, refresh))
        if FakeApi.refresh_error:
            raise FakeApi.refresh_error
        return dict(FakeApi.refresh_reply)

    def profile(self, token):
        return dict(FakeApi.profiles[token])

    def printers(self, token):
        return list(FakeApi.devices.get(token, []))


class FakeBridge:
    def __init__(self, live=True):
        self.live = live
        self.api = 1 if live else 0
        self.error = "" if live else "no multi-account support"
        self.reloads = 0
        self.status_doc = {"api": 1, "accounts": []}

    def reload(self):
        if not self.live:
            return None
        self.reloads += 1
        return 1

    def status(self):
        return self.status_doc if self.live else {}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ma_test_")
        with open(os.path.join(self.dir, ma.AUTH_FILE), "w", encoding="utf-8") as fh:
            json.dump(PRIMARY, fh)
        FakeApi.calls = []
        FakeApi.refresh_error = None
        FakeApi.password_reply = {"loginType": "verifyCode"}
        self.bridge = FakeBridge()
        self.core = ma.MultiAccountCore(base_dir=self.dir, api_factory=FakeApi, bridge=self.bridge)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def accounts_file(self):
        with open(os.path.join(self.dir, ma.ACCOUNTS_FILE), encoding="utf-8") as fh:
            return json.load(fh)


class LoginTests(Base):
    def test_code_login_writes_file_and_reloads(self):
        r = self.core.handle({"action": "send_code", "email": "me@work.example", "label": "Work"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["state"]["pending_email"], "me@work.example")
        r = self.core.handle({"action": "login_code", "code": "123456"})
        self.assertTrue(r["ok"], r)
        doc = self.accounts_file()
        self.assertEqual(doc["version"], 1)
        self.assertEqual(len(doc["accounts"]), 1)
        a = doc["accounts"][0]
        self.assertEqual(a["user_id"], "200")
        self.assertEqual(a["access_token"], "tok-work")
        self.assertEqual(a["refresh_token"], "ref-work")
        self.assertEqual(a["label"], "Work")
        self.assertTrue(a["enabled"])
        self.assertEqual(a["region"], "GLOBAL")
        self.assertRegex(a["expires_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(self.bridge.reloads, 1)
        self.assertEqual(r["state"]["pending_email"], "")
        self.assertEqual(r["state"]["accounts"][0]["printers"][0]["dev_id"], "W1")

    def test_wrong_code_keeps_pending(self):
        self.core.handle({"action": "send_code", "email": "me@work.example"})
        r = self.core.handle({"action": "login_code", "code": "000000"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"]["pending_email"], "me@work.example")
        self.assertFalse(os.path.exists(os.path.join(self.dir, ma.ACCOUNTS_FILE)))

    def test_code_without_send(self):
        r = self.core.handle({"action": "login_code", "code": "123456"})
        self.assertFalse(r["ok"])

    def test_rejects_main_account(self):
        FakeApi.code_reply = {"accessToken": "tok-home", "refreshToken": "r", "expiresIn": 100}
        try:
            self.core.handle({"action": "send_code", "email": "home@example.com"})
            r = self.core.handle({"action": "login_code", "code": "123456"})
        finally:
            FakeApi.code_reply = {"accessToken": "tok-work", "refreshToken": "ref-work", "expiresIn": 7776000}
        self.assertFalse(r["ok"])
        self.assertIn("already", r["error"])

    def test_password_falls_back_to_code(self):
        r = self.core.handle({"action": "login_password", "email": "me@work.example", "password": "pw"})
        self.assertTrue(r["ok"], r)
        self.assertIn(("send", "me@work.example"), FakeApi.calls)
        self.assertEqual(r["state"]["pending_email"], "me@work.example")

    def test_password_tfa_is_explained(self):
        FakeApi.password_reply = {"loginType": "tfa", "tfaKey": "k"}
        r = self.core.handle({"action": "login_password", "email": "me@work.example", "password": "pw"})
        self.assertFalse(r["ok"])
        self.assertIn("Email code", r["error"])

    def test_bad_email(self):
        r = self.core.handle({"action": "send_code", "email": "nope"})
        self.assertFalse(r["ok"])


class EditTests(Base):
    def setUp(self):
        super().setUp()
        self.core.handle({"action": "send_code", "email": "me@work.example"})
        self.core.handle({"action": "login_code", "code": "123456"})
        self.bridge.reloads = 0

    def test_toggle_label_remove(self):
        self.assertTrue(self.core.handle({"action": "enable", "user_id": "200", "enabled": False})["ok"])
        self.assertFalse(self.accounts_file()["accounts"][0]["enabled"])
        self.assertTrue(self.core.handle({"action": "label", "user_id": "200", "label": "  Office  "})["ok"])
        self.assertEqual(self.accounts_file()["accounts"][0]["label"], "Office")
        self.assertTrue(self.core.handle({"action": "remove", "user_id": "200"})["ok"])
        self.assertEqual(self.accounts_file()["accounts"], [])
        self.assertEqual(self.bridge.reloads, 3)

    def test_unknown_account(self):
        self.assertFalse(self.core.handle({"action": "remove", "user_id": "999"})["ok"])

    def test_make_primary_swaps(self):
        r = self.core.handle({"action": "make_primary", "user_id": "200"})
        self.assertTrue(r["ok"], r)
        with open(os.path.join(self.dir, ma.AUTH_FILE), encoding="utf-8") as fh:
            auth = json.load(fh)
        self.assertEqual(auth["user_id"], "200")
        self.assertEqual(auth["access_token"], "tok-work")
        self.assertNotIn("enabled", auth)
        self.assertNotIn("label", auth)
        extras = self.accounts_file()["accounts"]
        self.assertEqual([a["user_id"] for a in extras], ["100"])
        self.assertEqual(extras[0]["access_token"], "tok-home")
        self.assertTrue(r["state"]["restart_needed"])

    def test_status_merges_live_state(self):
        self.bridge.status_doc = {"api": 1, "accounts": [
            {"user_id": "200", "connected": True, "started": True}]}
        st = self.core.page_state()
        row = st["accounts"][0]
        self.assertTrue(st["live"])
        self.assertTrue(row["connected"])
        self.assertEqual(row["printers"][0]["dev_id"], "W1")
        self.assertEqual(st["primary"]["user_id"], "100")


class RefreshTests(Base):
    def _write(self, expires_at, refresh="ref-work"):
        with open(os.path.join(self.dir, ma.ACCOUNTS_FILE), "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "accounts": [{
                "user_id": "200", "access_token": "tok-work", "refresh_token": refresh,
                "expires_at": expires_at, "enabled": True, "label": ""}]}, fh)
        self.core.store.load()

    def test_due_token_is_refreshed(self):
        self._write(ma.iso_in(3600))
        self.assertEqual(self.core.refresh_tokens(), [])
        a = self.accounts_file()["accounts"][0]
        self.assertEqual(a["access_token"], "tok-new")
        self.assertEqual(a["refresh_token"], "ref-new")
        self.assertGreater(ma.parse_iso(a["expires_at"]), time.time() + 80 * 86400)

    def test_fresh_token_is_left(self):
        self._write(ma.iso_in(60 * 86400))
        self.core.refresh_tokens()
        self.assertNotIn("refresh", [c[0] for c in FakeApi.calls])

    def test_failed_refresh_marks_account(self):
        self._write(ma.iso_in(3600))
        FakeApi.refresh_error = ma.CloudError(401, "nope")
        failed = self.core.refresh_tokens()
        self.assertEqual(len(failed), 1)
        self.assertIn("Log this account in again", self.accounts_file()["accounts"][0]["error"])

    def test_startup_reloads(self):
        self._write(ma.iso_in(3600))
        self.core.startup()
        self.assertEqual(self.bridge.reloads, 1)


class HelperTests(unittest.TestCase):
    def test_iso_roundtrip(self):
        now = 1_800_000_000
        self.assertEqual(ma.parse_iso(ma.iso_in(60, now)), now + 60)
        self.assertIsNone(ma.parse_iso("garbage"))
        self.assertIsNone(ma.parse_iso(""))

    def test_needs_refresh(self):
        now = time.time()
        self.assertTrue(ma.needs_refresh({"expires_at": ""}, now))
        self.assertTrue(ma.needs_refresh({"expires_at": ma.iso_in(86400, now)}, now))
        self.assertFalse(ma.needs_refresh({"expires_at": ma.iso_in(60 * 86400, now)}, now))

    def test_session_from_login_needs_uid(self):
        with self.assertRaises(ma.CloudError):
            ma.session_from_login({"accessToken": "t"}, {"uid": 0}, "GLOBAL")
        rec = ma.session_from_login({"accessToken": "t", "expiresIn": 10}, {"uid": 5, "name": "N"}, "GLOBAL")
        self.assertEqual(rec["user_id"], "5")
        self.assertEqual(set(ma.SESSION_KEYS), set(rec))

    def test_store_ignores_unusable_entries(self):
        d = tempfile.mkdtemp(prefix="ma_store_")
        try:
            with open(os.path.join(d, ma.ACCOUNTS_FILE), "w", encoding="utf-8") as fh:
                json.dump({"accounts": [{"user_id": "1"}, {"user_id": "2", "access_token": "t"}, "x"]}, fh)
            store = ma.AccountStore(d).load()
            self.assertEqual([a["user_id"] for a in store.accounts], ["2"])
            self.assertTrue(store.accounts[0]["enabled"])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_offline_bridge_mode(self):
        d = tempfile.mkdtemp(prefix="ma_off_")
        try:
            core = ma.MultiAccountCore(base_dir=d, api_factory=FakeApi, bridge=FakeBridge(live=False))
            st = core.page_state()
            self.assertFalse(st["live"])
            self.assertEqual(st["primary"]["user_id"], "")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_icon_is_png(self):
        self.assertTrue(ma.icon_png_bytes(16).startswith(b"\x89PNG"))

    def test_bridge_without_library(self):
        d = tempfile.mkdtemp(prefix="ma_br_")
        try:
            b = ma.ObnBridge(d)
            self.assertFalse(b.live)
            self.assertTrue(b.error)
            self.assertIsNone(b.reload())
            self.assertEqual(b.status(), {})
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
