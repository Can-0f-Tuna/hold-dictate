from hold_dictate import feedback
from hold_dictate.config import Config


def test_beeps_play_even_when_the_pill_is_showing(monkeypatch):
    played = []
    monkeypatch.setattr(feedback.threading, "Thread", lambda target, daemon: type("T", (), {"start": lambda self: played.append(target)})())
    fb = feedback.Feedback.__new__(feedback.Feedback)
    fb.cfg, fb.events = Config(beeps=True), object()  # the Linux pill is running
    fb._beep("start")
    fb.cfg.beeps = False
    fb._beep("stop")
    assert len(played) == 1
