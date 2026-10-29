import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hold_dictate.account import Account
from hold_dictate.config import Config
from hold_dictate.settings_ui import SettingsServer


class FakeSite:
    """The hold-dictate site's account API: exchange, me and logout."""

    def __init__(self):
        self.requests = []
        self.me_status, self.premium = 200, False
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append(("GET", self.path, self.headers.get("Authorization")))
                if self.path == "/api/me":
                    return self._reply(outer.me_status, {"email": "you@example.com", "name": "You", "premium": outer.premium})
                self._reply(404, {})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append(("POST", self.path, self.headers.get("Authorization"), body))
                if self.path == "/api/auth/exchange":
                    if body.get("code") == "good-code":
                        return self._reply(200, {"token": "new-token", "user": {"email": "you@example.com", "name": "You", "premium": True}})
                    return self._reply(400, {"error": "code"})
                self._reply(200, {"ok": True})

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


@pytest.fixture
def site():
    fake = FakeSite()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def signed_out(signed_in):
    signed_in.unlink()


def serve(site):
    return SettingsServer(Config(api_key="gsk_secret1234"), port=0, account=Account(site=site.url)).start()


def get(server, path, token=True):
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", headers={"X-Token": server.token} if token else {})

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a):
            return None
    try:
        with urllib.request.build_opener(NoRedirect).open(request) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def post(server, path, body=None):
    data = json.dumps(body or {}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, headers={"X-Token": server.token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_signed_out_locks_the_settings(site, signed_out):
    s = serve(site)
    try:
        status, state = get(s, "/api/state")[0], json.loads(get(s, "/api/state")[2])
        assert status == 200 and state["account"] is None
        assert post(s, "/api/config", {"beeps": True}) == (400, {"error": "sign in first"})
        assert post(s, "/api/test", {"api_key": "gsk_x"})[0] == 400
    finally:
        s.stop()


def test_sign_in_round_trip(site, signed_out, signed_in):
    s = serve(site)
    try:
        url = post(s, "/api/account/signin")[1]["url"]
        assert url.startswith(site.url + "/api/auth/start?to=app&port=" + str(s.port) + "&state=")
        state = url.rsplit("state=", 1)[1]
        status, headers, _ = get(s, f"/auth?code=good-code&state={state}", token=False)
        assert status == 303 and headers["Location"] == f"/?t={s.token}"
        saved = json.loads(signed_in.read_text())
        assert saved["token"] == "new-token" and saved["premium"] is True
        if sys.platform != "win32":
            assert signed_in.stat().st_mode & 0o777 == 0o600
        account = json.loads(get(s, "/api/state")[2])["account"]
        assert account == {"email": "you@example.com", "name": "You", "premium": True}  # never the token
        assert post(s, "/api/config", {"beeps": True})[0] == 200
        # the same state can't be used twice
        status, _, body = get(s, f"/auth?code=good-code&state={state}", token=False)
        assert status == 400 and s.token.encode() not in body
    finally:
        s.stop()


def test_a_sign_in_this_app_did_not_start_is_refused(site, signed_out, signed_in):
    s = serve(site)
    try:
        status, headers, body = get(s, "/auth?code=good-code&state=made-up-state-123456", token=False)
        assert status == 400 and "Location" not in headers and s.token.encode() not in body
        assert not signed_in.exists()
        assert not any(r[1] == "/api/auth/exchange" for r in site.requests)
        # a bad code from the site leaves you signed out
        state = post(s, "/api/account/signin")[1]["url"].rsplit("state=", 1)[1]
        assert get(s, f"/auth?code=bad&state={state}", token=False)[0] == 400 and not signed_in.exists()
    finally:
        s.stop()


def test_auth_needs_the_local_host_name(site, signed_out):
    s = serve(site)
    try:
        request = urllib.request.Request(f"http://127.0.0.1:{s.port}/auth?code=good-code&state=x", headers={"Host": "evil.example:80"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(request)
        assert e.value.code == 403
    finally:
        s.stop()


def test_check_follows_the_server(site, signed_in):
    account = Account(site=site.url)
    site.premium = True
    assert account.check(max_age=0) and account.premium()
    assert ("GET", "/api/me", "Bearer test-token") in site.requests
    site.me_status = 503  # server trouble: keep what we know
    assert account.check(max_age=0) and account.signed_in()
    site.me_status = 401  # signed out or blocked on the server
    assert not account.check(max_age=0) and not signed_in.exists()


def test_check_keeps_the_account_offline(signed_in):
    account = Account(site="http://127.0.0.1:9")  # nothing listens there
    assert account.check(max_age=0, timeout=1) and account.signed_in()


def test_sign_out_forgets_the_token_and_tells_the_server(site, signed_in):
    s = serve(site)
    try:
        state = post(s, "/api/account/signout")[1]
        assert state["account"] is None and not signed_in.exists()
        assert any(r[:3] == ("POST", "/api/auth/logout", "Bearer test-token") for r in site.requests)
    finally:
        s.stop()


def test_dictation_waits_for_a_sign_in(signed_out):
    from hold_dictate.app import Dictation

    class Fb:
        errors = []
        def error(self, text): self.errors.append(text)
        def __getattr__(self, name): return lambda *a: None

    opened = []
    started = []
    d = Dictation(Config(api_key="gsk_secret1234"), Fb(), recorder_factory=lambda level: started.append(1),
                  on_no_key=lambda: opened.append(1), account=Account(site="http://127.0.0.1:9"))
    d.on_action("start")
    assert not started and opened and "sign in" in Fb.errors[-1]
