import json
import os
import subprocess

from hold_dictate import ducking
from hold_dictate.config import Config
from hold_dictate.ducking import Ducker

from test_app import FakeFeedback, FakeRecorder


class FakeBackend:
    def __init__(self):
        self.volume = 1.0
        self.snapshots = 0
        self.gains = []

    def snapshot(self):
        self.snapshots += 1
        return self.volume

    def apply(self, before, gain):
        self.volume = before * gain
        self.gains.append(gain)

    @property
    def pair(self):
        return self.snapshot, self.apply

    def lowest(self):
        return min(self.gains)


def test_fades_down_then_back_up():
    fake = FakeBackend()
    d = Ducker(Config(background_audio="lower"), fake.pair)
    d.duck()
    d.restore()
    d.close()
    low = fake.gains.index(fake.lowest())
    down, up = fake.gains[: low + 1], fake.gains[low:]
    assert len(down) > 2 and len(up) > 2, "a fade, not a jump"
    assert down == sorted(down, reverse=True) and up == sorted(up)
    assert abs(fake.lowest() - ducking.gain_at(1, 0, ducking.LOWER_DB)) < 1e-9 and fake.volume == 1.0


def test_lower_keeps_the_music_audible():
    assert 0.15 < ducking.gain_at(1, 0, ducking.LOWER_DB) < 0.3  # about -14 dB, not silence
    assert ducking.gain_at(1, 0, -60) == 0.0


def test_mute_and_off():
    fake = FakeBackend()
    cfg = Config(background_audio="mute")
    d = Ducker(cfg, fake.pair)
    d.duck()
    d.restore()
    d.close()
    assert fake.lowest() == 0.0 and fake.volume == 1.0
    cfg.background_audio = "off"  # the settings page changed it
    fake.gains.clear()
    d.duck()
    d.close()
    assert fake.gains == [] and fake.snapshots == 1


def test_double_duck_keeps_the_original_volume():
    fake = FakeBackend()
    d = Ducker(Config(), fake.pair)
    d.duck()
    d.duck()
    d.close()
    assert fake.snapshots == 1 and fake.volume == 1.0


def test_exit_restores_at_once():
    fake = FakeBackend()
    d = Ducker(Config(), fake.pair)
    d.duck()
    d.close()
    assert fake.gains[-1] == 1.0 and fake.gains[-2] < 0.3  # no fade on the way out


def test_a_failing_backend_does_not_break_dictation(capsys):
    def broken():
        raise RuntimeError("no audio server")
    d = Ducker(Config(), (broken, lambda s, g: None))
    d.duck()
    d.duck()
    d.close()
    assert capsys.readouterr().err.count("no audio server") == 1


def test_dictation_ducks_while_recording(api):
    from hold_dictate.app import Dictation
    fake = FakeBackend()
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.0)
    d = Dictation(cfg, FakeFeedback(), recorder_factory=FakeRecorder, paster=lambda t: "", ducker=Ducker(cfg, fake.pair))
    d.on_action("start")
    d.on_action("stop")
    d.ducker.close()
    for w in d.threads:
        w.join(5)
    assert fake.snapshots == 1 and fake.lowest() < 0.3 and fake.volume == 1.0


def test_pulse_backend_skips_our_own_streams(monkeypatch):
    streams = [
        {"index": 7, "volume": {"front-left": {"value": 65536}, "front-right": {"value": 32768}}, "properties": {"application.process.id": "1"}},
        {"index": 8, "volume": {"mono": {"value": 65536}}, "properties": {"application.process.id": str(os.getpid())}},
    ]
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, json.dumps(streams) if "list" in args else "", "")
    monkeypatch.setattr(subprocess, "run", run)
    saved = ducking._pulse_snapshot()
    ducking._pulse_apply(saved, 0.2)
    ducking._pulse_apply(saved, 1.0)
    # 0.2 in amplitude is 58% on the cubic pactl scale (-14 dB); 20% there would be -42 dB, nearly silent.
    assert calls[1] == ["pactl", "set-sink-input-volume", "7", "38326", "19163"]
    assert calls[2] == ["pactl", "set-sink-input-volume", "7", "65536", "32768"]
    assert len(calls) == 3
