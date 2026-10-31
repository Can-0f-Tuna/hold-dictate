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


def pill():
    fb = feedback.Feedback.__new__(feedback.Feedback)
    fb.cfg, fb.events = None, Lines()
    fb.proc = type("P", (), {"poll": lambda self: None})()
    return fb


def test_a_problem_marks_the_pill_and_keeps_the_explanation_for_the_settings(monkeypatch):
    fb, notified = pill(), []
    monkeypatch.setattr(fb, "notify", notified.append)
    fb.error("Groq returned 401: long detail", "The API key was rejected.")
    assert fb.events.lines == ["E alert"]
    assert fb.last_problem == {"brief": "The API key was rejected.", "detail": "Groq returned 401: long detail"}
    fb.error("no microphone", icon="mic")
    assert fb.events.lines[-1] == "E mic"
    assert fb.last_problem == {"brief": "No microphone", "detail": ""}
    assert notified == []
    fb.clear_problem()
    assert fb.last_problem is None


def test_without_the_pill_a_problem_is_a_notification(monkeypatch):
    fb, notified = pill(), []
    fb.proc, fb.events = None, None
    monkeypatch.setattr(fb, "notify", notified.append)
    fb.error("no API key yet.")
    assert notified == ["No API key yet."]
