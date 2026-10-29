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
        self.log = []

    def lower(self, factor):
        before, self.volume = self.volume, self.volume * factor
        self.log.append(("lower", factor))
        return before

    def restore(self, before):
        self.volume = before
        self.log.append("restore")


def test_lower_then_restore():
    fake = FakeBackend()
    d = Ducker(Config(background_audio="lower"), (fake.lower, fake.restore))
    d.duck()
    d.close()
    assert fake.log == [("lower", ducking.LOWER), "restore"] and fake.volume == 1.0


def test_mute_and_off():
    fake = FakeBackend()
    cfg = Config(background_audio="mute")
    d = Ducker(cfg, (fake.lower, fake.restore))
    d.duck()
    d.restore()
    cfg.background_audio = "off"  # the settings page changed it
    d.duck()
    d.close()
    assert fake.log == [("lower", 0.0), "restore"]


def test_double_duck_keeps_the_original_volume():
    fake = FakeBackend()
    d = Ducker(Config(), (fake.lower, fake.restore))
    d.duck()
    d.duck()
    d.close()
    assert fake.volume == 1.0 and fake.log.count(("lower", ducking.LOWER)) == 1


def test_a_failing_backend_does_not_break_dictation(capsys):
    def broken(_factor):
        raise RuntimeError("no audio server")
    d = Ducker(Config(), (broken, lambda s: None))
    d.duck()
    d.duck()
    d.close()
    assert capsys.readouterr().err.count("no audio server") == 1


def test_dictation_ducks_while_recording(api):
    from hold_dictate.app import Dictation
    fake = FakeBackend()
    cfg = Config(provider="custom", base_url=api.url, api_key="k", min_hold=0.0)
    d = Dictation(cfg, FakeFeedback(), recorder_factory=FakeRecorder, paster=lambda t: "", ducker=Ducker(cfg, (fake.lower, fake.restore)))
    d.on_action("start")
    d.on_action("stop")
    d.ducker.close()
    for w in d.threads:
        w.join(5)
    assert fake.log == [("lower", ducking.LOWER), "restore"] and fake.volume == 1.0


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
    saved = ducking._pulse_lower(0.2)
    ducking._pulse_restore(saved)
    assert calls[1] == ["pactl", "set-sink-input-volume", "7", "13107", "6553"]
    assert calls[2] == ["pactl", "set-sink-input-volume", "7", "65536", "32768"]
    assert len(calls) == 3
