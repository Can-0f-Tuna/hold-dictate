"""macOS only: the permissions, through the system's own calls. Nothing here runs on another system."""
import ctypes
import os
import sys
import threading

TERMINALS = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code", "WarpTerminal": "Warp"}


def microphone_status():
    """True, False, or None when macOS hasn't asked yet."""
    from .health import _mac_microphone
    return _mac_microphone()


def ask_microphone():
    """Gets macOS's Microphone question out of the way at start, in a thread of its own: a first recording that
    meets it waits for the answer, and the first word is lost. Opening the microphone is what asks."""
    def run():
        try:
            if microphone_status() is not None:
                return
            from . import audio
            recorder = audio.Recorder()
            recorder.live.wait(0.5)
            recorder.stop()
        except Exception as e:
            print(f"hold-dictate: couldn't ask for the microphone: {e}", file=sys.stderr, flush=True)
    threading.Thread(target=run, daemon=True, name="microphone question").start()


def accessibility_trusted() -> bool:
    """Asked anew each time: a switch turned on in System Settings counts at once, with no restart."""
    services = ctypes.CDLL("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
    services.AXIsProcessTrusted.restype = ctypes.c_bool
    return bool(services.AXIsProcessTrusted())


def ask_accessibility():
    """macOS's own question, which also puts the program in the Accessibility list, switched off."""
    try:
        import HIServices
        HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
    except Exception as e:
        print(f"hold-dictate: couldn't ask for Accessibility: {e}", file=sys.stderr, flush=True)


def grantee() -> tuple[str, str]:
    """(name, path) of the program macOS gives the permissions to: the terminal hold-dictate was started from,
    or its Python when it starts at login. Every Python is a program of its own to macOS, so a new one asks again."""
    term = os.environ.get("TERM_PROGRAM")
    if term:
        return TERMINALS.get(term, term), ""
    path = os.path.realpath(sys.executable)
    return os.path.basename(path), path


def accessibility_problem() -> str:
    """For the settings page's banner: a sentence, then the path on a line of its own."""
    name, path = grantee()
    where = "System Settings > Privacy & Security > Accessibility"
    if not path:
        return f"cannot read the keyboard. Turn on {name} in {where}. The shortcut works as soon as it's on."
    return (f"cannot read the keyboard. Turn on {name} in {where}. If it isn't in the list, add it with the + button "
            f"(Cmd+Shift+G takes a path). The shortcut works as soon as it's on. The program is:\n    {path}")
