"""Your hold-dictate account. You sign in with Google once on the settings page; after that the app asks
the server about every 10 minutes whether the account is still there and whether Premium is on.

Signing in: the settings page opens <site>/api/auth/start with this server's port and a random state. After
Google, the site sends the browser back to http://127.0.0.1:<port>/auth with a one-time code, which is
traded for the account token here. The token is kept in account.json next to the settings, readable only by you.
"""
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import __version__, config as cfgmod

SITE = os.environ.get("HOLD_DICTATE_SITE", "https://hold-dictate.vercel.app").rstrip("/")
CHECK_EVERY = 10 * 60  # seconds between checks with the server while the app runs
STATE_SECONDS = 10 * 60  # how long a started sign-in may take


def account_path() -> Path:
    return cfgmod.config_dir() / "account.json"


class Account:
    def __init__(self, path: Path | None = None, site: str = SITE):
        self.path = path or account_path()
        self.site = site
        self.pending: dict[str, float] = {}  # sign-in state -> when it expires
        self.lock = threading.Lock()
        self.checking = False

    # ---------- the saved account ----------

    def data(self) -> dict:
        """Read fresh each time: the stand-alone settings page and the running app share the file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) and isinstance(data.get("token"), str) and data["token"] else {}
        except Exception:
            return {}

    def signed_in(self) -> bool:
        return bool(self.data())

    def public(self) -> dict | None:
        """What the settings page may show. Never the token."""
        data = self.data()
        if not data:
            return None
        return {"email": str(data.get("email", "")), "name": str(data.get("name", "")), "premium": bool(data.get("premium"))}

    def premium(self) -> bool:
        return bool(self.data().get("premium"))

    def _write(self, data: dict):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        if sys.platform != "win32":
            tmp.chmod(0o600)  # holds the account token
        os.replace(tmp, self.path)

    def _clear(self):
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # ---------- talking to the site ----------

    def _request(self, path: str, body: dict | None = None, token: str = "", timeout: float = 8.0) -> tuple[int, dict]:
        headers = {"User-Agent": f"hold-dictate/{__version__}", "Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(self.site + path, data=data, headers=headers, method="POST" if body is not None else "GET")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.status, json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}

    def begin(self, port: int) -> str:
        """The address that starts a Google sign-in which comes back to this computer's settings page."""
        state = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.pending = {s: t for s, t in self.pending.items() if t > now}
            self.pending[state] = now + STATE_SECONDS
        return f"{self.site}/api/auth/start?" + urllib.parse.urlencode({"to": "app", "port": port, "state": state})

    def finish(self, code: str, state: str) -> str:
        """Trades the one-time code for the account token. Returns '' when signed in, else what went wrong."""
        with self.lock:
            expires = self.pending.pop(state, 0) if state else 0
        if expires < time.time():
            return "This sign-in link is old or wasn't started here. Go back to the settings and sign in again."
        if not code:
            return "Google didn't finish the sign-in. Go back to the settings and try again."
        try:
            status, reply = self._request("/api/auth/exchange", {"code": code})
        except Exception:
            return "Couldn't reach the hold-dictate server. Check the internet connection and try again."
        if status == 403:
            return "This account is blocked."
        if status != 200 or not isinstance(reply.get("token"), str):
            return "The sign-in didn't go through. Go back to the settings and try again."
        user = reply.get("user") or {}
        self._write({"token": reply["token"], "email": user.get("email", ""), "name": user.get("name", ""),
                     "premium": bool(user.get("premium")), "checked": time.time()})
        return ""

    def check(self, max_age: float = CHECK_EVERY, timeout: float = 8.0) -> bool:
        """Asks the server about the account when the last answer is older than max_age. Signed out by
        the server (or blocked): forget the account. Server unreachable: keep what we know."""
        data = self.data()
        if not data or time.time() - float(data.get("checked") or 0) < max_age:
            return bool(data)
        try:
            status, reply = self._request("/api/me", token=data["token"], timeout=timeout)
        except Exception:
            return True
        if status == 401:
            if self.data().get("token") == data["token"]:  # not replaced by a new sign-in meanwhile
                self._clear()
            return False
        if status == 200:
            data.update(email=reply.get("email", data.get("email", "")), name=reply.get("name", data.get("name", "")),
                        premium=bool(reply.get("premium")), checked=time.time())
            if self.data().get("token") == data["token"]:
                self._write(data)
        return True

    def tick(self):
        """Called often by the app; checks in the background about every 10 minutes."""
        if self.checking:
            return
        data = self.data()
        if not data or time.time() - float(data.get("checked") or 0) < CHECK_EVERY:
            return
        self.checking = True

        def run():
            try:
                self.check()
            finally:
                self.checking = False
        threading.Thread(target=run, daemon=True).start()

    def sign_out(self):
        token = self.data().get("token", "")
        self._clear()
        if token:
            try:
                self._request("/api/auth/logout", {}, token=token, timeout=5)
            except Exception:
                pass  # the token is gone from this computer; the server forgets it within a year anyway
