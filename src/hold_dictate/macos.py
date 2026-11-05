"""macOS only: the permissions, through the system's own calls. Nothing here runs on another system."""
import ctypes
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque

TERMINALS = {"Apple_Terminal": "Terminal", "iTerm.app": "iTerm", "vscode": "Visual Studio Code", "WarpTerminal": "Warp"}


def spawn_options(cmd: list) -> tuple[list, dict]:
    """Starts helpers with posix_spawn instead of fork: Python spawns when the program has a full path and
    close_fds is off. A fork right after the local model ran made its OpenMP runtime warn \"Forking a process
    while a parallel region is active\". Python's own descriptors aren't inherited either way (PEP 446)."""
    from shutil import which
    return [which(cmd[0]) or cmd[0], *cmd[1:]], {"close_fds": False}


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


_playing: deque = deque(maxlen=8)  # an NSSound must live until it has played


def play_sound(samples):
    """Plays float samples at 16 kHz through AppKit, or afplay if that fails: never through PortAudio."""
    from .audio import wav_bytes
    data = wav_bytes(samples)
    try:
        from AppKit import NSSound
        from Foundation import NSData
        sound = NSSound.alloc().initWithData_(NSData.dataWithBytes_length_(data, len(data)))
        if sound is not None and sound.play():
            _playing.append(sound)
            return
    except Exception:
        pass
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(data)
    try:
        subprocess.run(["/usr/bin/afplay", f.name], capture_output=True, timeout=5)
    finally:
        os.unlink(f.name)


TRANSIENT = "org.nspasteboard.TransientType"  # nspasteboard.org: don't keep this in a clipboard history
READ_WAIT = 2.0  # an app that hasn't read the paste by then never will: no text field had the focus
AFTER_READ = 0.15
PLAIN_WAIT = 0.5


def main_loop_running() -> bool:
    """Whether the main thread is in its run loop, which is where the pasteboard asks for promised data."""
    try:
        import CoreFoundation as cf
        return cf.CFRunLoopCopyCurrentMode(cf.CFRunLoopGetMain()) is not None
    except Exception:
        return False


_provider_class = None


def _provider(text: str, read: threading.Event):
    """An NSPasteboardItemDataProvider that hands over `text` and sets `read` when an app asks for it."""
    global _provider_class
    if _provider_class is None:
        import objc
        from Foundation import NSObject

        class HoldDictatePasteProvider(NSObject, protocols=[objc.protocolNamed("NSPasteboardItemDataProvider")]):
            def pasteboard_item_provideDataForType_(self, board, item, kind):
                item.setString_forType_(self.text, kind)
                self.read.set()

            def pasteboardFinishedWithDataProvider_(self, board):
                pass

        _provider_class = HoldDictatePasteProvider
    provider = _provider_class.alloc().init()
    provider.text, provider.read = text, read
    return provider


class Clipboard:
    """The pasteboard for one paste: it keeps every item and type that was there (images, rich text, files, not
    only the plain text pbcopy knows), puts the text up, and puts the old contents back."""

    def __init__(self):
        from AppKit import NSPasteboard
        self.board = NSPasteboard.generalPasteboard()
        self.saved = []
        for item in self.board.pasteboardItems() or []:
            kinds = {}
            for kind in item.types() or []:
                data = item.dataForType_(kind)
                if data is not None:
                    kinds[kind] = data
            if kinds:
                self.saved.append(kinds)
        self.ours = None  # the board's change count once the text is up

    def put(self, text: str):
        """With AppKit's main loop running (the menu-bar icon runs it), the text is only promised: the board asks
        us for it when an app reads it, which tells wait_read() the paste is done. Without that loop no one would
        answer, so the text goes up as it is."""
        from AppKit import NSPasteboardItem, NSPasteboardTypeString
        self.read = threading.Event()
        self.board.clearContents()
        try:
            self.board.prepareForNewContentsWithOptions_(1)  # NSPasteboardContentsCurrentHostOnly: not to other devices
        except Exception:
            pass
        provider = _provider(text, self.read) if main_loop_running() else None
        item = NSPasteboardItem.alloc().init()
        if provider is None:
            item.setString_forType_(text, NSPasteboardTypeString)
        else:
            item.setDataProvider_forTypes_(provider, [NSPasteboardTypeString])
            self.provider = provider  # kept alive until the board is done with it
        item.setData_forType_(b"", TRANSIENT)  # clipboard managers leave it out of their history
        self.board.writeObjects_([item])
        self.ours = self.board.changeCount()
        self.promised = provider is not None

    def wait_read(self, longest: float = READ_WAIT) -> bool:
        """Waits until an app has read the text, up to `longest`. True if it was read. A plain put can't tell
        when that happens, so it waits PLAIN_WAIT, the old fixed time."""
        if not self.promised:
            time.sleep(PLAIN_WAIT)
            return False
        if not self.read.wait(longest):
            return False
        time.sleep(AFTER_READ)  # an app that reads twice gets the board's copy the second time
        return True

    def restore(self):
        """Unless something else was copied in the meantime: that is newer than what we kept."""
        from AppKit import NSPasteboardItem
        if self.ours is not None and self.board.changeCount() != self.ours:
            return
        self.board.clearContents()
        items = []
        for kinds in self.saved:
            item = NSPasteboardItem.alloc().init()
            for kind, data in kinds.items():
                item.setData_forType_(data, kind)
            items.append(item)
        if items:
            self.board.writeObjects_(items)
