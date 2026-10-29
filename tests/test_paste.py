import os
import sys

import pytest

from hold_dictate import paste

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux paste helpers")


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Fake clipboard and key helpers on PATH that log their arguments, one line per call."""
    log = tmp_path / "log"
    for name in ("wl-copy", "wl-paste", "wtype", "xclip", "xdotool"):
        script = tmp_path / name
        script.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n[ "{name}" = xclip ] && [ "$3" = -o ] && printf old\nexit 0\n')
        script.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(paste.time, "sleep", lambda s: None)

    def calls():
        return log.read_text().splitlines() if log.exists() else []
    return calls


def test_wayland_paste_then_enter(tools, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert paste.paste_text("hi", app="firefox", submit=True) == ""
    keys = [c for c in tools() if c.startswith("wtype")]
    assert keys == ["wtype -M ctrl v -m ctrl", "wtype -k Return"]


def test_wayland_terminal_and_forced_shift(tools, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    paste.paste_text("hi", app="kitty")
    paste.paste_text("hi", app="kitty", shift=False)
    paste.paste_text("hi", app="emacs", shift=True)
    keys = [c for c in tools() if c.startswith("wtype")]
    assert keys == ["wtype -M ctrl -M shift v -m shift -m ctrl", "wtype -M ctrl v -m ctrl", "wtype -M ctrl -M shift v -m shift -m ctrl"]


def test_x11_paste_then_enter(tools, monkeypatch):
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert paste.paste_text("hi", app="xterm", submit=True) == ""
    keys = [c for c in tools() if c.startswith("xdotool")]
    assert keys == ["xdotool key --clearmodifiers ctrl+shift+v", "xdotool key --clearmodifiers Return"]


def test_copy_only(tools, monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert paste.copy_text("hi") == ""
    assert tools() == ["wl-copy -- hi"]


def test_copy_without_helpers(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert "wl-clipboard" in paste.copy_text("hi")


@pytest.mark.parametrize("app, expected", [
    ("Slack", "submit"), ("org.telegram.desktop", "off"), ("code-oss", "paste"), ("firefox", ""), ("", ""),
])
def test_rule_matching(app, expected):
    rules = {"slack": "submit", "telegram": "off", "code": "paste", "code-insiders": "shift"}
    assert paste.rule_for(rules, app) == expected


def test_longest_name_wins():
    assert paste.rule_for({"code": "paste", "code-insiders": "shift"}, "Code-Insiders") == "shift"


def test_can_detect_focus(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    assert not paste.can_detect_focus()  # GNOME / KDE: no compositor tool to ask
    (tmp_path / "niri").write_text("#!/bin/sh\n")
    os.chmod(tmp_path / "niri", 0o755)
    assert paste.can_detect_focus()
