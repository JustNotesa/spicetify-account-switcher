"""Tests for spotify_accounts_helper (v2, one Spotify profile per account).

Run from the helper directory:  python3 -m unittest -v test_helper

Everything runs against temporary directories (SPOTIFY_APPSUPPORT, SPOTIFY_CACHES,
SPOTIFY_MA_DIR, SPOTIFY_TRASH) with FAKE profiles (dummy prefs values only). The
process list, launching, killing and the browser are replaced by fakes - no real
Spotify process is ever started, listed for decisions, or signalled.
"""

import errno
import http.client
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import spotify_accounts_helper as sah  # noqa: E402

# keep expected error tracebacks of the in-process server out of the test output
sah.log.addHandler(__import__("logging").NullHandler())
sah.log.propagate = False

ORIGIN = "https://xpui.app.spotify.com"

DEFAULT_PREFS = (
    b'app.autostart-configured=true\n'
    b'app.window.position.height=893\n'
    b'app.window.position.saved=true\n'
    b'app.window.position.width=1512\n'
    b'app.window.position.x=10\n'
    b'app.window.position.y=33\n'
    b'app.window.state=2\n'
    b'app.window.state.saved=true\n'
    b'autologin.blob="SECRETBLOB1"\n'
    b'autologin.canonical_username="alice"\n'
    b'autologin.saved_credentials="SECRETCRED1"\n'
    b'autologin.username="alice"\n'
    b'core.clock_delta=0\n'
    b'storage.last-location="/Pfad/zu/Storage"  \n'
    b'zz.weird="\xc3\xa4 \\"quoted\\" = x"\r\n'
)
SECOND_PREFS = (
    b'app.autostart-configured=true\n'
    b'app.window.position.height=827\n'
    b'app.window.position.saved=true\n'
    b'app.window.position.width=1440\n'
    b'app.window.state.saved=true\n'
    b'autologin.blob="SECRETBLOB2"\n'
    b'autologin.canonical_username="bob"\n'
    b'autologin.username="bob"\n'
    b'core.clock_delta=0\n'
    b'ui.hide_hpto=true'          # no trailing newline on purpose
)
WINDOW_DEFAULT = [
    b'app.window.position.height=893',
    b'app.window.position.saved=true',
    b'app.window.position.width=1512',
    b'app.window.position.x=10',
    b'app.window.position.y=33',
    b'app.window.state=2',
    b'app.window.state.saved=true',
]


def _forbidden(*_a, **_k):
    raise AssertionError("real Spotify/browser/process control must not be used in tests")


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def write(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class FakeSystem:
    """Fake Spotify processes: pid -> profile. launch() spawns a fake process,
    kill() records the signal (SIGTERM ends the process unless ignore_term)."""

    def __init__(self):
        self.procs = {}
        self.next_pid = 200
        self.calls = []
        self.spawn_on_launch = True
        self.ignore_term = False
        self.lock = threading.Lock()

    def list(self):
        with self.lock:
            return sorted(self.procs.items())

    def launch(self, profile):
        with self.lock:
            self.calls.append(("launch", profile))
            if self.spawn_on_launch:
                self.procs[self.next_pid] = profile
                self.next_pid += 1

    def kill(self, pid, sig):
        with self.lock:
            self.calls.append(("kill", pid, sig))
            if pid not in self.procs:
                raise ProcessLookupError(pid)
            if sig == signal.SIGKILL or not self.ignore_term:
                del self.procs[pid]

    def browser(self, url):
        with self.lock:
            self.calls.append(("browser", url))

    def kills(self):
        return [c for c in self.calls if c[0] == "kill"]

    def pid_of(self, profile):
        for pid, prof in self.list():
            if prof == profile:
                return pid
        return None


ENV_KEYS = ("SPOTIFY_APPSUPPORT", "SPOTIFY_CACHES", "SPOTIFY_MA_DIR", "SPOTIFY_TRASH",
            "SPOTIFY_HELPER_DRYRUN", "SPOTIFY_HELPER_PORT", "SPOTIFY_HELPER_OPEN_BROWSER",
            "SPOTIFY_HELPER_IDLE", "SPOTIFY_HELPER_NO_LAUNCHD")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="spotify-helper-test-")
        self.appsupport = os.path.join(self.tmp, "AppSupport")
        self.caches = os.path.join(self.tmp, "Caches")
        self.ma_dir = os.path.join(self.tmp, "AppSupport", "spotify-multiaccount")
        self.trash = os.path.join(self.tmp, "Trash")
        write(os.path.join(self.appsupport, "Spotify", "prefs"), DEFAULT_PREFS)
        write(os.path.join(self.appsupport, "Spotify-second", "prefs"), SECOND_PREFS)
        os.chmod(os.path.join(self.appsupport, "Spotify-second", "prefs"), 0o644)
        # must all be ignored by the scan
        write(os.path.join(self.appsupport, "Spotify-Accounts", "prefs"), b'autologin.canonical_username="x"\n')
        write(os.path.join(self.appsupport, "Spotify-Profiles", "prefs"), b"a=1\n")
        write(os.path.join(self.appsupport, "Some Spotify Converter", "prefs"), b"a=1\n")
        write(os.path.join(self.appsupport, "Spotify-default", "prefs"), b"a=1\n")
        write(os.path.join(self.appsupport, "Spotify-x.y", "prefs"), b"a=1\n")
        os.makedirs(os.path.join(self.appsupport, "Spotify-noprefs"))
        write(os.path.join(self.caches, "com.spotify.client", "data"), b"cache")
        write(os.path.join(self.caches, "com.spotify.client-second", "data"), b"cache2")

        self._old_env = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({
            "SPOTIFY_APPSUPPORT": self.appsupport, "SPOTIFY_CACHES": self.caches,
            "SPOTIFY_MA_DIR": self.ma_dir, "SPOTIFY_TRASH": self.trash,
            "SPOTIFY_HELPER_DRYRUN": "1", "SPOTIFY_HELPER_PORT": "0"})

        # belt and braces: the real implementations must never run in tests
        self._orig = {n: getattr(sah, n) for n in
                      ("list_spotify_processes", "launch_spotify", "kill_pid", "open_browser")}
        for n in self._orig:
            setattr(sah, n, _forbidden)

        self.helper = sah.Helper.from_env()
        self.assertTrue(self.helper.dryrun)
        for p in (self.helper.appsupport, self.helper.caches, self.helper.ma_dir,
                  self.helper.trash_dir):
            self.assertTrue(p.startswith(self.tmp), p)
        self.fake = FakeSystem()
        self.helper.list_processes = self.fake.list
        self.helper.launch = self.fake.launch
        self.helper.kill = self.fake.kill
        self.helper.open_browser = self.fake.browser
        self.helper.switch_timeout = 2.0
        self.helper.add_timeout = 2.0
        self.helper.term_timeout = 0.3
        self.helper.poll_interval = 0.02
        self.helper.browser_delay = 0
        # side effects go to the fakes (dry run only logs; tested separately)
        self.helper.dryrun = False

        self.server = sah.make_server(self.helper)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.helper.wait_idle(10)
        self.server.shutdown()
        self.server.server_close()
        for n, f in self._orig.items():
            setattr(sah, n, f)
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ---- helpers ----
    def request(self, method, path, body=None, origin=None, headers=None, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = dict(headers or {})
        if origin is not None:
            hdrs["Origin"] = origin
        data = raw
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=hdrs)
        resp = conn.getresponse()
        payload = resp.read()
        conn.close()
        return resp.status, (json.loads(payload) if payload else None), resp

    def prefs(self, profile):
        return os.path.join(self.helper.profile_dir(profile), "prefs")

    def wait_idle(self):
        self.assertTrue(self.helper.wait_idle(10), "background job did not finish")

    def wait_for(self, cond, timeout=5.0, msg="condition"):
        deadline = time.time() + timeout
        while not cond():
            self.assertLess(time.time(), deadline, "timeout waiting for " + msg)
            time.sleep(0.01)

    def run_default(self, pid=100):
        self.fake.procs[pid] = "default"

    def mode(self, path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def accounts(self):
        status, payload, _ = self.request("GET", "/accounts")
        self.assertEqual(status, 200)
        return payload["accounts"]


# --------------------------------------------------------------------------

class ScanAndAccountsTests(Base):
    def test_health(self):
        status, payload, _ = self.request("GET", "/health")
        self.assertEqual((status, payload), (200, {"ok": True, "version": "2.0.0"}))

    def test_scan_rules(self):
        self.assertEqual(self.helper.scan_profiles(), ["default", "second"])
        os.remove(self.prefs("default"))
        self.assertEqual(self.helper.scan_profiles(), ["second"])

    def test_accounts_listing(self):
        self.run_default()
        accs = self.accounts()
        self.assertEqual(accs, [
            {"profile": "default", "username": "alice", "displayName": "alice", "imageUrl": None,
             "running": True, "current": True, "loggedIn": True},
            {"profile": "second", "username": "bob", "displayName": "bob", "imageUrl": None,
             "running": False, "current": False, "loggedIn": True},
        ])
        # nothing written by a plain listing (first run imports nothing)
        self.assertFalse(os.path.exists(os.path.join(self.ma_dir, "profiles.json")))

    def test_accounts_sorting_and_metadata(self):
        self.fake.procs[300] = "second"
        write(self.prefs("zzz"), b"a=1\n")   # not logged in
        self.request("POST", "/register", {"username": "alice", "displayName": "Zoe",
                                           "imageUrl": "https://i.example/a.jpg"})
        accs = self.accounts()
        self.assertEqual([a["profile"] for a in accs], ["second", "default", "zzz"])
        self.assertEqual(accs[0]["current"], True)
        self.assertEqual(accs[1]["displayName"], "Zoe")
        self.assertEqual(accs[1]["imageUrl"], "https://i.example/a.jpg")
        self.assertEqual(accs[2], {"profile": "zzz", "username": None, "displayName": None,
                                   "imageUrl": None, "running": False, "current": False,
                                   "loggedIn": False})

    def test_current_with_two_running_is_last_registered(self):
        self.fake.procs[100] = "default"
        self.fake.procs[300] = "second"
        # nobody registered: oldest instance
        self.assertEqual([a["profile"] for a in self.accounts() if a["current"]], ["default"])
        self.request("POST", "/register", {"username": "bob"})
        self.assertEqual([a["profile"] for a in self.accounts() if a["current"]], ["second"])
        self.request("POST", "/register", {"username": "alice"})
        self.assertEqual([a["profile"] for a in self.accounts() if a["current"]], ["default"])
        self.assertTrue(all(a["running"] for a in self.accounts()))

    def test_profiles_json_only_entry_listed(self):
        os.makedirs(self.ma_dir, exist_ok=True)
        write(os.path.join(self.ma_dir, "profiles.json"), json.dumps({"profiles": {
            "a20260101000000": {"username": None, "displayName": None, "imageUrl": None},
            "../evil": {"username": "x"}, "BAD": {}}}).encode())
        self.assertEqual([a["profile"] for a in self.accounts()],
                         ["default", "second", "a20260101000000"])


class RegisterTests(Base):
    def test_register_maps_by_canonical_username(self):
        status, payload, _ = self.request("POST", "/register", {
            "username": "bob", "displayName": "Bob", "imageUrl": "https://i.example/b.jpg"})
        self.assertEqual((status, payload), (200, {"ok": True, "profile": "second"}))
        status, payload, _ = self.request("POST", "/register", {"username": "alice"})
        self.assertEqual((status, payload), (200, {"ok": True, "profile": "default"}))
        path = os.path.join(self.ma_dir, "profiles.json")
        self.assertEqual(self.mode(path), 0o600)
        self.assertEqual(self.mode(self.ma_dir), 0o700)
        with open(path) as fh:
            data = json.load(fh)["profiles"]
        self.assertEqual(data["second"]["username"], "bob")
        self.assertEqual(data["second"]["displayName"], "Bob")
        self.assertEqual(data["second"]["imageUrl"], "https://i.example/b.jpg")
        self.assertIn("createdAt", data["second"])
        self.assertIn("updatedAt", data["second"])
        self.assertEqual(data["default"]["displayName"], "alice")
        # prefs never modified by register
        self.assertEqual(read(self.prefs("default")), DEFAULT_PREFS)
        self.assertEqual(read(self.prefs("second")), SECOND_PREFS)

    def test_register_unknown_404(self):
        status, payload, _ = self.request("POST", "/register", {"username": "nobody"})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])

    def test_register_prefers_running_profile(self):
        write(self.prefs("second"), b'autologin.canonical_username="bob"\n')
        self.fake.procs[400] = "second"
        _, payload, _ = self.request("POST", "/register", {"username": "bob"})
        self.assertEqual(payload["profile"], "second")
        del self.fake.procs[400]
        self.fake.procs[401] = "second"
        _, payload, _ = self.request("POST", "/register", {"username": "bob"})
        self.assertEqual(payload["profile"], "second")

    def test_register_fallback_running_profile_without_login_in_prefs(self):
        write(self.prefs("fresh"), b"a=1\n")
        self.fake.procs[100] = "default"
        self.fake.procs[500] = "fresh"
        _, payload, _ = self.request("POST", "/register", {"username": "carol"})
        self.assertEqual(payload["profile"], "fresh")

    def test_register_validation(self):
        for body in ({"displayName": "x"}, {"username": ""}, {"username": 42},
                     {"username": "a\x00b"}, {"username": "a" * 300},
                     {"username": "alice", "displayName": 5},
                     {"username": "alice", "imageUrl": ["x"]}):
            status, payload, _ = self.request("POST", "/register", body)
            self.assertEqual(status, 400, body)
            self.assertFalse(payload["ok"])


class SwitchTests(Base):
    def test_switch_to_current_is_noop(self):
        self.run_default()
        status, payload, _ = self.request("POST", "/switch", {"profile": "default"})
        self.assertEqual((status, payload), (200, {"ok": True, "switching": False}))
        self.assertFalse(self.helper.busy())
        self.assertEqual(self.fake.calls, [])

    def test_switch_unknown_and_invalid(self):
        status, payload, _ = self.request("POST", "/switch", {"profile": "nope"})
        self.assertEqual(status, 404)
        self.assertFalse(payload["ok"])
        for bad in ("Spotify-Accounts", "Accounts", "../x", "", None, 5, "a" * 41):
            status, _, _ = self.request("POST", "/switch", {"profile": bad})
            self.assertEqual(status, 400, bad)
        status, _, _ = self.request("POST", "/switch", {"profile": "noprefs"})
        self.assertEqual(status, 404)
        self.assertFalse(self.helper.busy())

    def test_switch_waits_for_register_then_kills_only_old_pid(self):
        self.run_default(100)
        self.fake.procs[150] = "other"   # unrelated instance, must survive
        status, payload, _ = self.request("POST", "/switch", {"profile": "second"})
        self.assertEqual((status, payload), (202, {"ok": True, "switching": True}))
        self.wait_for(lambda: ("launch", "second") in self.fake.calls, msg="launch")
        # window position copied before launch, all other lines byte-identical
        new = read(self.prefs("second"))
        self.assertEqual(new,
                         b'app.autostart-configured=true\n' + b"\n".join(WINDOW_DEFAULT) + b"\n"
                         b'autologin.blob="SECRETBLOB2"\n'
                         b'autologin.canonical_username="bob"\n'
                         b'autologin.username="bob"\n'
                         b'core.clock_delta=0\n'
                         b'ui.hide_hpto=true')
        self.assertEqual(self.mode(self.prefs("second")), 0o644)
        self.assertEqual(read(self.prefs("default")), DEFAULT_PREFS)
        time.sleep(0.2)
        self.assertEqual(self.fake.kills(), [], "old instance must stay until /register")
        self.assertTrue(self.helper.busy())
        # both running now; the target registers -> it becomes current
        status, payload, _ = self.request("POST", "/register", {"username": "bob"})
        self.assertEqual(payload, {"ok": True, "profile": "second"})
        self.wait_idle()
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM)])
        self.assertEqual(sorted(self.fake.procs.values()), ["other", "second"])
        self.assertEqual(self.helper.last_job.result, "done")
        self.assertEqual([a["profile"] for a in self.accounts() if a["current"]], ["second"])

    def test_register_from_old_instance_is_not_ready_signal(self):
        self.run_default(100)
        self.request("POST", "/switch", {"profile": "second"})
        self.wait_for(lambda: ("launch", "second") in self.fake.calls, msg="launch")
        self.request("POST", "/register", {"username": "alice"})
        time.sleep(0.2)
        self.assertEqual(self.fake.kills(), [])
        self.request("POST", "/register", {"username": "bob"})
        self.wait_idle()
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM)])

    def test_switch_timeout_keeps_old_instance(self):
        self.run_default(100)
        self.helper.switch_timeout = 0.3
        status, _, _ = self.request("POST", "/switch", {"profile": "second"})
        self.assertEqual(status, 202)
        self.wait_idle()
        self.assertEqual(self.helper.last_job.result, "timeout")
        self.assertEqual(self.fake.kills(), [])
        self.assertIn(100, self.fake.procs)
        # a late /register no longer kills anything
        self.request("POST", "/register", {"username": "bob"})
        time.sleep(0.1)
        self.assertEqual(self.fake.kills(), [])

    def test_sigkill_after_term_timeout(self):
        self.run_default(100)
        self.fake.ignore_term = True
        self.request("POST", "/switch", {"profile": "second"})
        self.wait_for(lambda: ("launch", "second") in self.fake.calls, msg="launch")
        t0 = time.monotonic()
        self.request("POST", "/register", {"username": "bob"})
        self.wait_idle()
        self.assertGreaterEqual(time.monotonic() - t0, 0.25)
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM),
                                             ("kill", 100, signal.SIGKILL)])
        self.assertEqual(list(self.fake.procs.values()), ["second"])

    def test_target_closed_while_waiting_aborts(self):
        self.run_default(100)
        self.helper.switch_timeout = 5
        self.request("POST", "/switch", {"profile": "second"})
        self.wait_for(lambda: self.fake.pid_of("second") is not None, msg="spawn")
        time.sleep(0.1)
        del self.fake.procs[self.fake.pid_of("second")]  # user quit the new window
        self.wait_idle()
        self.assertEqual(self.helper.last_job.result, "aborted")
        self.assertEqual(self.fake.kills(), [])

    def test_switch_to_already_running_target(self):
        self.fake.procs[100] = "default"
        self.fake.procs[300] = "second"
        status, _, _ = self.request("POST", "/switch", {"profile": "second"})
        self.assertEqual(status, 202)
        self.wait_idle()
        self.assertNotIn(("launch", "second"), self.fake.calls)
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM)])
        self.assertEqual(read(self.prefs("second")), SECOND_PREFS)  # running: no copy

    def test_switch_with_nothing_running(self):
        status, _, _ = self.request("POST", "/switch", {"profile": "second"})
        self.assertEqual(status, 202)
        self.wait_idle()
        self.assertEqual(self.fake.calls, [("launch", "second")])
        self.assertEqual(read(self.prefs("second")), SECOND_PREFS)  # no source

    def test_switch_to_default(self):
        self.fake.procs[300] = "second"
        self.request("POST", "/switch", {"profile": "default"})
        self.wait_for(lambda: ("launch", "default") in self.fake.calls, msg="launch")
        self.request("POST", "/register", {"username": "alice"})
        self.wait_idle()
        self.assertEqual(self.fake.kills(), [("kill", 300, signal.SIGTERM)])
        # window lines of second replaced the ones in default, rest unchanged
        lines = read(self.prefs("default")).split(b"\n")
        self.assertEqual([ln for ln in lines if ln.startswith(b"app.window.")], [
            b'app.window.position.height=827', b'app.window.position.saved=true',
            b'app.window.position.width=1440', b'app.window.state.saved=true'])
        self.assertEqual([ln for ln in lines if not ln.startswith(b"app.window.")],
                         [ln for ln in DEFAULT_PREFS.split(b"\n") if not ln.startswith(b"app.window.")])

    def test_dryrun_never_launches_or_kills(self):
        self.helper.dryrun = True
        self.helper.launch = self.helper.kill = self.helper.open_browser = _forbidden
        self.run_default(100)
        self.fake.procs[300] = "second"
        self.request("POST", "/switch", {"profile": "second"})
        self.wait_idle()
        self.assertIn(100, self.fake.procs)
        self.helper.add_timeout = 0.2
        self.request("POST", "/add", {})
        self.request("POST", "/register", {"username": "bob"})  # not the target
        self.wait_idle()


class AddTests(Base):
    EXPECTED_URL = (
        "https://accounts.spotify.com/logout?continue="
        "https%3A%2F%2Faccounts.spotify.com%2Fde%2Flogin%3Fallow_password%3D1%26continue%3D"
        "https%253A%252F%252Faccounts.spotify.com%252Fde%252Fstatus")

    def test_login_url_double_encoded(self):
        url = sah.build_login_url()
        self.assertEqual(url, self.EXPECTED_URL)
        from urllib.parse import parse_qs, urlsplit
        outer = urlsplit(url)
        self.assertEqual((outer.netloc, outer.path), ("accounts.spotify.com", "/logout"))
        inner = parse_qs(outer.query)["continue"][0]
        inner_q = parse_qs(urlsplit(inner).query)
        self.assertEqual(inner_q["allow_password"], ["1"])
        self.assertEqual(inner_q["continue"], ["https://accounts.spotify.com/de/status"])

    def test_add_flow(self):
        self.run_default(100)
        status, payload, _ = self.request("POST", "/add", {})
        self.assertEqual(status, 202)
        new = payload["profile"]
        self.assertRegex(new, r"^a\d{14}$")
        self.assertEqual(payload, {"ok": True, "profile": new, "loginUrl": self.EXPECTED_URL})
        with open(os.path.join(self.ma_dir, "profiles.json")) as fh:
            entry = json.load(fh)["profiles"][new]
        self.assertEqual((entry["username"], entry["displayName"], entry["imageUrl"]),
                         (None, None, None))
        self.wait_for(lambda: ("browser", self.EXPECTED_URL) in self.fake.calls, msg="browser")
        self.assertEqual(self.fake.calls[:2], [("launch", new), ("browser", self.EXPECTED_URL)])
        self.assertFalse(os.path.exists(self.helper.profile_dir(new)), "helper must not create it")
        accs = {a["profile"]: a for a in self.accounts()}
        self.assertEqual((accs[new]["running"], accs[new]["loggedIn"]), (True, False))
        time.sleep(0.2)
        self.assertEqual(self.fake.kills(), [])
        # user logs in, Spotify writes the new profile's prefs, extension registers
        write(self.prefs(new), b'autologin.canonical_username="carol"\n')
        status, payload, _ = self.request("POST", "/register",
                                          {"username": "carol", "displayName": "Carol"})
        self.assertEqual(payload, {"ok": True, "profile": new})
        self.wait_idle()
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM)])
        accs = self.accounts()
        self.assertEqual((accs[0]["profile"], accs[0]["displayName"], accs[0]["current"]),
                         (new, "Carol", True))

    def test_add_register_before_prefs_written(self):
        self.run_default(100)
        _, payload, _ = self.request("POST", "/add", {"openBrowser": False})
        new = payload["profile"]
        self.wait_for(lambda: self.fake.pid_of(new) is not None, msg="spawn")
        _, reg, _ = self.request("POST", "/register", {"username": "carol"})
        self.assertEqual(reg["profile"], new)
        self.wait_idle()
        self.assertEqual(self.fake.kills(), [("kill", 100, signal.SIGTERM)])
        self.assertNotIn(("browser", self.EXPECTED_URL), self.fake.calls)

    def test_add_timeout_keeps_old(self):
        self.run_default(100)
        self.helper.add_timeout = 0.3
        self.request("POST", "/add", {})
        self.wait_idle()
        self.assertEqual(self.helper.last_job.result, "timeout")
        self.assertEqual(self.fake.kills(), [])

    def test_add_id_collision(self):
        fixed = sah.datetime.datetime(2026, 11, 11, 12, 0, 0, tzinfo=sah.datetime.timezone.utc)
        self.helper.now = lambda: fixed
        os.makedirs(os.path.join(self.caches, "com.spotify.client-a20261111120000"))
        self.helper.add_timeout = 0.1
        _, payload, _ = self.request("POST", "/add", {"openBrowser": False})
        self.assertEqual(payload["profile"], "a20261111120000-2")
        self.wait_idle()

    def test_add_env_and_body_disable_browser(self):
        os.environ["SPOTIFY_HELPER_OPEN_BROWSER"] = "0"
        try:
            self.assertFalse(sah.Helper.from_env().open_browser_on_add)
        finally:
            del os.environ["SPOTIFY_HELPER_OPEN_BROWSER"]
        self.assertTrue(sah.Helper.from_env().open_browser_on_add)
        self.helper.open_browser_on_add = False
        self.helper.add_timeout = 0.1
        self.request("POST", "/add", {})
        self.wait_idle()
        self.assertEqual([c for c in self.fake.calls if c[0] == "browser"], [])

    def test_add_invalid_open_browser(self):
        status, payload, _ = self.request("POST", "/add", {"openBrowser": "no"})
        self.assertEqual(status, 400)
        self.assertFalse(self.helper.busy())
        self.assertFalse(os.path.exists(os.path.join(self.ma_dir, "profiles.json")))


class RemoveTests(Base):
    def test_remove_moves_to_trash(self):
        self.run_default(100)
        self.request("POST", "/register", {"username": "bob"})
        self.helper.now = lambda: sah.datetime.datetime(2026, 11, 11, 13, 14, 15,
                                                         tzinfo=sah.datetime.timezone.utc)
        status, payload, _ = self.request("POST", "/remove", {"profile": "second"})
        self.assertEqual((status, payload), (200, {"ok": True}))
        self.assertFalse(os.path.exists(os.path.join(self.appsupport, "Spotify-second")))
        self.assertFalse(os.path.exists(os.path.join(self.caches, "com.spotify.client-second")))
        self.assertEqual(sorted(os.listdir(self.trash)),
                         ["Spotify-second-20261111-131415",
                          "com.spotify.client-second-20261111-131415"])
        self.assertEqual(read(os.path.join(self.trash, "Spotify-second-20261111-131415", "prefs")),
                         SECOND_PREFS)
        with open(os.path.join(self.ma_dir, "profiles.json")) as fh:
            self.assertNotIn("second", json.load(fh)["profiles"])
        self.assertEqual([a["profile"] for a in self.accounts()], ["default"])
        status, _, _ = self.request("POST", "/remove", {"profile": "second"})
        self.assertEqual(status, 404)
        # default profile untouched
        self.assertEqual(read(self.prefs("default")), DEFAULT_PREFS)

    def test_remove_name_clash_in_trash(self):
        self.helper.now = lambda: sah.datetime.datetime(2026, 1, 2, 3, 4, 5,
                                                         tzinfo=sah.datetime.timezone.utc)
        os.makedirs(os.path.join(self.trash, "Spotify-second-20260102-030405"))
        self.request("POST", "/remove", {"profile": "second"})
        self.assertTrue(os.path.isdir(os.path.join(self.trash, "Spotify-second-20260102-030405-2")))

    def test_remove_refuses_default_running_unknown(self):
        status, _, _ = self.request("POST", "/remove", {"profile": "default"})
        self.assertEqual(status, 409)
        self.fake.procs[300] = "second"
        status, payload, _ = self.request("POST", "/remove", {"profile": "second"})
        self.assertEqual(status, 409)
        self.assertFalse(payload["ok"])
        status, _, _ = self.request("POST", "/remove", {"profile": "nope"})
        self.assertEqual(status, 404)
        status, _, _ = self.request("POST", "/remove", {"profile": "../Spotify"})
        self.assertEqual(status, 400)
        self.assertTrue(os.path.isfile(self.prefs("second")))
        self.assertTrue(os.path.isfile(self.prefs("default")))
        self.assertFalse(os.path.exists(self.trash))

    def test_remove_cross_device_falls_back_to_move(self):
        real_rename = os.rename
        moved = []

        def exdev(src, dst):
            raise OSError(errno.EXDEV, "cross-device link")

        def fake_move(src, dst):
            moved.append((src, dst))
            return real_rename(src, dst)
        with mock.patch.object(sah.os, "rename", exdev), \
                mock.patch.object(sah.shutil, "move", fake_move):
            status, _, _ = self.request("POST", "/remove", {"profile": "second"})
        self.assertEqual(status, 200)
        self.assertEqual(len(moved), 2)
        self.assertEqual(len(os.listdir(self.trash)), 2)


class LockTests(Base):
    def test_409_while_job_running(self):
        self.assertTrue(self.helper.switch_lock.acquire(blocking=False))
        try:
            for path, body in (("/switch", {"profile": "second"}), ("/add", {}),
                               ("/remove", {"profile": "second"})):
                status, payload, _ = self.request("POST", path, body)
                self.assertEqual(status, 409, path)
                self.assertFalse(payload["ok"])
        finally:
            self.helper.switch_lock.release()
        self.assertEqual(self.fake.calls, [])

    def test_parallel_requests_only_one_wins(self):
        self.run_default(100)
        status1, _, _ = self.request("POST", "/switch", {"profile": "second"})
        status2, _, _ = self.request("POST", "/add", {})
        status3, _, _ = self.request("POST", "/switch", {"profile": "second"})
        self.assertEqual((status1, status2, status3), (202, 409, 409))
        self.request("POST", "/register", {"username": "bob"})
        self.wait_idle()
        status, _, _ = self.request("POST", "/switch", {"profile": "default"})
        self.assertEqual(status, 202)
        self.request("POST", "/register", {"username": "alice"})
        self.wait_idle()

    def test_errors(self):
        status, payload, _ = self.request("GET", "/nope")
        self.assertEqual((status, payload["ok"]), (404, False))
        status, _, _ = self.request("GET", "/switch")
        self.assertEqual(status, 405)
        status, _, _ = self.request("POST", "/accounts", {})
        self.assertEqual(status, 405)
        status, payload, _ = self.request("POST", "/register", raw=b"{not json",
                                          headers={"Content-Type": "application/json"})
        self.assertEqual((status, payload["ok"]), (400, False))
        status, _, _ = self.request("POST", "/register", raw=b"[1]")
        self.assertEqual(status, 400)


class CorsTests(Base):
    def test_allowed_origin(self):
        status, _, resp = self.request("GET", "/health", origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), ORIGIN)
        self.assertEqual(resp.getheader("Access-Control-Allow-Private-Network"), "true")
        status, _, resp = self.request("POST", "/register", {"username": "alice"}, origin=ORIGIN)
        self.assertEqual(status, 200)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), ORIGIN)
        status, _, resp = self.request("POST", "/switch", {"profile": "nobody"}, origin=ORIGIN)
        self.assertEqual(status, 404)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), ORIGIN)

    def test_no_origin_ok(self):
        status, _, resp = self.request("GET", "/accounts")
        self.assertEqual(status, 200)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertIsNone(resp.getheader("Access-Control-Allow-Private-Network"))

    def test_foreign_origin_forbidden(self):
        self.run_default(100)
        for origin in ("https://evil.example", "null", "http://xpui.app.spotify.com",
                       "https://xpui.app.spotify.com.evil.example"):
            for method, path, body in (("GET", "/health", None), ("GET", "/accounts", None),
                                       ("POST", "/register", {"username": "alice"}),
                                       ("POST", "/switch", {"profile": "second"}),
                                       ("POST", "/remove", {"profile": "second"}),
                                       ("POST", "/add", {})):
                status, payload, resp = self.request(method, path, body, origin=origin)
                self.assertEqual(status, 403, (origin, path))
                self.assertFalse(payload["ok"])
                self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))
        self.assertFalse(os.path.exists(os.path.join(self.ma_dir, "profiles.json")))
        self.assertFalse(self.helper.busy())
        self.assertEqual(self.fake.calls, [])
        self.assertTrue(os.path.isfile(self.prefs("second")))

    def test_preflight(self):
        status, _, resp = self.request("OPTIONS", "/switch", origin=ORIGIN, headers={
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(status, 204)
        self.assertEqual(resp.getheader("Access-Control-Allow-Origin"), ORIGIN)
        self.assertIn("POST", resp.getheader("Access-Control-Allow-Methods"))
        self.assertIn("Content-Type", resp.getheader("Access-Control-Allow-Headers"))
        self.assertEqual(resp.getheader("Access-Control-Allow-Private-Network"), "true")

    def test_preflight_foreign_origin(self):
        status, _, resp = self.request("OPTIONS", "/switch", origin="https://evil.example",
                                       headers={"Access-Control-Request-Method": "POST"})
        self.assertEqual(status, 403)
        self.assertIsNone(resp.getheader("Access-Control-Allow-Origin"))

    def test_foreign_host_forbidden(self):
        status, _, _ = self.request("GET", "/health", headers={"Host": "evil.example:48655"})
        self.assertEqual(status, 403)
        for host in ("localhost:48655", "127.0.0.1:48655", "[::1]:48655"):
            status, _, _ = self.request("GET", "/health", headers={"Host": host})
            self.assertEqual(status, 200, host)


# --------------------------------------------------------------------------

class UnitTests(unittest.TestCase):
    def test_parse_ps(self):
        out = (
            "  100 /Applications/Spotify.app/Contents/MacOS/Spotify --remote-debugging-port=9229\n"
            "  101 /Applications/Spotify.app/Contents/Frameworks/Spotify Helper.app/Contents/MacOS/"
            "Spotify Helper --type=renderer --mu=second\n"
            "  200 /Applications/Spotify.app/Contents/MacOS/Spotify --mu=second\n"
            "  201 /Applications/Spotify.app/Contents/MacOS/Spotify\n"
            "  202 /Applications/Spotify.app/Contents/MacOS/SpotifyX --mu=evil\n"
            "  203 /usr/bin/grep /Applications/Spotify.app/Contents/MacOS/Spotify --mu=x\n"
            "  204 /Applications/Spotify.app/Contents/MacOS/Spotify --foo --mu=a20261111120000 --bar\n"
            "garbage\n\n")
        self.assertEqual(sah.parse_ps_output(out), [
            (100, "default"), (200, "second"), (201, "default"), (204, "a20261111120000")])

    def test_launch_command(self):
        with mock.patch.object(sah.subprocess, "run") as run:
            sah.launch_spotify("default")
            sah.launch_spotify("second")
        self.assertEqual(run.call_args_list[0][0][0], ["open", "-n", "-a", "/Applications/Spotify.app"])
        self.assertEqual(run.call_args_list[1][0][0], ["open", "-n", "-a", "/Applications/Spotify.app",
                                                       "--args", "--mu=second"])

    def test_ps_command(self):
        res = mock.Mock(stdout=b"  7 /Applications/Spotify.app/Contents/MacOS/Spotify --mu=x\n")
        with mock.patch.object(sah.subprocess, "run", return_value=res) as run:
            self.assertEqual(sah.list_spotify_processes(), [(7, "x")])
        self.assertEqual(run.call_args[0][0], ["ps", "-axww", "-o", "pid=,args="])

    def test_canonical_username_only(self):
        self.assertEqual(sah.canonical_username_from_bytes(DEFAULT_PREFS), "alice")
        self.assertEqual(sah.canonical_username_from_bytes(b'autologin.username="u"\n'), None)
        self.assertEqual(sah.canonical_username_from_bytes(
            b'autologin.canonical_username="a \\"b\\""\r\n'), 'a "b"')
        self.assertIsNone(sah.canonical_username_from_bytes(b'autologin.canonical_username=""\n'))

    def test_window_merge(self):
        lines, nl = sah.split_lines(SECOND_PREFS)
        merged = sah.merge_window_lines(lines, [b"app.window.position.x=1"])
        self.assertEqual(merged[:3], [b"app.autostart-configured=true", b"app.window.position.x=1",
                                      b'autologin.blob="SECRETBLOB2"'])
        self.assertEqual(sah.join_lines(merged, nl).endswith(b"ui.hide_hpto=true"), True)
        # no window lines yet -> alphabetical position
        self.assertEqual(sah.merge_window_lines([b"app.a=1", b"core.b=2"], [b"app.window.state=1"]),
                         [b"app.a=1", b"app.window.state=1", b"core.b=2"])
        self.assertEqual(sah.merge_window_lines([], [b"app.window.state=1"]), [b"app.window.state=1"])
        for ln, exp in ((b"app.window.position.x=1", True), (b"app.window.state=2", True),
                        (b"app.window.state.saved=true", True), (b"app.window.stateful=1", False),
                        (b"app.windows.position.x=1", False), (b'autologin.blob="x"', False)):
            self.assertEqual(sah.is_window_line(ln), exp, ln)

    def test_roundtrip_byte_identical(self):
        for data in (DEFAULT_PREFS, SECOND_PREFS, b"", b"\n\n", b"a=1\r\nb=2"):
            lines, nl = sah.split_lines(data)
            self.assertEqual(sah.join_lines(lines, nl), data)

    def test_new_profile_id(self):
        pid = sah.new_profile_id()
        self.assertRegex(pid, r"^a\d{14}$")
        self.assertTrue(sah.is_valid_profile_id(pid))


class WindowCopyTests(Base):
    def test_copy_only_into_existing_prefs_and_atomic(self):
        self.assertTrue(self.helper.copy_window_position("default", "second"))
        self.assertFalse(self.helper.copy_window_position("default", "second"))  # unchanged
        self.assertFalse(self.helper.copy_window_position("default", "doesnotexist"))
        self.assertFalse(os.path.exists(self.helper.profile_dir("doesnotexist")))
        self.assertFalse(self.helper.copy_window_position(None, "second"))
        write(self.prefs("nowin"), b"a=1\n")
        self.assertFalse(self.helper.copy_window_position("nowin", "second"))
        leftovers = [n for n in os.listdir(self.helper.profile_dir("second")) if ".tmp-" in n]
        self.assertEqual(leftovers, [])
        # autologin lines untouched (never copied)
        data = read(self.prefs("second"))
        self.assertIn(b'autologin.blob="SECRETBLOB2"', data)
        self.assertNotIn(b"SECRETBLOB1", data)


class IdleTests(Base):
    def _serve(self, idle):
        server = sah.make_server(self.helper, port=0, idle_timeout=idle)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        server.start_idle_watch()
        return server, t

    def test_idle_exit(self):
        server, t = self._serve(0.3)
        try:
            port = server.server_address[1]
            for _ in range(3):  # requests keep it alive
                time.sleep(0.15)
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/health")
                self.assertEqual(c.getresponse().status, 200)
                c.close()
            self.assertTrue(t.is_alive())
            t.join(3)
            self.assertFalse(t.is_alive())
            self.assertTrue(server.idle_exited)
        finally:
            server.server_close()

    def test_no_idle_exit_while_job_running(self):
        self.assertTrue(self.helper.switch_lock.acquire(blocking=False))
        server, t = self._serve(0.2)
        try:
            time.sleep(0.6)
            self.assertTrue(t.is_alive())
            self.helper.switch_lock.release()
            t.join(3)
            self.assertFalse(t.is_alive())
        finally:
            if self.helper.switch_lock.locked():
                self.helper.switch_lock.release()
            server.server_close()

    def test_idle_env(self):
        self.assertEqual(sah.idle_from_env(), 60.0)
        os.environ["SPOTIFY_HELPER_IDLE"] = "5"
        self.assertEqual(sah.idle_from_env(), 5.0)
        os.environ["SPOTIFY_HELPER_IDLE"] = "0"
        self.assertEqual(sah.idle_from_env(), 0.0)
        server = sah.make_server(self.helper, port=0, idle_timeout=0)
        self.assertIsNone(server.start_idle_watch())
        server.server_close()


class SocketActivationTests(unittest.TestCase):
    def setUp(self):
        self._env = os.environ.pop("SPOTIFY_HELPER_NO_LAUNCHD", None)

    def tearDown(self):
        os.environ.pop("SPOTIFY_HELPER_NO_LAUNCHD", None)
        if self._env is not None:
            os.environ["SPOTIFY_HELPER_NO_LAUNCHD"] = self._env

    def test_real_call_outside_launchd_raises(self):
        with self.assertRaises(OSError):
            sah.launchd_activate_socket("Listeners-does-not-exist")

    def test_fallback_on_activation_error(self):
        with mock.patch.object(sah, "launchd_activate_socket",
                               side_effect=OSError(errno.ESRCH, "no such process")):
            sock, why = sah.obtain_listen_socket()
        self.assertIsNone(sock)
        self.assertIn("no launchd socket", why)

    def test_fallback_on_env(self):
        os.environ["SPOTIFY_HELPER_NO_LAUNCHD"] = "1"
        with mock.patch.object(sah, "launchd_activate_socket", side_effect=_forbidden):
            sock, why = sah.obtain_listen_socket()
        self.assertIsNone(sock)

    def test_activated_socket_is_served(self):
        tmp = tempfile.mkdtemp(prefix="spotify-helper-sock-")
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = listener.getsockname()[1]
        fd = os.dup(listener.fileno())
        listener.close()  # like launchd: only our fd stays
        try:
            with mock.patch.object(sah, "launchd_activate_socket", return_value=[fd]) as act:
                sock, why = sah.obtain_listen_socket()
            act.assert_called_once_with("Listeners")
            self.assertEqual(why, "launchd socket activation")
            helper = sah.Helper(os.path.join(tmp, "as"), os.path.join(tmp, "c"),
                                os.path.join(tmp, "ma"), os.path.join(tmp, "t"))
            helper.list_processes = lambda: []
            server = sah.make_server(helper, sock=sock)
            self.assertEqual(server.server_address[1], port)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            try:
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                c.request("GET", "/health")
                r = c.getresponse()
                self.assertEqual(json.loads(r.read()), {"ok": True, "version": "2.0.0"})
                c.close()
            finally:
                server.shutdown()
                server.server_close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class SubprocessSmokeTest(unittest.TestCase):
    """Runs the real script as a process (outside launchd -> activation fails ->
    binds itself), DRYRUN, fake dirs, short idle timeout -> exits by itself."""

    def test_script_fallback_bind_and_idle_exit(self):
        tmp = tempfile.mkdtemp(prefix="spotify-helper-proc-")
        try:
            appsupport = os.path.join(tmp, "as")
            write(os.path.join(appsupport, "Spotify", "prefs"), DEFAULT_PREFS)
            write(os.path.join(appsupport, "Spotify-second", "prefs"), SECOND_PREFS)
            write(os.path.join(appsupport, "Spotify-Accounts", "prefs"), DEFAULT_PREFS)
            ma = os.path.join(tmp, "ma")
            port = free_port()
            env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
            env.update(SPOTIFY_APPSUPPORT=appsupport, SPOTIFY_CACHES=os.path.join(tmp, "c"),
                       SPOTIFY_MA_DIR=ma, SPOTIFY_TRASH=os.path.join(tmp, "t"),
                       SPOTIFY_HELPER_DRYRUN="1", SPOTIFY_HELPER_PORT=str(port),
                       SPOTIFY_HELPER_IDLE="1.5")
            proc = subprocess.Popen([sys.executable, os.path.join(HERE, "spotify_accounts_helper.py")],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.time() + 10
                while True:
                    try:
                        c = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
                        c.request("GET", "/health")
                        self.assertEqual(json.loads(c.getresponse().read()),
                                         {"ok": True, "version": "2.0.0"})
                        c.close()
                        break
                    except OSError:
                        self.assertLess(time.time(), deadline)
                        time.sleep(0.1)
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", "/accounts")
                accs = json.loads(c.getresponse().read())["accounts"]
                c.close()
                self.assertEqual(sorted(a["profile"] for a in accs), ["default", "second"])
                self.assertEqual(proc.wait(10), 0)  # idle exit
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(10)
                proc.stdout.close()
                proc.stderr.close()
            self.assertEqual(stat.S_IMODE(os.stat(ma).st_mode), 0o700)
            logdata = read(os.path.join(ma, "helper.log"))
            self.assertIn(b"own bind, no launchd socket", logdata)
            self.assertIn(b"idle for", logdata)
            for secret in (b"SECRETBLOB", b"SECRETCRED"):
                self.assertNotIn(secret, logdata)
            self.assertFalse(os.path.exists(os.path.join(ma, "profiles.json")))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
