import os
import struct
import sys
import threading

import pytest

from hold_dictate.hotkey import EV_FORMAT, EvdevBackend, HoldDetector, parse_hotkey

ALT, SPACE, SHIFT = 56, 57, 42


def test_parse():
    assert str(parse_hotkey("Alt+Space")) == "alt+space"
    assert str(parse_hotkey("option+space")) == "alt+space"
    assert str(parse_hotkey("ctrl+shift+d")) == "ctrl+shift+d"
    assert str(parse_hotkey("f9")) == "f9"


@pytest.mark.parametrize("bad", ["", "space", "alt+", "alt+shift", "alt+pageup", "foo+space"])
def test_parse_rejects(bad):
    with pytest.raises(ValueError):
        parse_hotkey(bad)


def test_hold_cycle():
    d = HoldDetector(parse_hotkey("alt+space"))
    assert d.feed("space", 57, True) is None  # no alt yet
    d.feed("space", 57, False)
    assert d.feed("alt", 56, True) is None
    assert d.feed("space", 57, True) == "start"
    assert d.feed("space", 57, True) is None  # no double start
    assert d.feed("space", 57, False) == "stop"
    assert d.feed("space", 57, True) == "start"  # alt still held
    assert d.feed("alt", 56, False) == "stop"  # releasing the modifier also stops
    assert d.feed("space", 57, False) is None


def test_left_and_right_alt():
    d = HoldDetector(parse_hotkey("alt+space"))
    d.feed("alt", 56, True)
    d.feed("alt", 100, True)
    d.feed("alt", 56, False)
    assert d.is_down("alt")
    assert d.feed("space", 57, True) == "start"


def _event(code, value):
    return struct.pack(EV_FORMAT, 0, 0, 1, code, value)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="evdev is Linux only")
def test_evdev_backend_reads_events():
    r, w = os.pipe()
    seq = [(ALT, 1), (SPACE, 1), (SPACE, 2), (SPACE, 0), (ALT, 0), (30, 1)]
    os.write(w, b"".join(_event(c, v) for c, v in seq))
    os.close(w)
    actions = []
    EvdevBackend([os.fdopen(r, "rb", buffering=0)]).run(HoldDetector(parse_hotkey("alt+space")), actions.append)
    assert actions == ["start", "stop"]


def test_two_hotkeys_the_more_specific_wins():
    d = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("alt+shift+space"))
    d.feed("alt", 56, True)
    assert d.feed("space", 57, True) == "start"
    assert d.feed("shift", 42, True) is None  # one at a time
    assert d.feed("space", 57, False) == "stop"
    assert d.feed("space", 57, True) == "second:start"  # alt and shift are down now
    assert d.feed("space", 57, True) is None
    assert d.feed("shift", 42, False) == "second:stop"  # letting go of any part of it stops
    assert d.feed("space", 57, False) is None
    assert d.feed("space", 57, True) == "start"
    assert d.feed("alt", 56, False) == "stop"


def test_second_hotkey_can_change_or_turn_off_while_held():
    d = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("ctrl+e"))
    d.feed("ctrl", 29, True)
    assert d.feed("e", 18, True) == "second:start"
    assert d.set_hotkey(parse_hotkey("alt+d")) is None  # dictation wasn't running
    assert d.set_second_hotkey(None) == "second:stop"
    d.feed("e", 18, False)
    assert d.feed("e", 18, True) is None  # off
    assert [str(h) for h in d.hotkeys()] == ["alt+d"]


def test_wait_released():
    d = HoldDetector(parse_hotkey("alt+space"))
    d.feed("alt", 56, True)
    assert d.wait_released(0.05) is False
    threading.Timer(0.05, lambda: d.feed("alt", 56, False)).start()
    assert d.wait_released(1.0) is True


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="evdev is Linux only")
def test_evdev_backend_reports_which_hotkey():
    ev = lambda code, value: struct.pack(EV_FORMAT, 0, 0, 1, code, value)
    r, w = os.pipe()
    seq = [(56, 1), (57, 1), (57, 0), (42, 1), (57, 1), (57, 0), (42, 0), (56, 0)]
    os.write(w, b"".join(ev(c, v) for c, v in seq))
    os.close(w)
    actions = []
    detector = HoldDetector(parse_hotkey("alt+space"), parse_hotkey("alt+shift+space"))
    EvdevBackend([os.fdopen(r, "rb", buffering=0)]).run(detector, actions.append)
    assert actions == ["start", "stop", "second:start", "second:stop"]
