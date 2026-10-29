"""Puts text at the cursor: set the clipboard, press the paste shortcut, restore the old clipboard."""
import json
import os
import re
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


def _mac_front_app() -> str:
    asn = _run(["lsappinfo", "front"], text=True).stdout.strip()
    out = _run(["lsappinfo", "info", "-only", "name", asn], text=True).stdout if asn else ""
    m = re.search(r'"(?:LSDisplayName|name)"\s*=\s*"([^"]*)"', out)
    return m.group(1) if m else ""


def _windows_front_app() -> str:
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")  # own instances: argtypes stay ours
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf, size = ctypes.create_unicode_buffer(512), wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0]
    finally:
        kernel32.CloseHandle(handle)
    return ""


def can_detect_focus() -> bool:
    """Whether focused_app() can name the focused app here (not on GNOME or KDE under Wayland)."""
    if sys.platform in ("darwin", "win32"):
        return True
    if _wayland():
        return any(shutil.which(n) for n in ("niri", "hyprctl", "swaymsg"))
    return _has("xdotool")


def focused_app() -> str:
    """Best-effort name of the app with keyboard focus: the window class on Linux, the app name on macOS,
    the program name on Windows. Used for the terminal paste shortcut and the per-app rules."""
    try:
        if sys.platform == "darwin":
            return _mac_front_app()
        if sys.platform == "win32":
            return _windows_front_app()
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


def is_terminal(app: str) -> bool:
    app = app.lower()
    return any(t in app for t in TERMINALS)


def rule_for(rules: dict, app: str) -> str:
    """The per-app rule for the focused app: an exact name first, else the longest name it contains. '' if none."""
    if not app or not isinstance(rules, dict):
        return ""
    app = app.lower()
    names = {str(k).strip().lower(): v for k, v in rules.items() if str(k).strip()}
    if app in names:
        return names[app]
    hits = [n for n in names if n in app]
    return names[max(hits, key=len)] if hits else ""


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


def _pynput_enter():
    from pynput.keyboard import Controller, Key
    Controller().tap(Key.enter)


def _enter_wayland() -> bool:
    if _has("wtype") and _run(["wtype", "-k", "Return"]).returncode == 0:
        return True
    return _has("ydotool") and _run(["ydotool", "key", "28:1", "28:0"]).returncode == 0


def _enter_x11() -> bool:
    if _has("xdotool"):
        return _run(["xdotool", "key", "--clearmodifiers", "Return"]).returncode == 0
    try:
        _pynput_enter()
        return True
    except Exception:
        return False


def _linux_wayland(text: str, shift: bool, submit: bool) -> str:
    """Returns '' when pasted, or a note when only the clipboard could be set."""
    if not _has("wl-copy"):
        return "install wl-clipboard (wl-copy) so dictated text can be pasted"
    old = _run(["wl-paste", "-n", "-t", "text"]) if _has("wl-paste") else None
    subprocess.run(["wl-copy", "--", text])
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
    if submit:
        _enter_wayland()
    if old is not None and old.returncode == 0 and old.stdout:
        subprocess.run(["wl-copy"], input=old.stdout)
    else:
        subprocess.run(["wl-copy", "--clear"])
    return ""


def _x11_copier():
    if _has("xclip"):
        return ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]
    if _has("xsel"):
        return ["xsel", "-b", "-i"], ["xsel", "-b", "-o"]
    return None, None


def _linux_x11(text: str, shift: bool, submit: bool) -> str:
    tool, getter = _x11_copier()
    if tool is None:
        return "install xclip so dictated text can be pasted"
    old = _run(getter)
    subprocess.run(tool, input=text.encode("utf-8"))
    if _has("xdotool"):
        _run(["xdotool", "key", "--clearmodifiers", "ctrl+shift+v" if shift else "ctrl+v"])
    else:
        try:
            _pynput_paste(shift)
        except Exception:
            return "text copied to the clipboard; press Ctrl+V (install xdotool to paste automatically)"
    time.sleep(0.3)
    if submit:
        _enter_x11()
    subprocess.run(tool, input=old.stdout if old.returncode == 0 else b"")
    return ""


def _mac(text: str, shift: bool, submit: bool) -> str:
    old = _run(["pbpaste"])
    subprocess.run(["pbcopy"], input=text.encode("utf-8"))
    try:
        _pynput_paste(shift)
    except Exception as e:
        return f"text copied to the clipboard; press Cmd+V ({e})"
    time.sleep(0.3)
    if submit:
        _pynput_enter()
    subprocess.run(["pbcopy"], input=old.stdout)
    return ""


def _windows(text: str, shift: bool, submit: bool) -> str:
    import pyperclip
    try:
        old = pyperclip.paste()
    except Exception:
        old = ""
    pyperclip.copy(text)
    time.sleep(0.05)
    _pynput_paste(shift)
    time.sleep(0.3)
    if submit:
        _pynput_enter()
    pyperclip.copy(old)
    return ""


def paste_text(text: str, app: str | None = None, shift: bool | None = None, submit: bool = False) -> str:
    """Pastes text into the focused field, then presses Enter if submit. shift None picks Ctrl+Shift+V for
    terminals on Linux. Returns '' on success or a short note for the user."""
    if sys.platform == "darwin":
        return _mac(text, bool(shift), submit)
    if sys.platform == "win32":
        return _windows(text, bool(shift), submit)
    if shift is None:
        shift = is_terminal(focused_app() if app is None else app)
    return _linux_wayland(text, shift, submit) if _wayland() else _linux_x11(text, shift, submit)


def copy_text(text: str) -> str:
    """Puts text on the clipboard without pasting. Returns '' on success or a note."""
    try:
        if sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), timeout=2, check=True)
        elif sys.platform == "win32":
            import pyperclip
            pyperclip.copy(text)
        elif _wayland():
            if not _has("wl-copy"):
                return "install wl-clipboard (wl-copy) to copy text"
            subprocess.run(["wl-copy", "--", text], timeout=2, check=True)
        else:
            tool, _ = _x11_copier()
            if tool is None:
                return "install xclip to copy text"
            subprocess.run(tool, input=text.encode("utf-8"), timeout=2, check=True)
    except Exception as e:
        return f"could not copy the text: {e}"
    return ""


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
