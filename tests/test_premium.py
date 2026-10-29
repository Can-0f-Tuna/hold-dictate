import json
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hold_dictate import premium, textproc
from hold_dictate.account import Account
from hold_dictate.app import Dictation
from hold_dictate.config import Config
from hold_dictate.hotkey import EV_FORMAT, EvdevBackend, HoldDetector, parse_hotkey
from hold_dictate.premium import Client, Learner, PremiumError, corrections
from hold_dictate.providers import PROMPT_BYTES, language_hints

from test_app import FakeFeedback, FakeRecorder, Pasted, settle


class FakePremiumSite:
    """The site's premium API: records every request, answers what the test sets per route."""

    def __init__(self):
        self.requests = []
        self.replies = {"edit": (200, {"text": "edited"}), "style": (200, {"text": "styled"}),
                        "learn": (200, {"terms": [], "fixes": {}})}
        self.me_status, self.delay = 200, 0.0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization")})
                self._reply(outer.me_status, {"email": "you@example.com", "name": "You", "premium": False})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                outer.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                       "groq": self.headers.get("X-Groq-Key"), "body": body})
                time.sleep(outer.delay)
                status, payload = outer.replies.get(self.path.rsplit("/", 1)[-1], (404, {}))
                self._reply(status, payload)

            def _reply(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:  # the client gave up waiting
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self, kind):
        return [r for r in self.requests if r["path"] == f"/api/premium/{kind}"]


@pytest.fixture
def site():
    fake = FakePremiumSite()
    yield fake
    fake.server.shutdown()


@pytest.fixture
def premium_account(signed_in):
    data = json.loads(signed_in.read_text())
    signed_in.write_text(json.dumps({**data, "premium": True}))
    return signed_in


def groq_cfg(**kw):
    return Config(**{"provider": "groq", "api_key": "gsk_mykey1234", "api_key_service": "groq", "languages": "he,en", **kw})


def wait_for(cond, seconds=3.0):
    deadline = time.monotonic() + seconds
    while not cond() and time.monotonic() < deadline:
        time.sleep(0.02)
    return cond()


# ---------- the client ----------

def test_client_sends_the_token_the_key_and_the_languages(site, premium_account):
    client = Client(groq_cfg(), Account(site=site.url))
    assert client.edit("Hello there", "make it formal", "slack") == "edited"
    req = site.posts("edit")[0]
    assert req["auth"] == "Bearer test-token" and req["groq"] == "gsk_mykey1234"
    assert req["body"] == {"text": "Hello there", "instruction": "make it formal", "app": "slack", "languages": ["he", "en"]}


def test_groq_key_comes_from_the_groq_service_or_the_environment(monkeypatch, premium_account):
    account = Account(site="http://127.0.0.1:9")
    assert Client(Config(provider="openai", api_key="sk-x", api_key_service="openai"), account).problem() == premium.NO_GROQ_KEY
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    assert premium.groq_key(Config(provider="openai", api_key="sk-x")) == "gsk_env"
    assert premium.groq_key(groq_cfg()) == "gsk_mykey1234"


def test_not_premium_or_signed_out_never_calls_the_server(site, signed_in):
    client = Client(groq_cfg(), Account(site=site.url))
    with pytest.raises(PremiumError, match="Premium feature"):
        client.style("hi")
    signed_in.unlink()
    with pytest.raises(PremiumError, match="Sign in"):
        client.style("hi")
    assert site.requests == []


@pytest.mark.parametrize("status, error, message", [
    (400, "groq key", "Groq didn't accept your key"),
    (400, "text", "couldn't use this text"),
    (429, "groq busy", "Groq is busy"),
    (502, "groq", "Groq didn't answer"),
    (503, "", "server is busy"),
])
def test_client_errors_have_short_messages(site, premium_account, status, error, message):
    site.replies["style"] = (status, {"error": error})
    with pytest.raises(PremiumError, match=message):
        Client(groq_cfg(), Account(site=site.url)).style("hello")


def test_signed_out_on_the_server_signs_out_here(site, premium_account):
    site.replies["edit"], site.me_status = (401, {"error": "signed out"}), 401
    with pytest.raises(PremiumError, match="signed out"):
        Client(groq_cfg(), Account(site=site.url)).edit("text", "shorter")
    assert wait_for(lambda: not premium_account.exists())  # account.check(max_age=0) saw the 401


def test_premium_turned_off_refreshes_the_account(site, premium_account):
    site.replies["style"] = (403, {"error": "premium"})
    with pytest.raises(PremiumError, match="Premium is off"):
        Client(groq_cfg(), Account(site=site.url)).style("hello")
    assert wait_for(lambda: json.loads(premium_account.read_text())["premium"] is False)


def test_unreachable_and_slow_servers(site, premium_account, monkeypatch):
    with pytest.raises(PremiumError, match="Couldn't reach"):
        Client(groq_cfg(), Account(site="http://127.0.0.1:9")).style("hello")
    monkeypatch.setitem(premium.TIMEOUTS, "style", 0.2)
    site.delay = 1.0
    with pytest.raises(PremiumError, match="too long"):
        Client(groq_cfg(), Account(site=site.url)).style("hello")


def test_edit_checks_the_selection_before_sending(site, premium_account):
    client = Client(groq_cfg(), Account(site=site.url))
    with pytest.raises(PremiumError, match="Select some text"):
        client.edit("  ", "shorter")
    with pytest.raises(PremiumError, match="too much text"):
        client.edit("x" * 8001, "shorter")
    assert site.requests == []


# ---------- writing that fits the app ----------

def styled_dictation(api, site, monkeypatch, **kw):
    monkeypatch.setenv("GROQ_API_KEY", "gsk_env")
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, beeps=False, premium_style=True, **kw)
    out, fb = Pasted(), FakeFeedback()
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: "slack",
                  account=Account(site=site.url))
    return d, out, fb


def say(d, seconds=0.3):
    d.on_action("start")
    time.sleep(seconds)
    d.on_action("stop")
    settle(d)


def test_style_rewrites_before_pasting(api, site, premium_account, monkeypatch):
    api.text = "hey can you check the build"
    site.replies["style"] = (200, {"text": "Hey, can you check the build?"})
    d, out, fb = styled_dictation(api, site, monkeypatch)
    say(d)
    assert [c["text"] for c in out.calls] == ["Hey, can you check the build?"]
    assert site.posts("style")[0]["body"] == {"text": "hey can you check the build", "app": "slack", "languages": []}
    assert fb.log == ["recording", "working", "done"]


@pytest.mark.parametrize("reply", [(502, {"error": "groq"}), (200, {"nope": 1}), (200, {"text": "  "})])
def test_style_failure_pastes_the_original(api, site, premium_account, monkeypatch, capsys, reply):
    api.text = "keep this exactly"
    site.replies["style"] = reply
    d, out, _ = styled_dictation(api, site, monkeypatch)
    say(d)
    assert [c["text"] for c in out.calls] == ["keep this exactly"]
    if reply[0] != 200 or "nope" in reply[1]:
        assert "pasting the text as dictated" in capsys.readouterr().err


def test_style_timeout_pastes_the_original(api, site, premium_account, monkeypatch):
    monkeypatch.setitem(premium.TIMEOUTS, "style", 0.2)
    site.delay = 1.0
    d, out, _ = styled_dictation(api, site, monkeypatch)
    say(d)
    assert [c["text"] for c in out.calls] == ["hello world"]


def test_style_is_skipped_without_premium_or_when_off(api, site, signed_in, monkeypatch):
    d, out, _ = styled_dictation(api, site, monkeypatch)  # signed in, not premium
    say(d)
    assert [c["text"] for c in out.calls] == ["hello world"] and site.requests == []
    signed_in.write_text(json.dumps({**json.loads(signed_in.read_text()), "premium": True}))
    d.cfg.premium_style = False
    say(d)
    assert len(out.calls) == 2 and site.requests == []


# ---------- edit by voice ----------

class Grab:
    def __init__(self, selected):
        self.selected, self.apps, self.restored = selected, [], 0

    def __call__(self, app):
        self.apps.append(app)
        return self.selected, self.restore

    def restore(self):
        self.restored += 1


class FakePremium:
    """Stands in for premium.Client in the Dictation tests."""

    def __init__(self, problem="", edited="edited", fail=None):
        self._problem, self.edited, self.fail = problem, edited, fail
        self.edits, self.learns = [], []

    def problem(self):
        return self._problem

    def edit(self, text, instruction, app=""):
        self.edits.append((text, instruction, app))
        if self.fail:
            raise PremiumError(self.fail)
        return self.edited

    def style(self, text, app=""):
        return text

    def learn(self, transcript, corrections, terms, fixes):
        self.learns.append({"transcript": transcript, "corrections": corrections, "terms": terms, "fixes": fixes})
        return terms + ["Learned"], fixes


def edit_dictation(api, grab, client, app="code", **kw):
    api.text = "make it shorter"
    cfg = Config(**{"provider": "custom", "base_url": api.url, "api_key": "k", "min_hold": 0.1, "beeps": False, **kw})
    out, fb = Pasted(), FakeFeedback()
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: app,
                  grab=grab, premium=client)
    return d, out, fb


def edit_say(d, seconds=0.3):
    d.on_action("edit:start")
    time.sleep(seconds)
    d.on_action("edit:stop")
    settle(d)


def test_edit_by_voice_replaces_the_selection(api):
    grab, client = Grab("This is a very long sentence that goes on."), FakePremium(edited="A long sentence.")
    d, out, fb = edit_dictation(api, grab, client, auto_submit=True)
    edit_say(d)
    assert client.edits == [("This is a very long sentence that goes on.", "make it shorter", "code")]
    assert out.calls == [{"text": "A long sentence.", "app": "code", "shift": None, "submit": False}]  # never Enter
    assert grab.restored == 1 and grab.apps == ["code"]
    assert fb.log == ["recording", "working", "done"]
    assert d.last_text == ""  # an edit is not a dictation


def test_edit_without_a_selection_says_so(api):
    grab, client = Grab(""), FakePremium()
    d, out, fb = edit_dictation(api, grab, client)
    edit_say(d)
    assert ("error", "Select some text first.") in fb.log
    assert client.edits == [] and out.calls == [] and grab.restored == 1


def test_edit_server_failure_leaves_the_selection(api):
    grab, client = Grab("some text"), FakePremium(fail="Groq is busy. Try again in a moment.")
    d, out, fb = edit_dictation(api, grab, client)
    edit_say(d)
    assert out.calls == [] and grab.restored == 1
    assert ("error", "Groq is busy. Try again in a moment.") in fb.log


def test_edit_copy_only_app_keeps_the_result_on_the_clipboard(api):
    grab, client = Grab("some text"), FakePremium(edited="new text")
    d, out, _ = edit_dictation(api, grab, client, app="keepassxc", app_rules={"keepass": "clipboard"})
    edit_say(d)
    assert out.copied == ["new text"] and grab.restored == 0


def test_edit_is_premium_only(api):
    grab, client = Grab("some text"), FakePremium(problem=premium.PREMIUM_ONLY)
    started = []
    d, out, fb = edit_dictation(api, grab, client)
    d.recorder_factory = lambda level: started.append(1)
    d.on_action("edit:start")
    d.on_action("edit:stop")
    assert fb.log == [("error", "Edit by voice is a Premium feature.")] and not started
    d.on_action("start")  # dictation itself still works
    assert started == [1]


def test_edit_without_a_groq_key(api):
    d, _, fb = edit_dictation(api, Grab("x"), FakePremium(problem=premium.NO_GROQ_KEY))
    d.on_action("edit:start")
    assert fb.log == [("error", premium.NO_GROQ_KEY)] and d.recorder is None


# ---------- two hotkeys ----------

def test_two_hotkeys_the_more_specific_wins():
    d = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("alt+shift+space"))
    d.feed("alt", 56, True)
    assert d.feed("space", 57, True) == "start"
    assert d.feed("shift", 42, True) is None  # one at a time
    assert d.feed("space", 57, False) == "stop"
    assert d.feed("space", 57, True) == "edit:start"  # alt and shift are down now
    assert d.feed("space", 57, True) is None
    assert d.feed("shift", 42, False) == "edit:stop"  # letting go of any part of it stops
    assert d.feed("space", 57, False) is None
    assert d.feed("space", 57, True) == "start"
    assert d.feed("alt", 56, False) == "stop"


def test_edit_hotkey_can_change_or_turn_off_while_held():
    d = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("ctrl+e"))
    d.feed("ctrl", 29, True)
    assert d.feed("e", 18, True) == "edit:start"
    assert d.set_hotkey(parse_hotkey("alt+d")) is None  # dictation wasn't running
    assert d.set_edit_hotkey(None) == "edit:stop"
    d.feed("e", 18, False)
    assert d.feed("e", 18, True) is None  # off
    assert [str(h) for h in d.hotkeys()] == ["alt+d"]


def test_wait_released():
    d = HoldDetector(parse_hotkey("alt+space"))
    d.feed("alt", 56, True)
    assert d.wait_released(0.05) is False
    threading.Timer(0.05, lambda: d.feed("alt", 56, False)).start()
    assert d.wait_released(1.0) is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="evdev is Linux only")
def test_evdev_backend_reports_which_hotkey():
    ev = lambda code, value: struct.pack(EV_FORMAT, 0, 0, 1, code, value)
    r, w = os.pipe()
    seq = [(56, 1), (57, 1), (57, 0), (42, 1), (57, 1), (57, 0), (42, 0), (56, 0)]
    os.write(w, b"".join(ev(c, v) for c, v in seq))
    os.close(w)
    actions = []
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("alt+shift+space"))
    EvdevBackend([os.fdopen(r, "rb", buffering=0)]).run(detector, actions.append)
    assert actions == ["start", "stop", "edit:start", "edit:stop"]


# ---------- a dictionary that learns ----------

def test_corrections_from_an_edited_copy():
    assert corrections("push it to get hub today.", "Push it to GitHub today.") == [{"heard": "get hub", "written": "GitHub"}]
    assert corrections("deploy to versal now", "deploy to Vercel now") == [{"heard": "versal", "written": "Vercel"}]
    assert corrections("hello world", "something else entirely, a new text") == []  # not a corrected version
    assert corrections("hello world", "hello world") == []
    assert corrections("hello there, world", "hello there world") == []  # punctuation only
    long = "one two three four five six seven eight nine ten"
    assert corrections(long, long.replace("two three four five", "2 3 4 5")) == []  # more than 3 words at once


def test_learner_batches_and_waits_between_calls():
    cfg = Config(premium_learn=True)
    client, saved = FakePremium(), []
    learner = Learner(cfg, client, lambda terms, fixes: saved.append((terms, fixes)), every=0.5)
    learner.add("first")
    assert wait_for(lambda: len(client.learns) == 1)
    learner.add("second")
    learner.add("", fixed=[{"heard": "get hub", "written": "GitHub"}])
    learner.add("third")
    time.sleep(0.2)
    assert len(client.learns) == 1  # waits out the minute (here half a second)
    assert wait_for(lambda: len(client.learns) == 2)
    second = client.learns[1]
    assert second["transcript"] == "second\nthird" and second["corrections"] == [{"heard": "get hub", "written": "GitHub"}]
    assert client.learns[0]["transcript"] == "first"
    assert saved[0] == (["Learned"], {})


def test_learner_is_off_without_the_setting_or_premium():
    client = FakePremium()
    Learner(Config(premium_learn=False), client, lambda *a: None, every=0).add("text")
    Learner(Config(premium_learn=True), FakePremium(problem=premium.PREMIUM_ONLY), lambda *a: None, every=0).add("text")
    time.sleep(0.1)
    assert client.learns == []


def test_learner_keeps_what_you_removed_meanwhile():
    cfg = Config(premium_learn=True, learned_terms="Kubernetes, Vercel", learned_fixes={"get hub": "GitHub", "versal": "Vercel"})
    learner = Learner(cfg, FakePremium(), lambda *a: None)
    sent_terms, sent_fixes = ["Kubernetes", "Vercel"], dict(cfg.learned_fixes)
    cfg.learned_terms, cfg.learned_fixes = "Kubernetes", {"get hub": "GitHub"}  # you removed two in the settings
    terms, fixes = learner._apply(sent_terms, sent_fixes, ["Kubernetes", "Vercel", "pnpm"], {**sent_fixes, "nucs": "Next"})
    assert terms == ["Kubernetes", "pnpm"] and fixes == {"get hub": "GitHub", "nucs": "Next"}


def test_learning_after_dictation_and_from_a_corrected_copy(api):
    api.text = "push it to get hub"
    client, clipboard = FakePremium(), []
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, beeps=False, premium_learn=True)
    out, fb = Pasted(), FakeFeedback()

    def read_clipboard():
        clipboard.append(1)
        return "push it to GitHub"
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=out.paste, copier=out.copy, focus=lambda: "",
                  clipboard=read_clipboard, premium=client)
    d.learner.every = 0.3
    say(d)
    assert out.calls[0]["text"] == "push it to get hub"
    assert wait_for(lambda: len(client.learns) == 1)
    assert client.learns[0]["transcript"] == "push it to get hub" and client.learns[0]["corrections"] == []
    assert wait_for(lambda: cfg.learned_terms == "Learned")  # saved
    d.on_action("edit:start")  # the next press of either hotkey looks at the clipboard, once
    d.on_action("edit:stop")
    d.finish()
    assert wait_for(lambda: len(client.learns) == 2, 3)
    assert client.learns[1]["corrections"] == [{"heard": "get hub", "written": "GitHub"}]
    d.on_action("start")
    d.on_action("stop")
    settle(d)
    assert clipboard == [1]


def test_learned_fixes_apply_and_your_own_win():
    cfg = Config(premium_learn=True, learned_fixes={"get hub": "GitHub", "versal": "Versel"}, word_overrides={"Versal": "Vercel"})
    assert textproc.clean("push get hub to versal", cfg) == "push GitHub to Vercel"
    cfg.premium_learn = False
    assert textproc.clean("push get hub to versal", cfg) == "push get hub to Vercel"


def test_learned_terms_join_the_spelling_hint_within_the_limit():
    cfg = Config(provider="groq", prompt="Kubernetes", premium_learn=True, learned_terms="kubernetes, Vercel, pnpm, Vercel")
    assert language_hints(cfg)[1] == "Kubernetes Vercel, pnpm."
    cfg.learned_terms = ", ".join(f"term{i:03d}" for i in range(100))
    prompt = language_hints(cfg)[1]
    assert len(prompt.encode()) <= PROMPT_BYTES and "term000" in prompt and "term099" not in prompt
    cfg.premium_learn = False
    assert language_hints(cfg)[1] == "Kubernetes"
