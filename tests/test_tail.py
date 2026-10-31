"""The release tail, through the real Recorder and Dictation. Only the sound device is fake."""
import sys
import threading
import time
import types

import numpy as np
import pytest

from hold_dictate import audio
from hold_dictate.app import Dictation
from hold_dictate.config import Config
from hold_dictate.live import LiveTyper

from test_app import FakeFeedback, Pasted, settle

SPEECH = (np.sin(2 * np.pi * 220 * np.arange(audio.BLOCK) / audio.SAMPLE_RATE) * 8000).astype(np.int16)
ROOM = np.full(audio.BLOCK, 20, np.int16)  # a quiet room: not a dead mic


def talking(t):
    return ROOM if t < 0.1 else SPEECH  # a clip that is loud from end to end doesn't count as speech


class Device:
    """Stands in for sounddevice. `sound(seconds)` says what the mic hears; None means the stream has stalled."""

    def __init__(self, sound=lambda t: talking(t), hang=False):
        self.sound, self.hang = sound, hang
        self.streams = []
        self.log = []

    def InputStream(self, samplerate, channels, dtype, blocksize, callback):
        stream = Stream(self, callback)
        self.streams.append(stream)
        return stream


class Stream:
    def __init__(self, device, callback):
        self.device, self.callback = device, callback
        self.running = True
        self.stopped_at = None

    def start(self):
        self.device.log.append("open")
        threading.Thread(target=self.feed, daemon=True).start()

    def feed(self):
        # Blocks follow the clock, as a sound card's do, however late this thread wakes up.
        began, sent = time.monotonic(), 0
        while self.running:
            while self.running and sent * audio.BLOCK / audio.SAMPLE_RATE <= time.monotonic() - began:
                block = self.device.sound(sent * audio.BLOCK / audio.SAMPLE_RATE)
                sent += 1
                if block is not None:
                    self.callback(block.reshape(-1, 1).copy(), len(block), None, None)
            time.sleep(0.005)

    def stop(self):
        if self.device.hang:
            threading.Event().wait()
        self.running = False
        self.stopped_at = time.monotonic()
        self.device.log.append("close")

    def close(self):
        pass


class Duck:
    def __init__(self, log):
        self.log = log

    def duck(self):
        pass

    def restore(self):
        self.log.append("restore")


class Feedback(FakeFeedback):
    def working(self):
        super().working()
        self.worked_at = time.monotonic()


@pytest.fixture
def device(monkeypatch):
    def make(sound=lambda t: talking(t), hang=False):
        found = Device(sound, hang)
        monkeypatch.setitem(sys.modules, "sounddevice", types.SimpleNamespace(InputStream=found.InputStream))
        return found
    monkeypatch.setattr(audio.Recorder, "closing", None)
    return make


def session(device, **settings):
    """A Dictation that records through `device` and hands each clip to `clips` instead of a service."""
    fb, out, clips = Feedback(), Pasted(), []
    cfg = Config(**{"api_key": "k", "min_hold": 0.1, "beeps": False, "background_audio": "off", **settings})
    d = Dictation(cfg, fb, paster=out.paste, copier=out.copy, focus=lambda: "app", ducker=Duck(device.log))
    d.transcribe = lambda speech: (clips.append((time.monotonic(), speech)) or "words", "test")
    return d, fb, out, clips


def hold(d, seconds=0.3):
    d.on_action("start")
    time.sleep(seconds)
    d.on_action("stop")
    return time.monotonic()


def test_recording_runs_on_for_the_tail_and_then_stops(device):
    mic = device()
    d, fb, out, clips = session(mic)
    released = hold(d)
    assert not d.idle()  # an update must not restart the app under the tail
    settle(d)
    assert 0.19 <= mic.streams[0].stopped_at - released <= 0.3
    assert len(clips[0][1]) >= 0.45 * audio.SAMPLE_RATE  # 0.3 s held and the tail
    assert [c["text"] for c in out.calls] == ["words"]


def test_the_release_is_heard_at_once_though_the_mic_stays_on(device):
    mic = device()
    d, fb, _, _ = session(mic)
    released = hold(d)
    assert mic.log == ["open", "restore"] and fb.log[-1] == "working"  # the stop sound comes from working()
    assert fb.worked_at - released < 0.05 and mic.streams[0].running
    settle(d)
    assert mic.log == ["open", "restore", "close"]


def test_a_stalled_stream_finishes_in_time_with_what_arrived(device):
    began = []

    def stalls(t):
        began.append(t)
        return talking(t) if not released else None
    released = []
    mic = device(stalls, hang=True)  # no more audio, and stream.stop() never returns
    d, _, out, clips = session(mic)
    d.on_action("start")
    time.sleep(0.3)
    released.append(1)
    at = time.monotonic()
    d.on_action("stop")
    settle(d)
    assert clips[0][0] - at <= 0.6  # tail + 100 ms, with room for a busy CI machine
    assert 0.2 * audio.SAMPLE_RATE < len(clips[0][1]) and len(out.calls) == 1


def test_a_clip_under_min_hold_gets_no_tail(device):
    mic = device()
    d, _, out, clips = session(mic, min_hold=0.5)
    hold(d, 0.05)
    assert mic.log[-1] == "close"  # already, as the key came up
    settle(d)
    assert clips == [] and out.calls == []


def test_silence_stop_in_a_tapped_mode_gets_no_tail(device):
    mic = device(lambda t: talking(t) if t < 0.3 else ROOM)
    d, fb, _, clips = session(mic, hotkey_mode="auto", silence_stop=0.3)
    hold(d, 0.05)
    deadline = time.monotonic() + 3
    while "working" not in fb.log and time.monotonic() < deadline:
        time.sleep(0.01)
    settle(d)
    assert mic.streams[0].stopped_at - fb.worked_at < 0.1 and len(clips) == 1


def test_a_press_during_the_tail_keeps_both_clips_in_order(device):
    mic = device()
    d, _, out, clips = session(mic)
    hold(d)
    time.sleep(0.05)
    released = hold(d)
    settle(d)
    assert mic.log == ["open", "restore", "close", "open", "restore", "close"]  # never two at once
    assert len(clips) == 2 and len(out.calls) == 2
    assert 0.25 * audio.SAMPLE_RATE <= len(clips[0][1])  # the fake mic can run a little slow
    assert 0.19 <= mic.streams[1].stopped_at - released <= 0.3


def test_the_press_cuts_the_old_tail_so_its_first_words_are_not_lost(device):
    mic = device()
    d, _, _, _ = session(mic)
    hold(d)
    time.sleep(0.05)
    pressed = time.monotonic()
    d.on_action("start")
    assert time.monotonic() - pressed < 0.1 and d.recorder is not None
    d.on_action("stop")
    settle(d)


@pytest.mark.parametrize("hit_the_key_at", [0.0, 0.05])
def test_a_key_click_after_the_release_does_not_make_a_silent_clip_speak(device, hit_the_key_at):
    released = []

    def room_then_click(t):
        return SPEECH if released and time.monotonic() - released[0] >= hit_the_key_at else ROOM
    mic = device(room_then_click)
    d, fb, out, clips = session(mic)
    d.on_action("start")
    time.sleep(0.4)
    released.append(time.monotonic())
    d.on_action("stop")
    settle(d)
    assert clips == [] and out.calls == [] and "done" in fb.log


def test_a_click_after_real_speech_keeps_the_clip(device):
    released = []
    mic = device(lambda t: talking(t) if t < 0.35 or released else ROOM)
    d, _, out, _ = session(mic)
    d.on_action("start")
    time.sleep(0.4)
    released.append(1)
    d.on_action("stop")
    settle(d)
    assert len(out.calls) == 1


def test_live_typing_does_not_send_the_tail_of_a_finished_phrase():
    tone = (SPEECH / 32768).astype(np.float32)
    said = np.tile(tone, 100)  # typed already
    quiet = np.zeros(audio.SAMPLE_RATE // 2, np.float32)
    click = np.tile(tone, 8)
    heard = []
    live = LiveTyper(None, None, str, lambda text: "")
    live.start_at = len(said)
    clip = np.concatenate([said, quiet, click])
    assert live.finish(clip, lambda s: heard.append(s) or "ghost", released=len(said) + len(quiet)) == ""
    assert heard == []


def test_live_typing_still_gets_the_last_word_through_the_tail():
    tone = (SPEECH / 32768).astype(np.float32)
    clip = np.concatenate([np.zeros(audio.SAMPLE_RATE // 2, np.float32), np.tile(tone, 100)])
    live = LiveTyper(None, None, str, lambda text: "")
    assert live.finish(clip, lambda s: f"{len(s) > audio.SAMPLE_RATE}", released=len(clip) - 3200) == "True"


def test_a_hung_device_does_not_stop_the_next_press_for_long(device):
    mic = device(hang=True)
    first = audio.Recorder()
    time.sleep(0.05)
    begun = time.monotonic()
    first.stop()
    assert time.monotonic() - begun < 0.2
    mic.hang = False
    begun = time.monotonic()
    audio.Recorder().stop()
    assert time.monotonic() - begun < 1
