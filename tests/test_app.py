import os
import struct
import sys
import threading
import time
from collections import deque

import pytest

import numpy as np

from hold_dictate import audio
from hold_dictate.app import Dictation
from hold_dictate.config import Config
from hold_dictate.live import LiveTyper
from hold_dictate.hotkey import EV_FORMAT, EvdevBackend, HoldDetector, parse_hotkey


class FakeFeedback:
    def __init__(self):
        self.log = []

    def level(self, v): pass
    def recording(self): self.log.append("recording")
    def working(self): self.log.append("working")
    def done(self): self.log.append("done")
    def error(self, m, brief="", icon="alert"): self.log.append(("error", brief or m))
    def notify(self, m): self.log.append(("notify", m))
    def clear_problem(self): pass


class FakeRecorder:
    def __init__(self, on_level):
        t = np.arange(audio.SAMPLE_RATE) / audio.SAMPLE_RATE
        self.audio = (np.sin(2 * np.pi * 220 * t) * 0.3).astype(np.float32)

    def stop(self):
        return np.concatenate([np.zeros(8000, np.float32), self.audio, np.zeros(8000, np.float32)])


def ev(code, value):
    return struct.pack(EV_FORMAT, 0, 0, 1, code, value)


def drive(cfg, hold_seconds):
    """Runs a hold of the hotkey through the real detector. Linux uses the evdev reader on a pipe;
    elsewhere events are fed straight to the detector, which is what the pynput backend does."""
    pasted, fb = [], FakeFeedback()
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=lambda t, **kw: pasted.append(t) or "", focus=lambda: "")
    detector = HoldDetector(parse_hotkey(cfg.hotkey))
    if sys.platform.startswith("linux"):
        r, w = os.pipe()
        backend = EvdevBackend([os.fdopen(r, "rb", buffering=0)])
        t = threading.Thread(target=backend.run, args=(detector, d.on_action), daemon=True)
        t.start()
        os.write(w, ev(56, 1) + ev(57, 1))
        time.sleep(hold_seconds)
        os.write(w, ev(57, 0) + ev(56, 0))
        os.close(w)
        t.join(2)
    else:
        for name, ident, down in [("alt", 56, True), ("space", 57, True)]:
            action = detector.feed(name, ident, down)
            action and d.on_action(action)
        time.sleep(hold_seconds)
        for name, ident in [("space", 57), ("alt", 56)]:
            action = detector.feed(name, ident, False)
            action and d.on_action(action)
    for worker in d.threads:
        worker.join(5)
    return pasted, fb


def test_full_dictation_flow(api):
    api.text = "dictated text"
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, beeps=False)
    pasted, fb = drive(cfg, 0.3)
    assert pasted == ["dictated text"]
    assert fb.log == ["recording", "working", "done"]


def test_short_tap_is_ignored(api):
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.5, beeps=False)
    pasted, fb = drive(cfg, 0.05)
    assert pasted == [] and api.requests == []


def test_api_failure_is_reported_not_pasted(api):
    api.status = 401
    cfg = Config(provider="custom", base_url=api.url, api_key="bad", min_hold=0.1, beeps=False, local_fallback=False)
    pasted, fb = drive(cfg, 0.3)
    assert pasted == [] and any(isinstance(e, tuple) and e[0] == "error" for e in fb.log)


class Pasted:
    """Records what Dictation pastes, with the options it chose."""

    def __init__(self):
        self.calls, self.copied = [], []

    def paste(self, text, app=None, shift=None, submit=False):
        self.calls.append({"text": text, "app": app, "shift": shift, "submit": submit})
        return ""

    def copy(self, text):
        self.copied.append(text)
        return ""


def dictation(cfg, app="", recorder=FakeRecorder):
    out, fb = Pasted(), FakeFeedback()
    d = Dictation(cfg, fb, recorder_factory=recorder, paster=out.paste, copier=out.copy, focus=lambda: app)
    return d, out, fb


def settle(d):
    with d.lock:
        workers = list(d.threads)
    for worker in workers:
        worker.join(5)


def tap(d, seconds=0.05):
    d.on_action("start")
    time.sleep(seconds)
    d.on_action("stop")


def cfg_for(api, **kw):
    return Config(**{"provider": "custom", "base_url": api.url, "api_key": "k", "min_hold": 0.1, "beeps": False, **kw})


def test_auto_mode_tap_starts_and_next_tap_stops(api):
    d, out, _ = dictation(cfg_for(api, hotkey_mode="auto"))
    tap(d)
    assert d.recorder is not None and d.latched  # a quick tap keeps recording
    time.sleep(0.2)
    tap(d)
    settle(d)
    assert d.recorder is None and [c["text"] for c in out.calls] == ["hello world"]


def test_auto_mode_long_hold_works_like_hold(api):
    d, out, _ = dictation(cfg_for(api, hotkey_mode="auto"))
    tap(d, 0.5)
    settle(d)
    assert d.recorder is None and len(out.calls) == 1


def test_toggle_mode_ignores_release(api):
    d, out, _ = dictation(cfg_for(api, hotkey_mode="toggle"))
    tap(d, 0.5)
    assert d.recorder is not None
    tap(d)
    settle(d)
    assert len(out.calls) == 1


def test_hold_mode_short_tap_still_ignored(api):
    d, out, _ = dictation(cfg_for(api, min_hold=0.3))
    tap(d)
    settle(d)
    assert d.recorder is None and out.calls == [] and api.requests == []


class TalkingRecorder(FakeRecorder):
    """Feeds levels like a real mic: speech for `talk` seconds, then quiet."""

    def __init__(self, on_level, talk=0.3):
        super().__init__(on_level)
        self.running = True
        started = time.monotonic()

        def feed():
            while self.running:
                on_level(0.05 if time.monotonic() - started >= talk else 0.7)  # >=: a coarse clock (Windows) reads 0 at first
                time.sleep(0.02)
        threading.Thread(target=feed, daemon=True).start()

    def stop(self):
        self.running = False
        return super().stop()


def test_silence_stops_a_tapped_recording(api):
    d, out, _ = dictation(cfg_for(api, hotkey_mode="auto", silence_stop=0.4), recorder=TalkingRecorder)
    tap(d)
    deadline = time.monotonic() + 3
    while d.recorder is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert d.recorder is None, "the recording should stop on its own"
    stopped_after = time.monotonic() - d.pressed_at
    assert 0.6 <= stopped_after < 1.5  # 0.3 s of speech, then 0.4 s of quiet
    settle(d)
    assert len(out.calls) == 1


def test_silence_stop_waits_while_you_talk_and_is_off_at_zero(api):
    d, _, _ = dictation(cfg_for(api, hotkey_mode="toggle", silence_stop=0.3), recorder=lambda lv: TalkingRecorder(lv, talk=99))
    tap(d)
    time.sleep(0.8)
    assert d.recorder is not None  # still talking
    d.finish()
    d, _, _ = dictation(cfg_for(api, hotkey_mode="toggle"), recorder=TalkingRecorder)
    tap(d)
    time.sleep(0.8)
    assert d.recorder is not None  # silence_stop = 0: only a tap stops it
    d.finish()
    settle(d)


def test_silence_stop_gives_up_when_nothing_is_said(api, monkeypatch):
    import hold_dictate.app as app_mod
    monkeypatch.setattr(app_mod, "NO_SPEECH_SECONDS", 1.5)
    d, _, _ = dictation(cfg_for(api, hotkey_mode="auto", silence_stop=0.2), recorder=lambda lv: TalkingRecorder(lv, talk=0))
    tap(d)
    time.sleep(0.5)
    assert d.recorder is not None  # waits NO_SPEECH_SECONDS, not silence_stop, before anything is said
    deadline = time.monotonic() + 10  # slow CI runners oversleep; poll instead of one fixed wait
    while d.recorder is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert d.recorder is None
    settle(d)


def test_text_is_cleaned_before_pasting(api):
    api.text = "Um, push it to get hub."
    d, out, _ = dictation(cfg_for(api, remove_fillers=True, word_overrides={"get hub": "GitHub"}))
    tap(d, 0.3)
    settle(d)
    assert out.calls[0]["text"] == "Push it to GitHub."


def test_hallucination_is_not_pasted(api):
    api.text = "Thanks for watching!"
    d, out, _ = dictation(cfg_for(api))
    tap(d, 0.3)
    settle(d)
    assert out.calls == [] and d.last_text == ""


@pytest.mark.parametrize("rules, auto_submit, app, expect", [
    ({}, False, "firefox", {"shift": None, "submit": False}),
    ({}, True, "firefox", {"shift": None, "submit": True}),
    ({"slack": "submit"}, False, "Slack", {"shift": None, "submit": True}),
    ({"code": "paste"}, True, "code-oss", {"shift": False, "submit": False}),  # paste overrides auto-submit
    ({"emacs": "shift"}, False, "emacs", {"shift": True, "submit": False}),
])
def test_app_rules_choose_how_to_paste(api, rules, auto_submit, app, expect):
    d, out, _ = dictation(cfg_for(api, app_rules=rules, auto_submit=auto_submit), app=app)
    tap(d, 0.3)
    settle(d)
    call = out.calls[0]
    assert {"shift": call["shift"], "submit": call["submit"]} == expect and call["app"] == app


def test_app_rules_copy_only_and_off(api):
    d, out, _ = dictation(cfg_for(api, app_rules={"keepass": "clipboard"}), app="KeePassXC")
    tap(d, 0.3)
    settle(d)
    assert out.calls == [] and out.copied == ["hello world"]
    d, out, _ = dictation(cfg_for(api, app_rules={"keepass": "off"}), app="KeePassXC")
    tap(d, 0.3)
    settle(d)
    assert out.calls == [] and out.copied == [] and d.last_text == "hello world"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell commands")
def test_post_hook_changes_or_drops_text(api):
    d, out, _ = dictation(cfg_for(api, post_hook='printf "[%s] " "$HOLD_DICTATE_APP"; cat'), app="kitty")
    tap(d, 0.3)
    settle(d)
    assert out.calls[0]["text"] == "[kitty] hello world"
    d, out, _ = dictation(cfg_for(api, post_hook="exit 77"))
    tap(d, 0.3)
    settle(d)
    assert out.calls == []


def test_paste_and_copy_last(api):
    d, out, _ = dictation(cfg_for(api, auto_submit=True), app="slack")
    assert d.paste_last() == "nothing dictated yet" and d.copy_last() == "nothing dictated yet"
    tap(d, 0.3)
    settle(d)
    assert d.last_text == "hello world" and d.last_app == "slack"
    assert d.paste_last() == "" and out.calls[-1] == {"text": "hello world", "app": "slack", "shift": None, "submit": False}
    assert d.copy_last() == "" and out.copied == ["hello world"]


class LiveRecorder(FakeRecorder):
    def snapshot(self):
        return self.stop()


def wait_for(check):
    deadline = time.monotonic() + 3
    while not check() and time.monotonic() < deadline:
        time.sleep(0.02)


TONE = (np.sin(2 * np.pi * 220 * np.arange(2 * audio.SAMPLE_RATE) / audio.SAMPLE_RATE) * 0.3).astype(np.float32)
QUIET = np.zeros(audio.SAMPLE_RATE // 2, np.float32)


class Clips:
    """snapshot() gives each clip once, the last one for ever."""

    def __init__(self, *clips):
        self.clips = list(clips)

    def snapshot(self):
        return self.clips.pop(0) if len(self.clips) > 1 else self.clips[0]


def typer(recorder, heard, typed=None, **kw):
    typed = [] if typed is None else typed
    LiveTyper.sent.clear()
    return LiveTyper(recorder, lambda s: next(heard), str, lambda t: typed.append(t) or "", gap=0.0, **kw), typed


def test_words_are_typed_while_you_speak_and_the_rest_at_the_end(api, monkeypatch):
    monkeypatch.setattr(LiveTyper, "GAP", 0.0)
    monkeypatch.setattr(LiveTyper, "FAST", 0.0)
    monkeypatch.setattr(LiveTyper, "sent", deque())
    api.text = "one two three four"
    typed, fb = [], FakeFeedback()
    d = Dictation(cfg_for(api, live_text=True, min_hold=0), fb, recorder_factory=LiveRecorder,
                  paster=lambda t, **kw: typed.append(t) or "", focus=lambda: "")
    d.on_action("start")
    wait_for(lambda: typed)
    d.on_action("stop")
    settle(d)
    assert "".join(typed) == "one two three four"
    assert d.last_text == "one two three four"
    assert not any(isinstance(e, tuple) and e[0] == "error" for e in fb.log)


def test_the_first_requests_come_quickly_and_the_rest_keep_to_the_limit():
    live, _ = typer(Clips(TONE), iter([]))
    live.gap = 3.7
    LiveTyper.sent.append(time.monotonic() - 2)
    assert live.may_send()
    live.steps = LiveTyper.FAST_STEPS
    assert not live.may_send()
    live.steps = 0
    LiveTyper.sent.extend([time.monotonic() - 10] * LiveTyper.PER_MINUTE)
    live.steps = 0
    assert not live.may_send()


def test_a_failed_request_pauses_live_typing_and_it_comes_back():
    clip = np.concatenate([TONE, TONE, QUIET])
    typed = []
    answers = iter([RuntimeError("limit"), "one two"])

    def heard(_):
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    LiveTyper.sent.clear()
    live = LiveTyper(Clips(clip), heard, str, lambda t: typed.append(t) or "", gap=0.0)
    live.AFTER_ERROR = 0.1
    live.start()
    wait_for(lambda: typed)
    live.stop()
    assert typed == ["one two"]


def test_the_last_words_of_an_unfinished_phrase_wait():
    clip = np.concatenate([QUIET, TONE, TONE])
    live, typed = typer(Clips(clip), iter(["one two three", "one two three four five"]))

    def stop_after_two(text):
        typed.append(text)
        if len(typed) == 2:
            live.stop()
        return ""
    live.type_text = stop_after_two
    live.run()
    assert typed == ["one", " two three"]


def test_a_finished_phrase_is_typed_whole_and_the_next_one_follows_it():
    first = np.concatenate([TONE, QUIET])
    live, typed = typer(Clips(first, np.concatenate([first, QUIET, TONE, QUIET])), iter(["one", "two"]))

    def stop_after_two(text):
        typed.append(text)
        if len(typed) == 2:
            live.stop()
        return ""
    live.type_text = stop_after_two
    live.run()
    assert typed == ["one", " two"]


def test_a_failed_live_request_leaves_the_rest_for_the_last_pass():
    clip = np.concatenate([TONE, QUIET])
    live, typed = typer(Clips(clip), iter([]))

    calls = []

    def fail(_):
        calls.append(1)
        raise RuntimeError("429")
    live.transcribe = fail
    live.start()
    wait_for(lambda: calls)
    assert typed == []
    assert live.finish(clip, lambda s: "all of it") == "all of it" and typed == ["all of it"]


def test_nothing_is_sent_while_the_held_keys_would_get_in_the_way():
    calls = []
    live, _ = typer(Clips(np.concatenate([TONE, QUIET])), iter([]), ready=lambda: False)
    live.transcribe = calls.append
    live.start()
    time.sleep(0.5)
    live.stop()
    assert calls == []


@pytest.mark.parametrize("settings", [{"post_hook": "cat"}, {"app_rules": {"slack": "clipboard"}}, {"app_rules": {"slack": "off"}}])
def test_live_typing_stays_out_of_the_way_of_what_changes_the_text(api, settings):
    d, _, _ = dictation(cfg_for(api, live_text=True, **settings), app="slack")
    assert not d._types_live("")


def test_a_dead_microphone_is_explained(api):
    class Muted:
        def __init__(self, on_level):
            pass

        def stop(self):
            return np.zeros(audio.SAMPLE_RATE * 2, np.float32)
    d, _, fb = dictation(cfg_for(api, min_hold=0), recorder=Muted)
    d.on_action("start")
    d.on_action("stop")
    settle(d)
    assert any(isinstance(e, tuple) and "microphone" in e[1] for e in fb.log)
