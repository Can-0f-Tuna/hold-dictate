import subprocess
import sys
import time

import pytest

from hold_dictate import config, feedback, indicator
from hold_dictate.config import Config
from hold_dictate.indicator import Indicator, Pill


def run(pill, seconds=0.2, dt=1 / 30):
    for _ in range(round(seconds / dt)):
        pill.step(dt)


def fed(pill, *lines):
    for line in lines:
        pill.feed(line)
    pill.step(0)


def test_the_bars_follow_the_voice_then_the_pill_closes_and_stops_asking_for_frames():
    pill = Pill()
    assert not pill.wants_frames()
    fed(pill, "S")
    run(pill)
    assert pill.shown == 1
    quiet = max(pill.heights)
    fed(pill, "L 0.9")
    run(pill)
    assert max(pill.heights) > quiet * 3
    fed(pill, "T")
    run(pill, 1)
    assert pill.level == 0 and max(pill.heights) < 0.6
    fed(pill, "Q")
    run(pill)
    assert not pill.wants_frames()  # no frames means the helper sleeps between recordings


def test_a_problem_stays_up_past_the_close_and_then_leaves():
    pill = Pill()
    fed(pill, "S", "E mic", "Q")  # the app says done right after the error
    run(pill, 1)
    assert pill.wants_frames() and pill.icon == "mic"
    assert pill.mark > 0.9 and pill.bar_color()[0] > 0.8  # red, flat, with its mark
    assert max(pill.heights) < 0.05
    run(pill, Pill.STAY + 0.5)
    assert not pill.wants_frames() and pill.icon == ""


def test_a_problem_can_open_the_pill_and_a_new_recording_clears_it():
    pill = Pill()
    fed(pill, "E net")
    run(pill, 0.6)
    assert pill.shown > 0.9 and pill.icon == "net"
    fed(pill, "S")
    assert pill.icon == "" and pill.red == 0 and pill.shown > 0.9  # no blink: it was already open


def test_the_look_comes_from_the_settings_line_and_a_bad_one_changes_nothing():
    pill = Pill()
    fed(pill, "C #112233 #ffffff top 9.00 lines", "S")
    run(pill)
    assert pill.top and pill.scale == 1.5 and pill.lines and pill.bg[0] == pytest.approx(0x11 / 255)
    shapes = pill.shapes()
    assert len(shapes) == Pill.LINE_BARS
    assert all(0 <= x and x + w <= Pill.FIELD_W and 0 <= y and y + h <= Pill.FIELD_H for x, y, w, h, _ in shapes)
    fed(pill, "C red blue bottom x dots", "L nope", "bogus", "")
    assert pill.top and pill.lines


def test_dots_stay_inside_the_field_at_full_level():
    pill = Pill()
    fed(pill, "S", "L 1.00")
    run(pill, 0.5)
    shapes = pill.shapes()
    assert shapes and all(0 <= x and x + w <= Pill.FIELD_W and 0 <= y and y + h <= Pill.FIELD_H for x, y, w, h, _ in shapes)


def wait_for(check, seconds=20):
    end = time.monotonic() + seconds
    while not check() and time.monotonic() < end:
        time.sleep(0.05)
    return check()


def test_a_helper_that_keeps_dying_is_given_up_on():
    # On Linux the real helper exits at once, which is what a broken PyObjC looks like elsewhere.
    if indicator.sys.platform != "linux":
        pytest.skip("the helper works here")
    pill = Indicator(Config(desktop_indicator=True))
    def dead():
        pill.send("S")
        return pill.starts >= indicator.MAX_STARTS and not pill._alive()
    assert wait_for(dead)
    pill.send("S")
    time.sleep(0.1)
    assert pill.starts == indicator.MAX_STARTS


def test_a_stuck_helper_never_holds_up_the_audio_thread(monkeypatch):
    def stuck(self):  # alive, and reads nothing
        self.starts += 1
        self.proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE, text=True, bufsize=1)
    monkeypatch.setattr(Indicator, "_start", stuck)
    pill = Indicator(Config(desktop_indicator=True))
    started = time.monotonic()
    for _ in range(200_000):  # far more than the pipe holds
        pill.send("L 0.50 " + "x" * 50)
    assert time.monotonic() - started < 5
    pill.starts = indicator.MAX_STARTS
    pill.proc.kill()


def test_turned_off_it_starts_nothing_but_a_pill_already_open_can_close(monkeypatch):
    started = []
    monkeypatch.setattr(Indicator, "_start", lambda self: started.append(self))
    cfg = Config(desktop_indicator=False)
    pill = Indicator(cfg)
    pill.send("S")
    time.sleep(0.1)
    assert pill not in started and pill.lines.empty()
    pill.proc = type("P", (), {"poll": lambda self: None, "stdin": type("In", (), {"write": lambda self, line: None})()})()
    pill.send("S")
    pill.send("Q")
    assert wait_for(pill.lines.empty)  # S was held back, Q went through


class Sent:
    def __init__(self):
        self.lines = []

    def send(self, line):
        self.lines.append(line)


def test_feedback_tells_the_helper_what_it_tells_the_linux_pill(monkeypatch):
    fb = feedback.Feedback.__new__(feedback.Feedback)
    notified = []
    fb.cfg, fb.events, fb.indicator, fb.proc = Config(beeps=False), None, Sent(), None
    fb.pending_update = lambda: ""
    monkeypatch.setattr(fb, "notify", notified.append)
    fb.recording()
    fb.level(0.5)
    fb.working()
    fb.done()
    kinds = [line.split()[0] for line in fb.indicator.lines]
    assert kinds[-4:] == ["S", "L", "T", "Q"] and kinds[0] == "C"
    fb.error("no microphone", icon="mic")
    assert fb.indicator.lines[-1] == "E mic"
    assert notified == ["No microphone"]  # the pill takes no clicks, so it can't explain


def test_on_by_default_only_on_macos(monkeypatch):
    for platform, on in (("darwin", True), ("win32", False), ("linux", False)):
        monkeypatch.setattr(config.sys, "platform", platform)
        assert Config().desktop_indicator is on


@pytest.mark.skipif(sys.platform not in ("darwin", "win32"), reason="a real helper needs a real desktop")
def test_the_real_helper_runs_every_state_and_leaves_when_the_app_does():
    helper = subprocess.Popen([sys.executable, "-m", "hold_dictate.indicator"], stdin=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    for line in ("C #0a0a0b #6b80ff top 1.25 lines", "S", "L 0.80", "T", "C #0a0a0b #6b80ff bottom 1.00 dots", "S", "L 0.80", "E mic", "Q"):
        helper.stdin.write(line + "\n")
        helper.stdin.flush()
        time.sleep(0.2)
    _, errors = helper.communicate(timeout=20)  # closes stdin, as the app's exit does
    assert helper.returncode == 0 and "Traceback" not in errors, errors
