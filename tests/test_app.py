import os
import struct
import sys
import threading
import time

import pytest

import numpy as np

from hold_dictate import audio
from hold_dictate.app import Dictation
from hold_dictate.config import Config
from hold_dictate.hotkey import EV_FORMAT, EvdevBackend, HoldDetector, parse_hotkey


class FakeFeedback:
    def __init__(self):
        self.log = []

    def level(self, v): pass
    def recording(self): self.log.append("recording")
    def working(self): self.log.append("working")
    def done(self): self.log.append("done")
    def error(self, m): self.log.append(("error", m))
    def notify(self, m): self.log.append(("notify", m))


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
    d = Dictation(cfg, fb, recorder_factory=FakeRecorder, paster=lambda t: pasted.append(t) or "")
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
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.1, sounds=False)
    pasted, fb = drive(cfg, 0.3)
    assert pasted == ["dictated text"]
    assert fb.log == ["recording", "working", "done"]


def test_short_tap_is_ignored(api):
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.5, sounds=False)
    pasted, fb = drive(cfg, 0.05)
    assert pasted == [] and api.requests == []


def test_api_failure_is_reported_not_pasted(api):
    api.status = 401
    cfg = Config(provider="custom", base_url=api.url, api_key="bad", min_hold=0.1, sounds=False, local_fallback=False)
    pasted, fb = drive(cfg, 0.3)
    assert pasted == [] and any(isinstance(e, tuple) and e[0] == "error" for e in fb.log)
