"""Puts text at the cursor: set the clipboard, press the paste shortcut, restore the old clipboard."""
import json
import os
import shutil
import subprocess
import sys
import time

TERMINALS = ("alacritty", "konsole", "kitty", "foot", "ghostty", "wezterm", "ptyxis", "terminal", "xterm", "tilix", "urxvt", "st-256color")


def _run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, timeout=kw.pop("timeout", 2), **kw)


def _has(*names) -> bool:
    return all(shutil.which(n) for n in names)


def _wayland() -> bool:
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def focused_app() -> str:
    """Best-effort class of the focused window on Linux, used to pick the terminal paste shortcut."""
    try:
        if _wayland():
            if shutil.which("niri"):
                return (json.loads(_run(["niri", "msg", "-j", "focused-window"], text=True).stdout) or {}).get("app_id") or ""
            if shutil.which("hyprctl"):
                return json.loads(_run(["hyprctl", "activewindow", "-j"], text=True).stdout).get("class") or ""
            if shutil.which("swaymsg"):
                tree = json.loads(_run(["swaymsg", "-t", "get_tree"], text=True).stdout)
                stack = [tree]
                while stack:
                    node = stack.pop()
                    if node.get("focused"):
                        return node.get("app_id") or (node.get("window_properties") or {}).get("class") or ""
                    stack += node.get("nodes", []) + node.get("floating_nodes", [])
        elif shutil.which("xdotool"):
            return _run(["xdotool", "getwindowfocus", "getwindowclassname"], text=True).stdout.strip()
    except Exception:
        pass
    return ""


def _is_terminal() -> bool:
    app = focused_app().lower()
    return any(t in app for t in TERMINALS)


def _pynput_paste(shift: bool = False):
    from pynput.keyboard import Controller, Key
    kb = Controller()
    mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    with kb.pressed(mod):
        if shift:
            with kb.pressed(Key.shift):
                kb.tap("v")
        else:
            kb.tap("v")


def _linux_wayland(text: str) -> str:
    """Returns '' when pasted, or a note when only the clipboard could be set."""
    if not _has("wl-copy"):
        return "install wl-clipboard (wl-copy) so dictated text can be pasted"
    old = _run(["wl-paste", "-n", "-t", "text"]) if _has("wl-paste") else None
    subprocess.run(["wl-copy", "--", text])
    shift = _is_terminal()
    pasted = False
    if _has("wtype"):
        keys = ["-M", "ctrl"] + (["-M", "shift"] if shift else []) + ["v"] + (["-m", "shift"] if shift else []) + ["-m", "ctrl"]
        pasted = _run(["wtype", *keys]).returncode == 0
    if not pasted and _has("ydotool"):
        seq = ["29:1"] + (["42:1"] if shift else []) + ["47:1", "47:0"] + (["42:0"] if shift else []) + ["29:0"]
        pasted = _run(["ydotool", "key", *seq]).returncode == 0
    if not pasted:
        return "text copied to the clipboard; press Ctrl+V (install wtype or ydotool to paste automatically)"
    time.sleep(0.3)  # let the app read the clipboard before it is restored
    if old is not None and old.returncode == 0 and old.stdout:
        subprocess.run(["wl-copy"], input=old.stdout)
    else:
        subprocess.run(["wl-copy", "--clear"])
    return ""


def _linux_x11(text: str) -> str:
    tool = ["xclip", "-selection", "clipboard"] if _has("xclip") else ["xsel", "-b", "-i"] if _has("xsel") else None
    if tool is None:
        return "install xclip so dictated text can be pasted"
    getter = ["xclip", "-selection", "clipboard", "-o"] if tool[0] == "xclip" else ["xsel", "-b", "-o"]
    old = _run(getter)
    subprocess.run(tool, input=text.encode("utf-8"))
    shift = _is_terminal()
    if _has("xdotool"):
        _run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v" if shift else "ctrl+v"])
    else:
        try:
            _pynput_paste(shift)
        except Exception:
            return "text copied to the clipboard; press Ctrl+V (install xdotool to paste automatically)"
    time.sleep(0.3)
    subprocess.run(tool, input=old.stdout if old.returncode == 0 else b"")
    return ""


def _mac(text: str) -> str:
    old = _run(["pbpaste"])
    subprocess.run(["pbcopy"], input=text.encode("utf-8"))
    try:
        _pynput_paste()
    except Exception as e:
        return f"text copied to the clipboard; press Cmd+V ({e})"
    time.sleep(0.3)
    subprocess.run(["pbcopy"], input=old.stdout)
    return ""


def _windows(text: str) -> str:
    import pyperclip
    try:
        old = pyperclip.paste()
    except Exception:
        old = ""
    pyperclip.copy(text)
    time.sleep(0.05)
    _pynput_paste()
    time.sleep(0.3)
    pyperclip.copy(old)
    return ""


def paste_text(text: str) -> str:
    """Pastes text into the focused field. Returns '' on success or a short note for the user."""
    if sys.platform == "darwin":
        return _mac(text)
    if sys.platform == "win32":
        return _windows(text)
    return _linux_wayland(text) if _wayland() else _linux_x11(text)


def missing_tools() -> list[str]:
    """Names of helper programs worth installing on Linux (empty elsewhere)."""
    if not sys.platform.startswith("linux"):
        return []
    if _wayland():
        need = [] if _has("wl-copy") else ["wl-clipboard"]
        if not _has("wtype") and not _has("ydotool"):
            need.append("wtype")
        return need
    need = [] if (_has("xclip") or _has("xsel")) else ["xclip"]
    return need + ([] if _has("xdotool") else ["xdotool"])
