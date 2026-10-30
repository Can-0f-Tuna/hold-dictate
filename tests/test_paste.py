import os
import shutil
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


@pytest.fixture
def board(tmp_path, monkeypatch):
    """A fake Wayland clipboard: wl-copy and wl-paste keep it in a file; wtype's Ctrl+C copies `selection`."""
    clip, primary, selection, log = (tmp_path / n for n in ("clip", "primary", "selection", "log"))
    scripts = {
        "wl-copy": f'echo "wl-copy $*" >> "{log}"\nif [ "$1" = --clear ]; then : > "{clip}"; elif [ "$1" = -- ]; then printf %s "$2" > "{clip}"; else cat > "{clip}"; fi',
        "wl-paste": f'if [ "$4" = --primary ]; then f="{primary}"; else f="{clip}"; fi\n[ -s "$f" ] || exit 1\ncat "$f"',
        "wtype": f'echo "wtype $*" >> "{log}"\ncase "$*" in *" c "*) cp "{selection}" "{clip}";; esac',
    }
    for name, body in scripts.items():
        (tmp_path / name).write_text("#!/bin/sh\n" + body + "\n")
        (tmp_path / name).chmod(0o755)
    for f in (clip, primary, selection):
        f.write_text("")
    for tool in ("cat", "cp"):
        (tmp_path / tool).symlink_to(shutil.which(tool))
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    return clip, primary, selection, log


def test_grab_selection_copies_then_restores_the_clipboard(board):
    clip, primary, selection, log = board
    clip.write_text("what you had copied")
    selection.write_text("the selected words")
    text, restore = paste.grab_selection("firefox")
    assert text == "the selected words"
    assert "wtype -M ctrl c -m ctrl" in log.read_text()
    restore()
    assert clip.read_text() == "what you had copied"
    assert paste.clipboard_text() == "what you had copied"


def test_grab_selection_nothing_selected_and_terminals(board, monkeypatch):
    clip, primary, selection, log = board
    monkeypatch.setattr(paste.time, "sleep", lambda s: None)
    clip.write_text("kept")
    text, restore = paste.grab_selection("kitty")  # a terminal copies with Ctrl+Shift+C
    assert text == "" and "wtype -M ctrl -M shift c -m shift -m ctrl" in log.read_text()
    restore()
    assert clip.read_text() == "kept"


def test_grab_selection_unknown_app_reads_the_primary_selection(board):
    clip, primary, selection, log = board
    primary.write_text("selected in a terminal")
    clip.write_text("kept")
    text, restore = paste.grab_selection("")  # GNOME or KDE on Wayland: no Ctrl+C that could stop a program
    restore()
    assert text == "selected in a terminal" and not log.exists() and clip.read_text() == "kept"


@pytest.mark.parametrize("desktop, listing, expected", [
    ("NIRI_SOCKET", '[{"id": 7, "title": "hold-dictate settings - Firefox"}]', ["niri", "msg", "action", "focus-window", "--id", "7"]),
    ("HYPRLAND_INSTANCE_SIGNATURE", '[{"address": "0xab", "title": "Hold-Dictate Settings"}]', ["hyprctl", "dispatch", "focuswindow", "address:0xab"]),
    ("SWAYSOCK", '{"nodes": [{"id": 4, "pid": 9, "name": "hold-dictate settings", "nodes": []}]}', ["swaymsg", "[con_id=4]", "focus"]),
])
def test_raise_window_finds_the_browser_by_its_title(monkeypatch, desktop, listing, expected):
    calls = []
    for name in ("NIRI_SOCKET", "HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(desktop, "x")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr(paste.sys, "platform", "linux")
    monkeypatch.setattr(paste, "_run", lambda cmd, **kw: (calls.append(cmd), type("R", (), {"stdout": listing, "returncode": 0})())[1])
    assert paste.raise_window("hold-dictate settings")
    assert calls[-1] == expected


def test_raise_window_gives_up_when_the_window_never_comes(monkeypatch):
    monkeypatch.setenv("NIRI_SOCKET", "x")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-1")
    monkeypatch.setattr(paste.sys, "platform", "linux")
    monkeypatch.setattr(paste, "_run", lambda cmd, **kw: type("R", (), {"stdout": "[]", "returncode": 0})())
    assert not paste.raise_window("hold-dictate settings", wait=0)
