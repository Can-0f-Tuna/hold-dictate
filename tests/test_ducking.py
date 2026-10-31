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
    d = Dictation(cfg, FakeFeedback(), recorder_factory=FakeRecorder, paster=lambda t, **kw: "", focus=lambda: "", ducker=Ducker(cfg, fake.pair))
    d.on_action("start")
    d.on_action("stop")
    d.ducker.close()
    for w in d.threads:
        w.join(5)
    assert fake.snapshots == 1 and fake.lowest() < 0.3 and fake.volume == 1.0


def test_pulse_backend_skips_our_own_streams_and_headphones(monkeypatch):
    sinks = [
        {"index": 1, "properties": {"device.bus": "pci"}, "active_port": "[Out] Speaker", "ports": [{"name": "[Out] Speaker", "type": "Speaker"}]},
        {"index": 2, "properties": {"device.bus": "bluetooth"}},
        {"index": 3, "properties": {"device.bus": "pci"}, "active_port": "[Out] Headphones", "ports": [{"name": "[Out] Headphones", "type": "Headphones"}]},
        {"index": 4, "properties": {"device.bus": "usb", "device.form_factor": "headset"}},
    ]
    vol = {"front-left": {"value": 65536}, "front-right": {"value": 32768}}
    streams = [
        {"index": 7, "sink": 1, "volume": vol, "properties": {"application.process.id": "1"}},
        {"index": 8, "sink": 1, "volume": {"mono": {"value": 65536}}, "properties": {"application.process.id": str(os.getpid())}},
        *({"index": 10 + n, "sink": n, "volume": vol, "properties": {}} for n in (2, 3, 4)),
    ]
    calls = []

    def run(args, **kw):
        calls.append(args)
        listed = sinks if args[-1] == "sinks" else streams
        return subprocess.CompletedProcess(args, 0, json.dumps(listed) if "list" in args else "", "")
    monkeypatch.setattr(subprocess, "run", run)
    saved = ducking._pulse_snapshot()
    ducking._pulse_apply(saved, 0.2)
    ducking._pulse_apply(saved, 1.0)
    changes = [c for c in calls if "set-sink-input-volume" in c]
    # 0.2 in amplitude is 58% on the cubic pactl scale (-14 dB); 20% there would be -42 dB, nearly silent.
    assert changes == [
        ["pactl", "set-sink-input-volume", "7", "38326", "19163"],
        ["pactl", "set-sink-input-volume", "7", "65536", "32768"],
    ]


def test_mac_lowers_only_running_players(monkeypatch):
    scripts = []

    def run(args, **kw):
        script = args[-1]
        scripts.append(script)
        out = "80" if "Spotify" in script and "get sound volume" in script else ""
        return subprocess.CompletedProcess(args, 0, out, "")
    monkeypatch.setattr(subprocess, "run", run)
    saved = ducking._mac_snapshot()
    assert saved == [("Spotify", 80)]
    ducking._mac_apply(saved, 0.2)
    assert scripts[-1].endswith('tell application "Spotify" to set sound volume to 16')
    assert not any("output volume" in s for s in scripts)  # the system volume shows a popup
