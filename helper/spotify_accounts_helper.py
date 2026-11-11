#!/usr/bin/env python3
"""Local helper service for the Spotify multi-account switcher (macOS), v2.

Binding spec: docs/ARCHITECTURE.md.

One Spotify profile per account: every account has its own Spotify data
directory (``Spotify-<id>``, started with ``--mu=<id>``); ``default`` is the
plain ``Spotify`` directory. A switch starts the target profile, waits until
the Spicetify extension in it calls ``/register`` and only then terminates the
old instance. ``autologin.*`` values are never read, copied or written - the
only exception is reading ``autologin.canonical_username`` to map a profile to
an account.

Python 3.9 standard library only. Listens on 127.0.0.1 only. Normally started
by launchd via socket activation (``Sockets`` -> ``Listeners``) and exits by
itself after ``SPOTIFY_HELPER_IDLE`` seconds without requests and without a
running switch. Without launchd it binds the port itself.

Environment variables (mainly for tests):
  SPOTIFY_APPSUPPORT          instead of ~/Library/Application Support
  SPOTIFY_CACHES              instead of ~/Library/Caches
  SPOTIFY_MA_DIR              data directory of this helper
  SPOTIFY_TRASH               instead of ~/.Trash
  SPOTIFY_HELPER_DRYRUN=1     never start/terminate Spotify or open a browser, only log
  SPOTIFY_HELPER_PORT         TCP port when binding itself (default 48655)
  SPOTIFY_HELPER_OPEN_BROWSER=0  never open the browser on /add
  SPOTIFY_HELPER_IDLE=<s>     idle exit after <s> seconds (default 60, 0 = never)
  SPOTIFY_HELPER_NO_LAUNCHD=1 do not try launchd socket activation
"""

import ctypes
import ctypes.util
import datetime
import errno
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.0.0"
DEFAULT_PORT = 48655
DEFAULT_IDLE = 60.0
LAUNCHD_SOCKET_NAME = "Listeners"
ALLOWED_ORIGIN = "https://xpui.app.spotify.com"
SPOTIFY_APP = "/Applications/Spotify.app"
SPOTIFY_BINARY = "/Applications/Spotify.app/Contents/MacOS/Spotify"
DEFAULT_PROFILE = "default"
MAX_BODY = 64 * 1024

SWITCH_TIMEOUT = 20.0       # wait for /register of the target on /switch
ADD_TIMEOUT = 600.0         # wait for /register of the new profile on /add
TERM_TIMEOUT = 8.0          # SIGTERM -> SIGKILL
POLL_INTERVAL = 1.0
BROWSER_DELAY = 2.0

LOGIN_STATUS_URL = "https://accounts.spotify.com/de/status"
LOGIN_PAGE_URL = "https://accounts.spotify.com/de/login?allow_password=1&continue="
LOGOUT_URL = "https://accounts.spotify.com/logout?continue="

PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
CANONICAL_PREFIX = b"autologin.canonical_username="
# app.window.position.* and app.window.state / app.window.state.*
WINDOW_LINE_RE = re.compile(rb"^app\.window\.(?:position\.[^=]*|state(?:\.[^=]*)?)=")

log = logging.getLogger("spotify-accounts-helper")


class HelperError(Exception):
    """Error that maps to an HTTP error response."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = int(status)
        self.message = message


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def is_valid_profile_id(value):
    return isinstance(value, str) and bool(PROFILE_ID_RE.match(value))


def validate_profile(value):
    if not isinstance(value, str) or not value:
        raise HelperError(HTTPStatus.BAD_REQUEST, "profile must be a non-empty string")
    if not PROFILE_ID_RE.match(value):
        raise HelperError(HTTPStatus.BAD_REQUEST, "invalid profile id")
    return value


def validate_username(value):
    if not isinstance(value, str) or not value:
        raise HelperError(HTTPStatus.BAD_REQUEST, "username must be a non-empty string")
    if len(value) > 256:
        raise HelperError(HTTPStatus.BAD_REQUEST, "username too long")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in value):
        raise HelperError(HTTPStatus.BAD_REQUEST, "invalid username")
    return value


def _optional_string(body, key, limit):
    value = body.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > limit:
        raise HelperError(HTTPStatus.BAD_REQUEST, "%s must be a string or null" % key)
    return value or None


# --------------------------------------------------------------------------
# prefs handling (line based, byte exact)
# --------------------------------------------------------------------------

def split_lines(data):
    """Split bytes on b'\\n'. Returns (lines, ends_with_newline)."""
    if not data:
        return [], True
    parts = data.split(b"\n")
    if parts[-1] == b"":
        return parts[:-1], True
    return parts, False


def join_lines(lines, ends_with_newline=True):
    if not lines:
        return b""
    out = b"\n".join(lines)
    if ends_with_newline:
        out += b"\n"
    return out


def parse_value(line):
    """Return the decoded value of a 'key="value"' line (quotes/escapes removed)."""
    _, sep, raw = line.partition(b"=")
    if not sep:
        return None
    text = raw.decode("utf-8", errors="replace").strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        inner = text[1:-1]
        out = []
        i = 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                out.append(inner[i + 1])
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    return text


def canonical_username_from_bytes(data):
    """Only autologin.canonical_username is ever looked at."""
    for line in data.split(b"\n"):
        if line.startswith(CANONICAL_PREFIX):
            return parse_value(line.rstrip(b"\r")) or None
    return None


def is_window_line(line):
    return bool(WINDOW_LINE_RE.match(line))


def merge_window_lines(target_lines, window_lines):
    """Return target_lines with all its window lines replaced by window_lines.

    Every other line stays byte-identical and in order. The new block goes
    where the first old window line was, otherwise before the first line that
    sorts after it, otherwise at the end."""
    insert_at = None
    kept = []
    for ln in target_lines:
        if is_window_line(ln):
            if insert_at is None:
                insert_at = len(kept)
            continue
        kept.append(ln)
    if insert_at is None:
        insert_at = len(kept)
        anchor = window_lines[0] if window_lines else b"app.window."
        for idx, ln in enumerate(kept):
            stripped = ln.strip()
            if not stripped or stripped.startswith(b"#"):
                continue
            if ln > anchor:
                insert_at = idx
                break
    return kept[:insert_at] + list(window_lines) + kept[insert_at:]


def atomic_write(path, data, mode):
    """Write bytes atomically (tmp file in same dir + rename) with given mode."""
    directory = os.path.dirname(path) or "."
    tmp = os.path.join(directory, ".%s.tmp-%d-%d" % (os.path.basename(path), os.getpid(),
                                                        threading.get_ident()))
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def build_login_url():
    """URL that first clears the browser's Spotify web session, then shows the
    password login page, then the account status page (continue double-encoded)."""
    inner = LOGIN_PAGE_URL + urllib.parse.quote(LOGIN_STATUS_URL, safe="")
    return LOGOUT_URL + urllib.parse.quote(inner, safe="")


def utc_now_iso():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()


def new_profile_id(now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    return "a" + now.strftime("%Y%m%d%H%M%S")


# --------------------------------------------------------------------------
# Spotify processes (real implementations; the Helper holds replaceable refs)
# --------------------------------------------------------------------------

def parse_ps_output(text):
    """Parse `ps -axo pid=,args=` output -> list of (pid, profile) for Spotify
    main processes (program exactly SPOTIFY_BINARY). --mu=<id> -> <id>, else
    'default'."""
    result = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        pid_s, _, args = raw.partition(" ")
        args = args.lstrip()
        if args != SPOTIFY_BINARY and not args.startswith(SPOTIFY_BINARY + " "):
            continue
        try:
            pid = int(pid_s)
        except ValueError:
            continue
        profile = DEFAULT_PROFILE
        for token in args[len(SPOTIFY_BINARY):].split():
            if token.startswith("--mu="):
                profile = token[len("--mu="):]
        result.append((pid, profile))
    return result


def list_spotify_processes():
    try:
        out = subprocess.run(["ps", "-axww", "-o", "pid=,args="], capture_output=True,
                             check=False, timeout=10).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("ps failed: %s", exc)
        return []
    return parse_ps_output(out.decode("utf-8", errors="replace"))


def launch_spotify(profile):
    cmd = ["open", "-n", "-a", SPOTIFY_APP]
    if profile != DEFAULT_PROFILE:
        cmd += ["--args", "--mu=" + profile]
    log.info("starting Spotify profile %r: %s", profile, " ".join(cmd))
    subprocess.run(cmd, capture_output=True, check=False, timeout=30)


def kill_pid(pid, sig):
    os.kill(pid, sig)


def open_browser(url):
    log.info("opening default browser: %s", url)
    subprocess.run(["open", url], capture_output=True, check=False, timeout=30)


# --------------------------------------------------------------------------
# Helper state
# --------------------------------------------------------------------------

class Job:
    def __init__(self, kind, target, old_profile, old_pids):
        self.kind = kind                # "switch" | "add"
        self.target = target
        self.old_profile = old_profile
        self.old_pids = list(old_pids)
        self.ready = threading.Event()  # set by /register of the target
        self.result = None              # done | timeout | aborted | error
        self.thread = None


class Helper:
    def __init__(self, appsupport, caches, ma_dir, trash_dir, dryrun=False,
                 open_browser_on_add=True):
        self.appsupport = appsupport
        self.caches = caches
        self.ma_dir = ma_dir
        self.trash_dir = trash_dir
        self.profiles_file = os.path.join(ma_dir, "profiles.json")
        self.dryrun = dryrun
        self.open_browser_on_add = open_browser_on_add
        self.switch_timeout = SWITCH_TIMEOUT
        self.add_timeout = ADD_TIMEOUT
        self.term_timeout = TERM_TIMEOUT
        self.poll_interval = POLL_INTERVAL
        self.browser_delay = BROWSER_DELAY
        # replaceable for tests
        self.list_processes = list_spotify_processes
        self.launch = launch_spotify
        self.kill = kill_pid
        self.open_browser = open_browser
        self.now = lambda: datetime.datetime.now(datetime.timezone.utc)
        # Only one switch/add at a time. Acquired by the request handler,
        # released by the background worker.
        self.switch_lock = threading.Lock()
        self.data_lock = threading.RLock()
        self.job = None
        self.last_job = None
        self.registered_at = {}   # profile -> monotonic time of last /register

    @classmethod
    def from_env(cls):
        home = os.path.expanduser("~")
        appsupport = os.environ.get("SPOTIFY_APPSUPPORT") or os.path.join(
            home, "Library", "Application Support")
        caches = os.environ.get("SPOTIFY_CACHES") or os.path.join(home, "Library", "Caches")
        ma_dir = os.environ.get("SPOTIFY_MA_DIR") or os.path.join(
            home, "Library", "Application Support", "spotify-multiaccount")
        trash = os.environ.get("SPOTIFY_TRASH") or os.path.join(home, ".Trash")
        dryrun = os.environ.get("SPOTIFY_HELPER_DRYRUN") == "1"
        browser = os.environ.get("SPOTIFY_HELPER_OPEN_BROWSER", "1") != "0"
        return cls(appsupport, caches, ma_dir, trash, dryrun, browser)

    # ---- paths ----------------------------------------------------------------

    def ensure_dirs(self):
        os.makedirs(self.ma_dir, mode=0o700, exist_ok=True)
        os.chmod(self.ma_dir, 0o700)

    def profile_dir(self, profile):
        if profile == DEFAULT_PROFILE:
            return os.path.join(self.appsupport, "Spotify")
        return os.path.join(self.appsupport, "Spotify-" + profile)

    def cache_dir(self, profile):
        if profile == DEFAULT_PROFILE:
            return os.path.join(self.caches, "com.spotify.client")
        return os.path.join(self.caches, "com.spotify.client-" + profile)

    def prefs_path(self, profile):
        return os.path.join(self.profile_dir(profile), "prefs")

    # ---- profiles.json --------------------------------------------------------

    def load_profiles(self):
        try:
            with open(self.profiles_file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            log.error("profiles.json unreadable: %s", exc)
            return {}
        profiles = data.get("profiles") if isinstance(data, dict) else None
        if not isinstance(profiles, dict):
            return {}
        return {k: v for k, v in profiles.items()
                if is_valid_profile_id(k) and isinstance(v, dict)}

    def save_profiles(self, profiles):
        self.ensure_dirs()
        payload = json.dumps({"profiles": profiles}, indent=2, ensure_ascii=False,
                             sort_keys=True).encode("utf-8") + b"\n"
        atomic_write(self.profiles_file, payload, 0o600)

    # ---- scan -----------------------------------------------------------------

    def scan_profiles(self):
        """Profiles found on disk: 'default' (Spotify/prefs) and Spotify-<id>/prefs."""
        found = []
        if os.path.isfile(self.prefs_path(DEFAULT_PROFILE)):
            found.append(DEFAULT_PROFILE)
        try:
            names = sorted(os.listdir(self.appsupport))
        except OSError:
            names = []
        for name in names:
            if not name.startswith("Spotify-"):
                continue
            pid = name[len("Spotify-"):]
            if pid == DEFAULT_PROFILE or not PROFILE_ID_RE.match(pid):
                continue
            if os.path.isfile(os.path.join(self.appsupport, name, "prefs")):
                found.append(pid)
        return found

    def known_profiles(self):
        profiles = self.load_profiles()
        known = list(self.scan_profiles())
        for pid in sorted(profiles):
            if pid not in known:
                known.append(pid)
        return known, profiles

    def canonical_username(self, profile):
        try:
            with open(self.prefs_path(profile), "rb") as fh:
                return canonical_username_from_bytes(fh.read())
        except OSError:
            return None

    # ---- processes ------------------------------------------------------------

    def running(self):
        """-> dict profile -> sorted list of main PIDs."""
        result = {}
        for pid, profile in self.list_processes():
            result.setdefault(profile, []).append(pid)
        for pids in result.values():
            pids.sort()
        return result

    def current_profile(self, running=None):
        running = self.running() if running is None else running
        if not running:
            return None
        if len(running) == 1:
            return next(iter(running))
        registered = [(self.registered_at[p], p) for p in running if p in self.registered_at]
        if registered:
            return max(registered)[1]
        return min(running, key=lambda p: running[p][0])  # oldest instance

    # ---- API: accounts / register / remove -----------------------------------

    def list_accounts(self):
        with self.data_lock:
            running = self.running()
            current = self.current_profile(running)
            known, meta = self.known_profiles()
            result = []
            for pid in known:
                m = meta.get(pid) or {}
                canonical = self.canonical_username(pid)
                username = canonical or (m.get("username") if isinstance(m.get("username"), str) else None)
                display = m.get("displayName") if isinstance(m.get("displayName"), str) else None
                image = m.get("imageUrl") if isinstance(m.get("imageUrl"), str) else None
                result.append({
                    "profile": pid,
                    "username": username,
                    "displayName": display or username,
                    "imageUrl": image,
                    "running": pid in running,
                    "current": pid == current,
                    "loggedIn": canonical is not None,
                })
        result.sort(key=lambda a: (not a["current"], (a["displayName"] or "~").casefold(),
                                   a["profile"]))
        return {"accounts": result}

    def resolve_profile(self, username, running):
        """Map an account (canonical username) to a profile id."""
        known, meta = self.known_profiles()
        job = self.job
        pending = job.target if job is not None else None

        def pick(candidates):
            if not candidates:
                return None
            if pending in candidates and pending in running:
                return pending
            for pid in candidates:
                if pid in running:
                    return pid
            return candidates[0]

        # 1. autologin.canonical_username in the profile's prefs (authoritative)
        hit = pick([p for p in known if self.canonical_username(p) == username])
        if hit:
            return hit
        # 2. Spotify may write the login to prefs late: a profile that is running
        #    but whose prefs has no login yet. Prefer the pending switch/add target,
        #    then one remembered with this username, then the only such profile.
        fresh = [p for p in running if p in known or p == pending]
        fresh = [p for p in fresh if self.canonical_username(p) is None]
        if pending in fresh:
            return pending
        remembered = [p for p in fresh if (meta.get(p) or {}).get("username") == username]
        if remembered:
            return remembered[0]
        if len(fresh) == 1:
            return fresh[0]
        return None

    def register(self, body):
        username = validate_username(body.get("username"))
        display = _optional_string(body, "displayName", 512)
        image = _optional_string(body, "imageUrl", 4096)
        with self.data_lock:
            running = self.running()
            profile = self.resolve_profile(username, running)
            if profile is None:
                log.warning("register: no profile found for %r", username)
                raise HelperError(HTTPStatus.NOT_FOUND, "no profile found for this account")
            profiles = self.load_profiles()
            entry = dict(profiles.get(profile) or {})
            now = utc_now_iso()
            entry.setdefault("createdAt", now)
            entry.update({"username": username, "displayName": display or username,
                          "imageUrl": image, "updatedAt": now})
            profiles[profile] = entry
            self.save_profiles(profiles)
            self.registered_at[profile] = time.monotonic()
            job = self.job
            if job is not None and job.target == profile:
                job.ready.set()
        log.info("registered %r as profile %r", username, profile)
        return {"ok": True, "profile": profile}

    def _trash_target(self, name):
        stamp = self.now().strftime("%Y%m%d-%H%M%S")
        base = os.path.join(self.trash_dir, "%s-%s" % (name, stamp))
        dest, n = base, 1
        while os.path.lexists(dest):
            n += 1
            dest = "%s-%d" % (base, n)
        return dest

    def move_to_trash(self, path):
        if not os.path.lexists(path):
            return None
        os.makedirs(self.trash_dir, mode=0o700, exist_ok=True)
        dest = self._trash_target(os.path.basename(path))
        try:
            os.rename(path, dest)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise
            shutil.move(path, dest)
        log.info("moved %s to %s", path, dest)
        return dest

    def remove(self, body):
        profile = validate_profile(body.get("profile"))
        if profile == DEFAULT_PROFILE:
            raise HelperError(HTTPStatus.CONFLICT, "the default profile cannot be removed")
        if not self.switch_lock.acquire(blocking=False):
            raise HelperError(HTTPStatus.CONFLICT, "a switch is in progress")
        try:
            with self.data_lock:
                known, profiles = self.known_profiles()
                if profile not in known:
                    raise HelperError(HTTPStatus.NOT_FOUND, "unknown profile")
                if profile in self.running():
                    raise HelperError(HTTPStatus.CONFLICT, "profile is running")
                self.move_to_trash(self.profile_dir(profile))
                self.move_to_trash(self.cache_dir(profile))
                if profile in profiles:
                    del profiles[profile]
                    self.save_profiles(profiles)
                self.registered_at.pop(profile, None)
        finally:
            self.switch_lock.release()
        log.info("removed profile %r", profile)
        return {"ok": True}

    # ---- window position -------------------------------------------------------

    def copy_window_position(self, source, target):
        """Copy app.window.position.* / app.window.state* lines from the source
        profile's prefs into the target's prefs (atomic, other lines unchanged).
        The target's prefs is never created."""
        if not source or source == target:
            return False
        try:
            with open(self.prefs_path(source), "rb") as fh:
                src_lines, _ = split_lines(fh.read())
        except OSError:
            return False
        window = [ln for ln in src_lines if is_window_line(ln)]
        if not window:
            return False
        path = self.prefs_path(target)
        with self.data_lock:
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
                mode = os.stat(path).st_mode & 0o777
            except OSError:
                log.info("target prefs %s missing, window position not copied", target)
                return False
            lines, nl = split_lines(data)
            merged = merge_window_lines(lines, window)
            if merged == lines:
                return False
            atomic_write(path, join_lines(merged, nl), mode)
        log.info("copied window position from %r to %r", source, target)
        return True

    # ---- side effects (dry run aware) ----------------------------------------

    def _launch(self, profile):
        if self.dryrun:
            log.info("dry run: not starting Spotify profile %r", profile)
            return
        self.launch(profile)

    def _open_login_page(self, url):
        if self.dryrun:
            log.info("dry run: not opening browser, URL would be %s", url)
            return
        time.sleep(self.browser_delay)  # let the new instance show its login screen first
        self.open_browser(url)

    def _main_pids_of(self, profile):
        return set(self.running().get(profile, []))

    def terminate(self, profile, pids):
        """SIGTERM the given main PIDs of `profile`, SIGKILL after term_timeout.
        A PID is only signalled while it is still a Spotify main process of
        that profile (protects against PID reuse)."""
        targets = [p for p in pids if p in self._main_pids_of(profile)]
        if not targets:
            log.info("old instance %r already gone", profile)
            return
        if self.dryrun:
            log.info("dry run: not terminating %r (pids %s)", profile, targets)
            return
        log.info("terminating old instance %r (pids %s)", profile, targets)
        for pid in targets:
            self._signal(pid, signal.SIGTERM)
        deadline = time.monotonic() + self.term_timeout
        while True:
            alive = [p for p in targets if p in self._main_pids_of(profile)]
            if not alive:
                log.info("old instance %r ended", profile)
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(min(0.25, self.poll_interval))
        log.warning("old instance %r still alive after %.0f s, SIGKILL %s",
                    profile, self.term_timeout, alive)
        for pid in alive:
            self._signal(pid, signal.SIGKILL)

    def _signal(self, pid, sig):
        try:
            self.kill(pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            log.warning("kill %s %s failed: %s", sig.name, pid, exc)

    def _wait_ready(self, job, timeout):
        """Wait for /register of job.target. Gives up early if the target
        instance was seen running and then disappeared (user closed it)."""
        deadline = time.monotonic() + timeout
        seen = False
        missing = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            if job.ready.wait(min(self.poll_interval, remaining)):
                return "ready"
            if job.target in self.running():
                seen, missing = True, 0
            elif seen:
                missing += 1
                if missing >= 2:
                    return "aborted"

    # ---- background jobs --------------------------------------------------------

    def _run_job(self, job, open_browser_flag=False):
        try:
            if job.kind == "switch" and job.target in self.running():
                log.info("target %r is already running", job.target)
                self.registered_at[job.target] = time.monotonic()
                job.ready.set()
            else:
                try:
                    self.copy_window_position(job.old_profile, job.target)
                except Exception:
                    log.exception("copying the window position failed")
                self._launch(job.target)
            if job.kind == "add" and open_browser_flag and self.open_browser_on_add:
                try:
                    self._open_login_page(build_login_url())
                except Exception:
                    log.exception("opening the browser failed")
            if job.kind == "switch" and not job.old_pids:
                job.result = "done"
                return
            timeout = self.add_timeout if job.kind == "add" else self.switch_timeout
            state = self._wait_ready(job, timeout)
            if state != "ready":
                log.warning("%s to %r: %s while waiting for /register, old instance %r kept",
                            job.kind, job.target, state, job.old_profile)
                job.result = state
                return
            if job.old_pids and job.old_profile != job.target:
                self.terminate(job.old_profile, job.old_pids)
            job.result = "done"
            log.info("%s to %r done", job.kind, job.target)
        except Exception:
            job.result = "error"
            log.exception("%s to %r failed", job.kind, job.target)
        finally:
            self.job = None
            self.switch_lock.release()

    def _start_job(self, job, *args):
        t = threading.Thread(target=self._run_job, args=(job,) + args,
                             name="%s-worker" % job.kind, daemon=True)
        job.thread = t
        self.last_job = job
        t.start()
        return t

    def busy(self):
        return self.switch_lock.locked()

    def request_switch(self, body):
        """Return (status, payload, start) - start() runs the background job
        after the response was sent, or None."""
        target = validate_profile(body.get("profile"))
        if not self.switch_lock.acquire(blocking=False):
            raise HelperError(HTTPStatus.CONFLICT, "a switch is already in progress")
        try:
            known, _ = self.known_profiles()
            if target not in known:
                raise HelperError(HTTPStatus.NOT_FOUND, "unknown profile")
            running = self.running()
            current = self.current_profile(running)
            if target == current:
                self.switch_lock.release()
                return HTTPStatus.OK, {"ok": True, "switching": False}, None
            job = Job("switch", target, current, running.get(current, []) if current else [])
            self.job = job
        except BaseException:
            self.switch_lock.release()
            raise
        log.info("switch %r -> %r requested", job.old_profile, target)
        return (HTTPStatus.ACCEPTED, {"ok": True, "switching": True},
                lambda: self._start_job(job))

    def request_add(self, body):
        flag = body.get("openBrowser", True)
        if not isinstance(flag, bool):
            raise HelperError(HTTPStatus.BAD_REQUEST, "openBrowser must be a boolean")
        if not self.switch_lock.acquire(blocking=False):
            raise HelperError(HTTPStatus.CONFLICT, "a switch is already in progress")
        try:
            with self.data_lock:
                known, profiles = self.known_profiles()
                base = new_profile_id(self.now())
                profile, n = base, 1
                while (profile in known or os.path.lexists(self.profile_dir(profile))
                       or os.path.lexists(self.cache_dir(profile))):
                    n += 1
                    profile = "%s-%d" % (base, n)
                now = utc_now_iso()
                profiles[profile] = {"username": None, "displayName": None, "imageUrl": None,
                                     "createdAt": now, "updatedAt": now}
                self.save_profiles(profiles)
            running = self.running()
            current = self.current_profile(running)
            job = Job("add", profile, current, running.get(current, []) if current else [])
            self.job = job
        except BaseException:
            self.switch_lock.release()
            raise
        log.info("add profile %r requested (openBrowser=%s, enabled=%s)", profile, flag,
                 self.open_browser_on_add)
        payload = {"ok": True, "profile": profile, "loginUrl": build_login_url()}
        return HTTPStatus.ACCEPTED, payload, lambda: self._start_job(job, flag)

    def wait_idle(self, timeout=10.0):
        """Wait until no switch/add is running (used by tests)."""
        if self.switch_lock.acquire(timeout=timeout):
            self.switch_lock.release()
            return True
        return False


# --------------------------------------------------------------------------
# HTTP layer
# --------------------------------------------------------------------------

_ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


class Handler(BaseHTTPRequestHandler):
    server_version = "SpotifyAccountsHelper/" + VERSION
    sys_version = ""
    protocol_version = "HTTP/1.1"
    timeout = 10  # idle keep-alive connections must not block the idle exit

    @property
    def helper(self):
        return self.server.helper

    def log_message(self, fmt, *args):  # route access log to logger, no bodies
        log.debug("%s %s", self.address_string(), fmt % args)

    # ---- response helpers ---------------------------------------------------

    def _cors_headers(self):
        if self.headers.get("Origin") == ALLOWED_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
            # Chromium Private Network Access (https page -> 127.0.0.1)
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.send_header("Vary", "Origin")

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(int(status))
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.wfile.flush()

    def _error(self, status, message):
        self._send_json(status, {"ok": False, "error": message})

    def _check_request(self):
        """Origin + Host checks. Returns True if the request may proceed."""
        origin = self.headers.get("Origin")
        if origin is not None and origin != ALLOWED_ORIGIN:
            log.warning("rejected request from origin %r", origin[:200])
            self.close_connection = True  # request body (if any) is not read
            self._error(HTTPStatus.FORBIDDEN, "origin not allowed")
            return False
        host = (self.headers.get("Host") or "").strip().lower()
        hostname = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if host and hostname not in _ALLOWED_HOSTS:
            # DNS-rebinding protection
            log.warning("rejected request with Host %r", host[:200])
            self.close_connection = True
            self._error(HTTPStatus.FORBIDDEN, "host not allowed")
            return False
        return True

    def _read_json_body(self):
        length = self.headers.get("Content-Length")
        try:
            length = int(length) if length else 0
        except ValueError:
            raise HelperError(HTTPStatus.BAD_REQUEST, "invalid Content-Length")
        if length < 0 or length > MAX_BODY:
            raise HelperError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw.strip():
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise HelperError(HTTPStatus.BAD_REQUEST, "invalid JSON")
        if not isinstance(data, dict):
            raise HelperError(HTTPStatus.BAD_REQUEST, "JSON body must be an object")
        return data

    def _path(self):
        return self.path.split("?", 1)[0]

    def _tracked(self, func):
        self.server.request_started()
        try:
            func()
        finally:
            self.server.request_finished()

    # ---- methods ------------------------------------------------------------

    def do_OPTIONS(self):
        self._tracked(self._options)

    def do_GET(self):
        self._tracked(self._get)

    def do_POST(self):
        self._tracked(self._post)

    def _options(self):
        if not self._check_request():
            return
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _get(self):
        if not self._check_request():
            return
        path = self._path()
        try:
            if path == "/health":
                self._send_json(HTTPStatus.OK, {"ok": True, "version": VERSION})
            elif path == "/accounts":
                self._send_json(HTTPStatus.OK, self.helper.list_accounts())
            elif path in ("/register", "/switch", "/add", "/remove"):
                self._error(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except HelperError as exc:
            self._error(exc.status, exc.message)
        except Exception:
            log.exception("GET %s failed", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _post(self):
        if not self._check_request():
            return
        path = self._path()
        start = None
        try:
            body = self._read_json_body()
            if path == "/register":
                status, payload = HTTPStatus.OK, self.helper.register(body)
            elif path == "/remove":
                status, payload = HTTPStatus.OK, self.helper.remove(body)
            elif path == "/switch":
                status, payload, start = self.helper.request_switch(body)
            elif path == "/add":
                status, payload, start = self.helper.request_add(body)
            elif path in ("/health", "/accounts"):
                raise HelperError(HTTPStatus.METHOD_NOT_ALLOWED, "method not allowed")
            else:
                raise HelperError(HTTPStatus.NOT_FOUND, "not found")
        except HelperError as exc:
            self._error(exc.status, exc.message)
            return
        except Exception:
            log.exception("POST %s failed", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")
            return
        try:
            self._send_json(status, payload)
        finally:
            if start:
                start()


class HelperServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, helper, port=DEFAULT_PORT, sock=None, idle_timeout=0):
        self.helper = helper
        self.idle_timeout = float(idle_timeout or 0)
        self.idle_exited = False
        self._activity_lock = threading.Lock()
        self._inflight = 0
        self._last_activity = time.monotonic()
        self._stop_watch = threading.Event()
        if sock is None:
            super().__init__(("127.0.0.1", port), Handler)
        else:
            # socket handed over by launchd: already bound and listening
            super().__init__(("127.0.0.1", 0), Handler, bind_and_activate=False)
            self.socket.close()
            sock.setblocking(True)
            self.socket = sock
            self.server_address = sock.getsockname()[:2]
            self.server_name, self.server_port = self.server_address[:2]

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)  # skip the slow getfqdn()
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, socket.timeout)):
            log.debug("connection from %s ended: %s", client_address, exc)
        else:
            log.exception("error while handling a request from %s", client_address)

    # ---- idle exit ----------------------------------------------------------

    def request_started(self):
        with self._activity_lock:
            self._inflight += 1
            self._last_activity = time.monotonic()

    def request_finished(self):
        with self._activity_lock:
            self._inflight -= 1
            self._last_activity = time.monotonic()

    def is_idle(self):
        with self._activity_lock:
            if self._inflight or self.helper.busy():
                self._last_activity = time.monotonic()
                return False
            return time.monotonic() - self._last_activity >= self.idle_timeout

    def start_idle_watch(self):
        if self.idle_timeout <= 0:
            return None
        t = threading.Thread(target=self._idle_watch, name="idle-watch", daemon=True)
        t.start()
        return t

    def _idle_watch(self):
        interval = max(0.05, min(1.0, self.idle_timeout / 4))
        while not self._stop_watch.wait(interval):
            if self.is_idle():
                log.info("idle for %.0f s, exiting", self.idle_timeout)
                self.idle_exited = True
                self.shutdown()
                return

    def server_close(self):
        self._stop_watch.set()
        super().server_close()


# --------------------------------------------------------------------------
# launchd socket activation
# --------------------------------------------------------------------------

def launchd_activate_socket(name=LAUNCHD_SOCKET_NAME):
    """Return the list of fds launchd created for `name` (launch_activate_socket(3)).
    Raises OSError if not running under launchd or the socket is unknown."""
    libname = ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib"
    lib = ctypes.CDLL(libname, use_errno=True)
    func = getattr(lib, "launch_activate_socket", None)
    if func is None:
        raise OSError(errno.ENOSYS, "launch_activate_socket not available")
    func.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_int)),
                     ctypes.POINTER(ctypes.c_size_t)]
    func.restype = ctypes.c_int
    fds_ptr = ctypes.POINTER(ctypes.c_int)()
    cnt = ctypes.c_size_t(0)
    err = func(name.encode("ascii"), ctypes.byref(fds_ptr), ctypes.byref(cnt))
    if err != 0:
        raise OSError(err, "launch_activate_socket: %s" % os.strerror(err))
    try:
        return [fds_ptr[i] for i in range(cnt.value)]
    finally:
        libc_free = lib.free
        libc_free.argtypes = [ctypes.c_void_p]
        libc_free(ctypes.cast(fds_ptr, ctypes.c_void_p))


def obtain_listen_socket():
    """-> (socket or None, description). None means: bind yourself."""
    if os.environ.get("SPOTIFY_HELPER_NO_LAUNCHD") == "1":
        return None, "SPOTIFY_HELPER_NO_LAUNCHD=1"
    try:
        fds = launchd_activate_socket(LAUNCHD_SOCKET_NAME)
    except (OSError, AttributeError) as exc:
        return None, "no launchd socket (%s)" % exc
    if not fds:
        return None, "launchd returned no sockets"
    sock = socket.socket(fileno=fds[0])
    for extra in fds[1:]:
        try:
            os.close(extra)
        except OSError:
            pass
    return sock, "launchd socket activation"


def setup_logging(ma_dir, also_stderr=False):
    os.makedirs(ma_dir, mode=0o700, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        os.path.join(ma_dir, "helper.log"), maxBytes=1_000_000, backupCount=2,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s"))
    log.addHandler(handler)
    if also_stderr:
        log.addHandler(logging.StreamHandler(sys.stderr))
    log.setLevel(logging.INFO)


def idle_from_env():
    raw = os.environ.get("SPOTIFY_HELPER_IDLE")
    if raw is None or raw == "":
        return DEFAULT_IDLE
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_IDLE


def make_server(helper=None, port=None, sock=None, idle_timeout=0):
    helper = helper or Helper.from_env()
    if port is None:
        port = int(os.environ.get("SPOTIFY_HELPER_PORT") or DEFAULT_PORT)
    helper.ensure_dirs()
    return HelperServer(helper, port=port, sock=sock, idle_timeout=idle_timeout)


def main():
    os.umask(0o077)
    helper = Helper.from_env()
    helper.ensure_dirs()
    setup_logging(helper.ma_dir)
    sock, source = obtain_listen_socket()
    server = make_server(helper, sock=sock, idle_timeout=idle_from_env())
    log.info("helper %s listening on %s:%d via %s (dryrun=%s, idle=%ss, profiles=%s)",
             VERSION, server.server_address[0], server.server_address[1],
             source if sock is not None else "own bind, " + source, helper.dryrun,
             server.idle_timeout, helper.scan_profiles())

    def _term(signum, frame):
        log.info("received signal %d, shutting down", signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _term)
    server.start_idle_watch()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("helper stopped")


if __name__ == "__main__":
    main()
