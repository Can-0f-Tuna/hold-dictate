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


class Lines:
    def __init__(self):
        self.lines = []

    def write(self, text):
        self.lines.append(text.strip())


def test_the_premium_notice_comes_with_the_next_dictation_until_it_is_clicked(monkeypatch):
    fb = feedback.Feedback.__new__(feedback.Feedback)
    fb.cfg, fb.proc, fb.premium_sent = None, None, False
    fb.events = Lines()
    monkeypatch.setattr(fb, "_ensure_overlay", lambda: None)
    fb.pending_update = lambda: ""

    fb.pending_premium = lambda: False
    fb.recording()
    assert "P on" not in fb.events.lines
    fb.premium_gone()
    assert "P -" not in fb.events.lines

    fb.pending_premium = lambda: True
    fb.recording()
    assert fb.events.lines[-2:] == ["P on", "S"]  # before S, so the ball comes with the pill
    fb.premium_gone()
    fb.premium_gone()
    assert fb.events.lines.count("P -") == 1
