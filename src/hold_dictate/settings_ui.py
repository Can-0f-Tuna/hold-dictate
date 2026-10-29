"""The settings page: a small web server on 127.0.0.1 that the browser talks to.

Only this computer can reach it. Every request needs the token, and API calls must carry it
in a header, which a web page on another origin cannot send without a CORS preflight we never allow.
"""
import importlib.util
import json
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import asdict, fields
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, account as accountmod, config as cfgmod, ducking, textproc
from .config import APP_RULES, DUCK_MODES, HOTKEY_MODES, LANGUAGES, LOCAL_MODELS, OVERLAY_STYLES, PROVIDERS, Config

PAGE = Path(__file__).with_name("settings.html")
REOPEN = {"linux": "from your app menu (hold-dictate settings)", "darwin": "from the hold-dictate icon in the menu bar"}
STALE_PAGE = f"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="light dark"><title>hold-dictate settings</title>
<style>
@font-face {{ font-family:"Geist"; src:url(/fonts/geist.woff2) format("woff2"); font-weight:100 900; }}
:root {{ --paper:#fcfcfb; --ink:#0c0c0d; --muted:#6a6a6f; --soft:#f2f2ef; --line:#e7e7e3; }}
@media (prefers-color-scheme: dark) {{ :root {{ --paper:#0a0a0b; --ink:#ededeb; --muted:#9b9ba0; --soft:#1f1f22; --line:#27272b; }} }}
body {{ margin:0; background:var(--paper); color:var(--ink); font:15px/1.55 "Geist",system-ui,sans-serif; -webkit-font-smoothing:antialiased; }}
main {{ max-width:560px; margin:18vh auto; padding:0 24px; }}
.brand {{ display:flex; align-items:center; gap:10px; font-weight:600; margin-bottom:48px; }}
h1 {{ margin:0 0 12px; font-size:32px; font-weight:600; letter-spacing:-.02em; line-height:1.15; }}
p {{ margin:0; color:var(--muted); font-size:17px; }}
code {{ font:500 .9em ui-monospace,Menlo,Consolas,monospace; background:var(--soft); border:1px solid var(--line); border-radius:6px; padding:1px 5px; color:var(--ink); }}
</style>
<main><div class="brand"><svg width="22" height="22" viewBox="0 0 32 32" aria-hidden="true"><rect x="1" y="1" width="30" height="30" rx="8" fill="#000"/><rect x="3.5" y="2.5" width="25" height="23" rx="6" fill="#232326"/><g fill="#fff"><rect x="8" y="12" width="2.6" height="4" rx="1.3"/><rect x="12.4" y="9" width="2.6" height="10" rx="1.3"/><rect x="16.8" y="6.5" width="2.6" height="15" rx="1.3" fill="#5b74ff"/><rect x="21.2" y="10.5" width="2.6" height="7" rx="1.3"/></g></svg>hold-dictate</div>
<h1>This settings link has expired.</h1>
<p>Open the settings again {REOPEN.get("linux" if sys.platform.startswith("linux") else sys.platform, "from the hold-dictate icon next to the clock")}, or run <code>hold-dictate settings</code> in a terminal.</p></main>
""".encode("utf-8")


def auth_page(problem: str) -> bytes:
    """Shown when a sign-in can't finish. Same look as the expired-link page."""
    from html import escape
    return STALE_PAGE.replace(b"This settings link has expired.", b"The sign-in didn't finish.").replace(
        STALE_PAGE[STALE_PAGE.index(b"<p>"):STALE_PAGE.index(b"</p>") + 4], f"<p>{escape(problem)}</p>".encode("utf-8"))


FONTS = {"/fonts/geist.woff2": "geist-latin.woff2", "/fonts/geist-mono.woff2": "geist-mono-latin.woff2"}
PORT = 47811
SECRET_FIELDS = {"api_key"}
EDITABLE = {f.name for f in fields(Config)} - {"idle_unload_seconds", "api_key_service"}


def runtime_file() -> Path:
    return cfgmod.config_dir() / "settings-server.json"


def running_url(timeout: float = 1.0) -> str:
    """URL of the settings page of the hold-dictate that is running now, or ''."""
    try:
        info = json.loads(runtime_file().read_text(encoding="utf-8"))
        request = urllib.request.Request(f"http://127.0.0.1:{info['port']}/api/ping", headers={"X-Token": info["token"]})
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if json.load(response).get("app") == "hold-dictate":
                return f"http://127.0.0.1:{info['port']}/?t={info['token']}"
    except Exception:
        pass
    return ""


def saved_token() -> str:
    """The token of the previous run. Keeping it means a settings tab left open still works after
    hold-dictate restarts, for example after an update."""
    try:
        token = json.loads(runtime_file().read_text(encoding="utf-8")).get("token", "")
        return token if isinstance(token, str) and len(token) >= 24 else ""
    except Exception:
        return ""


class SettingsServer:
    """daemon=True when it runs inside `hold-dictate run`; False for the stand-alone `settings` command."""

    def __init__(self, cfg: Config, on_saved=None, updater=None, daemon: bool = True, port: int = PORT, session=None, account=None):
        self.cfg = cfg
        self.account = account or accountmod.Account()  # nothing works until you sign in
        self.on_saved = on_saved or (lambda cfg: None)
        self.updater = updater
        self.session = session  # the running dictation: last_text, last_app, copy_last(), paste_last()
        self.daemon = daemon
        self.problem = ""  # set when no shortcut can work (the keyboard can't be read), shown on the page
        self.hotkey_problem = ""  # set when the saved shortcut was invalid and the default is used
        self.token = (daemon and saved_token()) or secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        handler = type("Handler", (_Handler,), {"app": self})
        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
        except OSError:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    def start(self):
        if self.daemon:
            path = runtime_file()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"port": self.port, "token": self.token}), encoding="utf-8")
            if sys.platform != "win32":
                path.chmod(0o600)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self

    def stop(self):
        self.httpd.shutdown()

    def open(self):
        webbrowser.open(self.url)

    # ---------- API ----------

    def state(self) -> dict:
        cfg = self.cfg
        values = {k: v for k, v in asdict(cfg).items() if k in EDITABLE and k not in SECRET_FIELDS}
        values["languages"] = cfg.language_codes()
        key = cfg.saved_key()
        updater = self.updater
        session = self.session
        from .paste import can_detect_focus
        return {
            "version": __version__,
            "account": self.account.public(),
            "platform": sys.platform,
            "running": self.daemon,
            "problem": self.problem,
            "hotkey_problem": self.hotkey_problem,
            "config": values,
            "key_hint": f"…{key[-4:]}" if len(key) >= 8 else ("set" if key else ""),
            "key_from_env": bool(cfg.effective_key()) and not key,
            "providers": {k: {"label": v["label"], "keys_url": v["keys_url"], "model": v["model"], "models": v["models"]} for k, v in PROVIDERS.items()},
            "local_models": LOCAL_MODELS,
            "languages": [[code, name] for code, name, _ in LANGUAGES],
            "local_installed": importlib.util.find_spec("faster_whisper") is not None,
            "overlay": sys.platform.startswith("linux"),
            "ducking": ducking.available(),
            "update": {
                "latest": updater.latest if updater else "",
                "available": bool(updater and updater.available()),
                "status": updater.status if updater else "",
                "progress": updater.progress if updater else None,
                "can_update": (updater.can_update() if updater else "not running") or "",
            },
            "last": {"text": getattr(session, "last_text", "") or "", "app": getattr(session, "last_app", "") or ""},
            "fillers_default": list(textproc.FILLERS),
            "focus_detect": can_detect_focus(),
            "config_path": str(cfgmod.config_path()),
        }

    def candidate(self, changes: dict) -> Config:
        """A copy of the current config with the changes applied and checked. Raises ValueError."""
        from .hotkey import parse_hotkey
        if not self.account.signed_in():
            raise ValueError("sign in first")
        new = Config(**asdict(self.cfg))
        for name, value in changes.items():
            if name not in EDITABLE:
                raise ValueError(f"unknown setting {name}")
            if name == "api_key" and not str(value).strip():
                continue  # empty field means keep the saved key
            setattr(new, name, cfgmod.coerce(name, value))
        if str(changes.get("api_key", "")).strip():
            new.api_key_service = new.provider
        if new.provider not in (*PROVIDERS, "local"):
            raise ValueError("unknown provider")
        parse_hotkey(new.hotkey)
        if new.overlay_position not in ("bottom", "top"):
            raise ValueError("position must be bottom or top")
        if new.background_audio not in DUCK_MODES:
            raise ValueError("background audio must be " + ", ".join(DUCK_MODES))
        if new.overlay_style not in OVERLAY_STYLES:
            raise ValueError("style must be " + " or ".join(OVERLAY_STYLES))
        if new.hotkey_mode not in HOTKEY_MODES:
            raise ValueError("hotkey mode must be " + ", ".join(HOTKEY_MODES))
        new.silence_stop = min(60.0, max(0.0, new.silence_stop))
        new.word_overrides = {k.strip(): v for k, v in new.word_overrides.items() if k.strip()}
        new.app_rules = {k.strip().lower(): v for k, v in new.app_rules.items() if k.strip()}
        for app, rule in new.app_rules.items():
            if rule not in APP_RULES:
                raise ValueError(f"rule for {app} must be " + ", ".join(APP_RULES))
        new.post_hook = new.post_hook.strip()
        if not new.local_model:
            new.local_model = LOCAL_MODELS[0][0]
        new.overlay_size = min(1.5, max(0.75, new.overlay_size))
        new.min_hold = min(2.0, max(0.0, new.min_hold))
        new.timeout = min(120.0, max(3.0, new.timeout))
        for name in ("overlay_bg", "overlay_bars"):
            value = getattr(new, name)
            if not (len(value) == 7 and value.startswith("#") and all(c in "0123456789abcdefABCDEF" for c in value[1:])):
                raise ValueError(f"{name} must be a colour like #1a2b3c")
        return new

    def save(self, changes: dict) -> dict:
        with self.lock:
            new = self.candidate(changes)
            cfgmod.save(new)
            for f in fields(Config):
                setattr(self.cfg, f.name, getattr(new, f.name))
            self.on_saved(self.cfg)
        return self.state()

    def test_key(self, changes: dict) -> dict:
        from .providers import check_api
        new = self.candidate(changes)
        if new.provider == "local":
            ok = importlib.util.find_spec("faster_whisper") is not None
            return {"ok": ok, "message": "" if ok else 'Install the local model first: uv tool install --force "hold-dictate[local]"'}
        problem = check_api(new)
        if " returned 401" in problem:
            problem = f"{new.preset()['label']} didn't accept this key. Check that you copied all of it."
        return {"ok": not problem, "message": problem}

    def start_service(self) -> dict:
        """Installs and starts the app, then hands back the address of its own settings page."""
        from . import service
        message = service.install()
        url = ""
        for _ in range(20):  # the app takes a moment to start its server
            url = running_url(timeout=0.5)
            if url:
                break
            time.sleep(0.25)
        return {"message": message, "url": url}

    def sign_in(self) -> dict:
        return {"url": self.account.begin(self.port)}

    def sign_out(self) -> dict:
        self.account.sign_out()
        return self.state()

    def check_account(self) -> dict:
        self.account.check(max_age=0, timeout=5)
        return self.state()

    def last(self, action: str) -> dict:
        if not self.session:
            return {"message": "hold-dictate is not running"}
        note = self.session.copy_last() if action == "copy" else self.session.paste_last()
        return {"message": note or ""}

    def update(self, action: str) -> dict:
        if not self.updater:
            return self.state()
        if action == "check":
            self.updater.check()
        elif action == "install":
            self.updater.update_now()
        return self.state()


class _Handler(BaseHTTPRequestHandler):
    app: SettingsServer
    server_version = "hold-dictate"

    def log_message(self, *args):
        pass

    def _host_ok(self) -> bool:
        # blocks DNS-rebinding pages that point their own name at 127.0.0.1
        return self.headers.get("Host", "") in (f"127.0.0.1:{self.app.port}", f"localhost:{self.app.port}")

    def _allowed(self, token: str) -> bool:
        return self._host_ok() and secrets.compare_digest(token, self.app.token)

    def _send(self, code: int, body: bytes, content_type: str = "application/json"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            token = dict(p.partition("=")[::2] for p in query.split("&") if p).get("t", "")
            if not self._allowed(token):
                return self._send(403, STALE_PAGE, "text/html; charset=utf-8")
            state = json.dumps(self.app.state(), ensure_ascii=False).replace("</", "<\\/")
            page = PAGE.read_text(encoding="utf-8").replace("/*STATE*/null", state, 1)
            return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/auth" and self._host_ok():  # the browser, back from Google through the site
            params = urllib.parse.parse_qs(query)
            problem = self.app.account.finish(params.get("code", [""])[0], params.get("state", [""])[0])
            if problem:  # no settings link here: this request carries no proof it came from you
                return self._send(400, auth_page(problem), "text/html; charset=utf-8")
            self.send_response(303)
            self.send_header("Location", f"/?t={self.app.token}")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path in FONTS and self._allowed(self.app.token):  # the Host check only: fonts are not private
            return self._send(200, (PAGE.parent / "fonts" / FONTS[path]).read_bytes(), "font/woff2")
        if not self._allowed(self.headers.get("X-Token", "")):
            return self._json({"error": "forbidden"}, 403)
        if path == "/api/ping":
            return self._json({"app": "hold-dictate", "version": __version__})
        if path == "/api/state":
            return self._json(self.app.state())
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._allowed(self.headers.get("X-Token", "")):
            return self._json({"error": "forbidden"}, 403)
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(length, 65536)) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            routes = {
                "/api/config": lambda: self.app.save(body),
                "/api/test": lambda: self.app.test_key(body),
                "/api/service": self.app.start_service,
                "/api/update/check": lambda: self.app.update("check"),
                "/api/update/install": lambda: self.app.update("install"),
                "/api/last/copy": lambda: self.app.last("copy"),
                "/api/last/paste": lambda: self.app.last("paste"),
                "/api/account/signin": self.app.sign_in,
                "/api/account/signout": self.app.sign_out,
                "/api/account/check": self.app.check_account,
            }
            if self.path not in routes:
                return self._json({"error": "not found"}, 404)
            self._json(routes[self.path]())
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except Exception as e:
            self._json({"error": f"{type(e).__name__}: {e}"}, 500)
