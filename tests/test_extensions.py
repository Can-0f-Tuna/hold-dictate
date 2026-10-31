"""An extension can add to the app and a broken one can't stop it. Real entry points are in the private package's tests."""
import json
import urllib.error
import urllib.request

import pytest

from hold_dictate import cli, config, extensions, textproc
from hold_dictate.app import Dictation
from hold_dictate.config import Config
from hold_dictate.settings_ui import SettingsServer

from test_app import FakeFeedback, FakeRecorder, Pasted, cfg_for, settle, tap


class Broken:
    """Every hook this test file uses raises."""

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def hook(*args):
            raise RuntimeError("broken")
        return hook


@pytest.fixture
def add(monkeypatch):
    def add(*found):
        monkeypatch.setattr(extensions, "_loaded", list(found))
    return add


def dictation(cfg, app="slack"):
    out, fb = Pasted(), FakeFeedback()
    return Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: app), out, fb


def test_a_broken_entry_point_is_skipped(monkeypatch, capsys):
    class Point:
        name = "bad"

        def load(self):
            raise ImportError("gone")

    class Good:
        name = "good"

        def load(self):
            return lambda: "the extension"

    monkeypatch.setattr(extensions, "_loaded", None)
    monkeypatch.setattr(extensions, "_broken", False)
    monkeypatch.setattr(extensions, "entry_points", lambda group: [Point(), Good()] if group == "hold_dictate.extensions" else [])
    assert extensions.loaded() == ["the extension"]
    assert "bad" in capsys.readouterr().err and extensions._broken


def test_a_broken_extension_does_not_stop_dictation(api, add, capsys):
    add(Broken())
    api.text = "dictated"
    d, out, fb = dictation(cfg_for(api, word_overrides={"dictated": "typed"}))
    tap(d, 0.3)
    settle(d)
    assert [c["text"] for c in out.calls] == ["typed"] and fb.log == ["recording", "working", "done"]
    assert "an extension failed" in capsys.readouterr().err


def test_before_recording_can_say_no(api, add):
    class No:
        def before_recording(self, dictation):
            return "not now"
    add(No())
    opened = []
    d, _, fb = dictation(cfg_for(api))
    d.on_no_key = lambda: opened.append(1)
    d.on_action("start")
    assert d.recorder is None and ("error", "not now") in fb.log and opened == [1]


def test_after_dictation_gets_the_audio_and_all_three_texts(api, add):
    seen = []

    class Watcher:
        def rewriter(self, dictation):
            return lambda text, app: text.upper()

        def after_dictation(self, dictation, audio, raw, cleaned, final, app):
            seen.append((len(audio) > 0, raw, cleaned, final, app))
    add(Watcher())
    api.text = "get hub"
    d, out, _ = dictation(cfg_for(api, word_overrides={"get hub": "GitHub"}))
    tap(d, 0.3)
    settle(d)
    assert out.calls[0]["text"] == "GITHUB"
    assert seen == [(True, "get hub", "GitHub", "GITHUB", "slack")]


def test_after_dictation_is_skipped_when_a_text_hook_drops_the_text(api, add):
    seen = []

    class Watcher:
        def after_dictation(self, *args):
            seen.append(args)
    add(Watcher())
    d, _, _ = dictation(cfg_for(api, post_hook="exit 77"))
    tap(d, 0.3)
    settle(d)
    assert seen == []


def test_a_failing_rewriter_pastes_the_text_and_any_rewriter_stops_live_typing(api, add):
    class Failing:
        def rewriter(self, dictation):
            def rewrite(text, app):
                raise RuntimeError("no")
            return rewrite
    add(Failing())
    d, out, _ = dictation(cfg_for(api, live_text=True))
    assert not d._types_live()
    tap(d, 0.3)
    settle(d)
    assert [c["text"] for c in out.calls] == ["hello world"]


def test_the_second_hotkey_hands_its_recording_to_the_extension(api, add):
    got = []
    answers = iter([got.append, None, False])

    class Second:
        def second_recording(self, dictation):
            return next(answers)
    add(Second())
    d, out, _ = dictation(cfg_for(api, min_hold=0))
    d.on_action("second:start")
    d.finish()
    settle(d)
    assert len(got) == 1 and out.calls == []
    d.on_action("second:start")  # None: a plain dictation
    d.finish()
    settle(d)
    assert len(out.calls) == 1
    d.on_action("second:start")  # False: nothing happens
    assert d.recorder is None


def test_text_hooks_change_the_prompt_and_the_words(add):
    from hold_dictate.providers import language_hints

    class Words:
        def adjust_prompt(self, prompt, cfg):
            return prompt + " Vercel"

        def adjust_words(self, overrides, cfg):
            return {"get hub": "GitHub", **overrides}
    add(Broken(), Words())
    cfg = Config(prompt="Hint")
    assert language_hints(cfg)[1] == "Hint Vercel"
    assert textproc.clean("push get hub", cfg) == "push GitHub"


def test_review_transcript_can_ask_again_and_a_failed_request_is_not_hidden(add, monkeypatch):
    from hold_dictate import providers
    from hold_dictate.providers import TranscribeError, transcribe_api
    answers = [("first", "english", -0.9), ("second", "hebrew", -0.1)]
    monkeypatch.setattr(providers, "_request", lambda *a: answers.pop(0))

    class Review:
        def review_transcript(self, text, cfg, heard, sure, again):
            assert (heard, sure) == ("en", -0.9)
            return again("he")[0]
    add(Review())
    cfg = Config(provider="groq", api_key="k", languages="he,en")
    assert transcribe_api(cfg, None) == "second"

    class Down:
        def review_transcript(self, text, cfg, heard, sure, again):
            raise TranscribeError("down")
    add(Down())
    answers[:] = [("first", "english", -0.9)]
    with pytest.raises(TranscribeError):
        transcribe_api(cfg, None)


def test_the_overlay_gets_pill_lines_before_the_recording():
    from hold_dictate.feedback import Feedback

    class Lines:
        def __init__(self):
            self.lines = []

        def write(self, text):
            self.lines.append(text.strip())
    fb = Feedback.__new__(Feedback)
    fb.cfg, fb.proc, fb.events = None, None, Lines()
    fb._ensure_overlay = lambda: None
    fb.pending_update = lambda: ""
    extensions._loaded.append(type("Pill", (), {"pill_lines": lambda self: ["X on"]})())
    fb.recording()
    assert fb.events.lines == ["U -", "X on", "S"]


class Page:
    def settings_fields(self):
        return {"my_switch": False, "my_table": {}}

    def settings_state(self, cfg):
        return {"mine": cfg.setting("my_switch", False)}

    def settings_page(self):
        return "<section id=mine></section>", "<script>/*mine*/</script>"

    def settings_routes(self, server):
        def page(handler, query):
            handler._json({"query": query})
        return {"POST /api/mine": lambda: {"port": server.port}, "GET /mine": page}

    def check_settings(self, new, changes):
        if new.hotkey == "ctrl+x":
            raise ValueError("not that one")


@pytest.fixture
def server(add):
    add(Broken(), Page())
    s = SettingsServer(Config(), port=0).start()
    yield s
    s.stop()


def call(server, path, body=None):
    headers = {"X-Token": server.token, "Content-Type": "application/json"}
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=5) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_settings_fields_are_saved_with_the_rest(server):
    code, state = call(server, "/api/state")
    assert state["config"]["my_switch"] is False and state["mine"] is False
    code, state = call(server, "/api/config", {"my_switch": True})
    assert code == 200 and state["config"]["my_switch"] is True and state["mine"] is True
    assert config.load().extra == {"my_switch": True}
    assert call(server, "/api/config", {"my_switch": "yes"})[0] == 400


def test_check_settings_can_refuse_a_save(server):
    code, body = call(server, "/api/config", {"hotkey": "ctrl+x"})
    assert (code, body) == (400, {"error": "not that one"})


def test_the_page_and_the_routes_of_an_extension(server):
    with urllib.request.urlopen(server.url, timeout=5) as r:
        page = r.read().decode()
    assert page.index("<section id=mine>") < page.index('id="updGroup"')
    assert page.index("/*mine*/") < page.index("---- start ----")
    assert call(server, "/api/mine", {}) == (200, {"port": server.port})
    assert call(server, "/mine?a=1") == (200, {"query": "a=1"})


def test_doctor_rows_and_commands_of_an_extension(add, capsys):
    class Cli:
        def doctor_lines(self):
            return [(True, "my own check", "")]

        def extend_cli(self, commands):
            commands.add_parser("mine").set_defaults(fn=lambda args: 7)
    add(Broken(), Cli())
    assert cli.main(["mine"]) == 7
    cli.main(["doctor"])
    assert "my own check" in capsys.readouterr().out


class Source:
    def latest(self):
        return "v99.0.0"

    def install_args(self, tag):
        return ["--with", "extra.whl", f"core@{tag}"]


def installed_with(monkeypatch):
    """What the updater runs. It stands in for the install itself, which only runs from a uv tool folder."""
    from hold_dictate import updater
    ran = []
    monkeypatch.setattr(updater, "installed_by_uv", lambda: True)
    monkeypatch.setattr(updater, "find_uv", lambda: "uv")
    u = updater.Updater(Config())
    monkeypatch.setattr(u, "_run_install", lambda cmd: ran.append(cmd))
    return u, ran


def test_an_update_source_decides_what_is_installed(add, monkeypatch):
    add(type("Ext", (), {"update_source": lambda self: Source()})())
    u, ran = installed_with(monkeypatch)
    assert u.install() is True and ran == [["uv", "tool", "install", "--force", "--with", "extra.whl", "core@v99.0.0"]]


def test_a_failing_update_source_stays_on_this_version(add, monkeypatch):
    class Down(Source):
        def latest(self):
            raise OSError("no answer")
    add(type("Ext", (), {"update_source": lambda self: Down()})())
    u, ran = installed_with(monkeypatch)
    assert u.install() is False and ran == [] and "could not check" in u.status


@pytest.mark.parametrize("broken_load", [False, True])
def test_a_broken_extension_is_not_replaced_from_github(add, monkeypatch, broken_load):
    add(Broken())
    if broken_load:
        add()
        monkeypatch.setattr(extensions, "_broken", True)
    u, ran = installed_with(monkeypatch)
    assert u.source is False and u.install() is False and ran == []


def test_with_no_extensions_the_hooks_do_nothing():
    cfg = Config(prompt="x", word_overrides={"a": "b"})
    assert extensions.update_source() is None
    assert extensions.before_recording(None) == "" and extensions.rewriter(None) is None
    assert extensions.second_hotkey(cfg, None) is None and extensions.second_recording(None) is None
    assert extensions.adjust_prompt("x", cfg) == "x" and extensions.adjust_words({"a": "b"}, cfg) == {"a": "b"}
    assert extensions.pill_lines() == [] and extensions.notices() == [] and extensions.settings_page() == ("", "")
