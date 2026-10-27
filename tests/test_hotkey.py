import os
import struct
import sys
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
