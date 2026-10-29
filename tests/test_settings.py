import json
import urllib.error
import urllib.request

import pytest

from hold_dictate import config, updater
from hold_dictate.config import HEBREW_PROMPT, Config
from hold_dictate.hotkey import HoldDetector, parse_hotkey
from hold_dictate.settings_ui import SettingsServer


@pytest.fixture
def server(tmp_path, monkeypatch, signed_in):
    monkeypatch.setenv("HOLD_DICTATE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "account.json").write_text(signed_in.read_text())
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
    code, state = call(server, "/api/config", {"hotkey": "ctrl+alt+d", "beeps": False, "overlay_bg": "#112233", "api_key": ""})
    assert code == 200 and state["config"]["hotkey"] == "ctrl+alt+d"
    assert server.cfg.api_key == "gsk_secret1234"  # empty key field keeps the saved key
    assert server.saved and server.saved[-1].beeps is False
    on_disk = config.load(tmp_path / "config.toml")
    assert on_disk.hotkey == "ctrl+alt+d" and on_disk.overlay_bg == "#112233"


@pytest.mark.parametrize("bad", [{"hotkey": "space"}, {"overlay_bg": "red"}, {"provider": "x"}, {"beeps": "yes"}, {"nope": 1}, {"overlay_style": "stars"}, {"background_audio": "loud"}])
def test_save_rejects_bad_values(server, bad):
    code, body = call(server, "/api/config", bad)
    assert code == 400 and body["error"]
    assert server.cfg.hotkey == "alt+space"


def test_style_and_models(server, tmp_path):
    _, state = call(server, "/api/state")
    assert state["config"]["overlay_style"] == "dots" and state["config"]["model"] == ""
    assert state["providers"]["groq"]["models"][0][0] == state["providers"]["groq"]["model"]
    assert state["local_models"][0][0] == Config().local_model  # Automatic for local = the first one
    call(server, "/api/config", {"overlay_style": "lines", "model": "whisper-large-v3-turbo", "local_model": ""})
    on_disk = config.load(tmp_path / "config.toml")
    assert on_disk.overlay_style == "lines" and on_disk.effective_model() == "whisper-large-v3-turbo"
    assert on_disk.local_model == "small"


def test_overlay_gets_the_style():
    from hold_dictate.feedback import Feedback
    fb = Feedback.__new__(Feedback)
    fb.cfg, fb.proc, fb.sent = Config(overlay_style="lines"), None, []
    fb.events = type("W", (), {"write": lambda self, line: fb.sent.append(line)})()
    fb._ensure_overlay = lambda: None
    fb.pending_update = lambda: ""
    fb.recording()
    assert fb.sent[0].split() == ["C", "#0a0a0b", "#6b80ff", "bottom", "1.00", "lines"]
    assert fb.sent[1:] == ["U -\n", "S\n"]
    fb.sent.clear()
    fb.pending_update = lambda: "v9.9.9"
    fb.recording()
    assert fb.sent[1] == "U v9.9.9\n"


def test_updater_still_checks_when_auto_update_is_off(monkeypatch):
    u = updater.Updater(Config(auto_update=False))
    ran = []
    monkeypatch.setattr(u, "check", lambda: ran.append("check"))
    monkeypatch.setattr(u, "install", lambda: ran.append("install"))
    monkeypatch.setattr(updater.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: target()})())
    u.next_check = 0
    u.tick()
    assert ran == ["check"]


def test_update_command_installs_through_the_daemon(server, monkeypatch, capsys):
    from hold_dictate import cli
    asked = []
    server.updater = type("U", (), {
        "latest": "v9.9.9", "status": "", "progress": None, "available": lambda self: True, "can_update": lambda self: "",
        "update_now": lambda self: asked.append("install"),
    })()
    assert cli.main(["update"]) == 0
    assert asked == ["install"] and "v9.9.9" in capsys.readouterr().out


def test_languages_are_a_list(server):
    _, state = call(server, "/api/config", {"languages": ["he", "EN", "he"]})
    assert server.cfg.languages == "he,en" and state["config"]["languages"] == ["he", "en"]
    assert ["he", "Hebrew"] in state["languages"]
    code, body = call(server, "/api/config", {"languages": ["he", "klingon"]})
    assert code == 400 and "klingon" in body["error"] and server.cfg.languages == "he,en"
    call(server, "/api/config", {"languages": []})
    assert server.cfg.languages == ""


def test_old_language_settings_migrate(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(f'language = ""\nprompt = "{HEBREW_PROMPT}"\n', encoding="utf-8")
    cfg = config.load(p)
    assert cfg.languages == "he,en" and cfg.prompt == ""
    p.write_text('language = "fr"\n')
    assert config.load(p).languages == "fr"
    config.save(config.load(p), p)
    assert config.load(p).languages == "fr" and "language =" not in p.read_text(encoding="utf-8")


def test_token_survives_a_restart(server):
    again = SettingsServer(Config(), port=0)
    assert again.token == server.token
    again.httpd.server_close()


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


def test_a_key_belongs_to_its_service(server):
    call(server, "/api/config", {"api_key": "gsk_newkey12345"})
    assert server.cfg.api_key_service == "groq"
    _, state = call(server, "/api/config", {"provider": "openai"})
    assert state["key_hint"] == "" and server.cfg.effective_key() == ""  # a Groq key does not work with OpenAI
    _, state = call(server, "/api/config", {"provider": "groq"})
    assert state["key_hint"] == "…2345"


def test_old_configs_keep_their_key(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "openai"\napi_key = "sk-old"\n')
    cfg = config.load(p)
    assert cfg.api_key_service == "openai" and cfg.effective_key() == "sk-old"


def test_old_sounds_setting_migrates(tmp_path, monkeypatch):
    p = tmp_path / "config.toml"
    p.write_text("sounds = true\n")
    monkeypatch.setattr(config, "pill_available", lambda: False)  # macOS, Windows, Linux without the pill: it always played
    assert config.load(p).beeps is True and Config().beeps is True
    monkeypatch.setattr(config, "pill_available", lambda: True)
    assert config.load(p).beeps is False  # it never played with the pill, so turning it on now must not surprise anyone
    assert Config().beeps is False
    p.write_text("sounds = true\nbeeps = true\n")
    assert config.load(p).beeps is True


def test_install_progress_follows_uv(monkeypatch):
    u = updater.Updater(Config())
    u.progress = 0
    seen = []
    class Proc:
        stdout = iter(["Resolved 5 packages in 1s\n", "   Building hold-dictate @ git+https://x\n", "Prepared 5 packages\n", "Installed 5 packages\n"])
        def wait(self):
            return 0
    def popen(*a, **k):
        proc = Proc()
        lines = proc.stdout
        def tracked():
            for line in lines:
                yield line
                seen.append(u.progress)
        proc.stdout = tracked()
        return proc
    monkeypatch.setattr(updater.subprocess, "Popen", popen)
    assert u._run_install(["uv"]) is None
    assert seen == [30, 40, 85, 95]


def test_word_overrides_and_app_rules_persist(server):
    code, state = call(server, "/api/config", {
        "word_overrides": {" gitub ": "GitHub", "um so": "", " ": "x"},
        "app_rules": {" Slack ": "submit", "Terminal": "shift"},
        "hotkey_mode": "auto", "silence_stop": 2.5, "post_hook": "  tr a-z A-Z  ",
    })
    assert code == 200
    assert state["config"]["word_overrides"] == {"gitub": "GitHub", "um so": ""}
    assert state["config"]["app_rules"] == {"slack": "submit", "terminal": "shift"}
    on_disk = config.load(config.config_path())
    assert on_disk.word_overrides == {"gitub": "GitHub", "um so": ""}
    assert on_disk.app_rules == {"slack": "submit", "terminal": "shift"}
    assert on_disk.hotkey_mode == "auto" and on_disk.silence_stop == 2.5 and on_disk.post_hook == "tr a-z A-Z"


@pytest.mark.parametrize("bad", [{"app_rules": {"slack": "enter"}}, {"hotkey_mode": "press"}, {"word_overrides": {"a": 1}}, {"app_rules": "slack"}])
def test_save_rejects_bad_new_values(server, bad):
    code, body = call(server, "/api/config", bad)
    assert code == 400 and body["error"]
    assert server.cfg.app_rules == {} and server.cfg.hotkey_mode == "hold"


def test_silence_stop_is_clamped(server):
    assert call(server, "/api/config", {"silence_stop": 500})[1]["config"]["silence_stop"] == 60
    assert call(server, "/api/config", {"silence_stop": -3})[1]["config"]["silence_stop"] == 0


def test_last_dictation_without_session(server):
    _, state = call(server, "/api/state")
    assert state["last"] == {"text": "", "app": ""}
    assert "um" in state["fillers_default"] and isinstance(state["focus_detect"], bool)
    assert call(server, "/api/last/copy", {}) == (200, {"message": "hold-dictate is not running"})
    assert call(server, "/api/last/paste", {}) == (200, {"message": "hold-dictate is not running"})


def test_last_dictation_with_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLD_DICTATE_CONFIG_DIR", str(tmp_path))
    done = []
    session = type("Session", (), {
        "last_text": "שלום world", "last_app": "slack",
        "copy_last": lambda self: done.append("copy") or "",
        "paste_last": lambda self: done.append("paste") or "nothing to paste",
    })()
    s = SettingsServer(Config(), port=0, session=session).start()
    try:
        _, state = call(s, "/api/state")
        assert state["last"] == {"text": "שלום world", "app": "slack"}
        assert call(s, "/api/last/copy", {}) == (200, {"message": ""})
        assert call(s, "/api/last/paste", {}) == (200, {"message": "nothing to paste"})
        assert done == ["copy", "paste"]
    finally:
        s.stop()


def test_edit_shortcut_is_checked_and_must_differ(server):
    code, state = call(server, "/api/config", {"edit_hotkey": "Ctrl+Shift+E"})
    assert code == 200 and state["config"]["edit_hotkey"] == "ctrl+shift+e"  # written the usual way
    code, body = call(server, "/api/config", {"edit_hotkey": "alt+space"})
    assert code == 400 and "different shortcut" in body["error"]
    code, body = call(server, "/api/config", {"hotkey": "ctrl+shift+e"})
    assert code == 400 and "Edit by voice shortcut" in body["error"]
    for bad in ("space", "alt+pageup", "shift+"):
        code, body = call(server, "/api/config", {"edit_hotkey": bad})
        assert code == 400 and body["error"].startswith("edit shortcut")
    code, state = call(server, "/api/config", {"edit_hotkey": ""})
    assert code == 200 and state["config"]["edit_hotkey"] == ""  # off
    assert server.cfg.edit_hotkey == ""


def test_premium_switches_and_the_learned_lists(server, tmp_path):
    server.cfg.learned_terms, server.cfg.learned_fixes = "Kubernetes, Vercel", {"get hub": "GitHub", "versal": "Vercel"}
    code, state = call(server, "/api/state")
    assert state["premium"] == {"groq_key": True, "terms": ["Kubernetes", "Vercel"]}
    code, state = call(server, "/api/config", {"premium_style": True, "premium_learn": True, "learned_terms": "Kubernetes",
                                                "learned_fixes": {"get hub": "GitHub"}})
    assert code == 200 and state["config"]["premium_style"] and state["config"]["premium_learn"]
    assert state["premium"]["terms"] == ["Kubernetes"] and state["config"]["learned_fixes"] == {"get hub": "GitHub"}
    saved = config.load(tmp_path / "config.toml")
    assert saved.learned_terms == "Kubernetes" and saved.learned_fixes == {"get hub": "GitHub"} and saved.premium_style
    assert call(server, "/api/config", {"premium_style": "yes"})[0] == 400
    assert call(server, "/api/config", {"learned_fixes": ["nope"]})[0] == 400
    code, state = call(server, "/api/config", {"learned_terms": " a, A, b ,, c", "learned_fixes": {"x" * 61: "y", "ok": "fine"}})
    assert state["config"]["learned_terms"] == "a, b, c" and state["config"]["learned_fixes"] == {"ok": "fine"}
    code, state = call(server, "/api/config", {"learned_terms": "", "learned_fixes": {}})  # Forget everything
    assert state["premium"]["terms"] == [] and state["config"]["learned_fixes"] == {}


def test_no_groq_key_is_reported(server):
    code, state = call(server, "/api/config", {"provider": "openai"})
    assert state["premium"]["groq_key"] is False
