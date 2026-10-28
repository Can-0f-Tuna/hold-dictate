import json
import urllib.error
import urllib.request

import pytest

from hold_dictate import config, updater
from hold_dictate.config import HEBREW_PROMPT, Config
from hold_dictate.hotkey import HoldDetector, parse_hotkey
from hold_dictate.settings_ui import SettingsServer


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLD_DICTATE_CONFIG_DIR", str(tmp_path))
    saved = []
    s = SettingsServer(Config(api_key="gsk_secret1234"), on_saved=saved.append, port=0).start()
    s.saved = saved
    yield s
    s.stop()


def call(server, path, body=None, token=None, host=None):
    headers = {"X-Token": server.token if token is None else token, "Content-Type": "application/json"}
    if host:
        headers["Host"] = host
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_page_needs_token(server):
    with urllib.request.urlopen(server.url, timeout=5) as r:
        assert b"hold-dictate settings" in r.read()
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/?t=wrong", timeout=5)
    assert e.value.code == 403


def test_api_rejects_bad_token_and_foreign_host(server):
    assert call(server, "/api/state", token="nope")[0] == 403
    assert call(server, "/api/config", {"hotkey": "ctrl+d"}, token="")[0] == 403
    assert call(server, "/api/state", host="evil.example:1234")[0] == 403


def test_state_hides_the_key(server):
    code, state = call(server, "/api/state")
    assert code == 200
    assert "gsk_secret1234" not in json.dumps(state)
    assert state["key_hint"] == "…1234" and state["running"] is True


def test_save_applies_and_persists(server, tmp_path):
    code, state = call(server, "/api/config", {"hotkey": "ctrl+alt+d", "sounds": False, "overlay_bg": "#112233", "api_key": ""})
    assert code == 200 and state["config"]["hotkey"] == "ctrl+alt+d"
    assert server.cfg.api_key == "gsk_secret1234"  # empty key field keeps the saved key
    assert server.saved and server.saved[-1].sounds is False
    on_disk = config.load(tmp_path / "config.toml")
    assert on_disk.hotkey == "ctrl+alt+d" and on_disk.overlay_bg == "#112233"


@pytest.mark.parametrize("bad", [{"hotkey": "space"}, {"overlay_bg": "red"}, {"provider": "x"}, {"sounds": "yes"}, {"nope": 1}])
def test_save_rejects_bad_values(server, bad):
    code, body = call(server, "/api/config", bad)
    assert code == 400 and body["error"]
    assert server.cfg.hotkey == "alt+space"


def test_hebrew_language_maps_to_prompt(server):
    _, state = call(server, "/api/config", {"language": "he"})
    assert server.cfg.prompt == HEBREW_PROMPT and server.cfg.language == ""
    assert state["config"]["language"] == "he"
    call(server, "/api/config", {"language": "en"})
    assert server.cfg.prompt == "" and server.cfg.language == "en"


def test_running_url_finds_the_daemon(server):
    from hold_dictate.settings_ui import running_url
    assert running_url() == server.url


def test_hotkey_can_change_while_held():
    d = HoldDetector(parse_hotkey("alt+space"))
    assert d.feed("alt", 1, True) is None and d.feed("space", 2, True) == "start"
    assert d.set_hotkey(parse_hotkey("ctrl+d")) == "stop"
    assert d.feed("ctrl", 3, True) is None and d.feed("d", 4, True) == "start"


def test_version_compare_and_install_spec(monkeypatch):
    assert updater.newer("v0.10.0", "0.9.9") and not updater.newer("v0.3.0", "0.3.0")
    assert updater.parse_version("v1.2.3rc1") == (1, 2, 3)
    assert updater.install_spec("v0.4.0").endswith("git+https://github.com/Can-0f-Tuna/hold-dictate@v0.4.0")


def test_updater_skips_dev_checkouts():
    u = updater.Updater(Config())
    assert u.can_update()  # tests run from a checkout, not a uv tool install
    assert u.install() is False
