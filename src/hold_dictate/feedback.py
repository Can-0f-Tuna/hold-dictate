"""What the user sees and hears while dictating: the Linux overlay pill, beeps, notifications."""
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE
from .config import pill_available

QML = Path(__file__).with_name("overlay.qml")
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
EVENTS_FILE = os.path.join(RUNTIME, "hold-dictate.events")


def _stop_stale_overlays(keep=None):
    """An update restarts the daemon in place, so the overlay the old version started is still running.
    Every overlay of ours but `keep` is stale (versions before 0.4.3 kept no record of theirs)."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) in (os.getpid(), keep):
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            if entry.stat().st_uid == os.getuid() and argv[0].endswith(b"quickshell") and argv[2].endswith(b"hold_dictate/overlay.qml"):
                os.kill(int(entry.name), 15)
        except (OSError, IndexError):
            pass


def _tone(freqs, seconds=0.08, volume=0.15):
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    parts = [np.sin(2 * np.pi * f * t) * np.hanning(len(t)) for f in freqs]
    return (np.concatenate(parts) * volume).astype(np.float32)


TONES = {"start": _tone([660]), "stop": _tone([520]), "error": _tone([300, 220], 0.12)}


class Feedback:
    def __init__(self, cfg=None):
        self.cfg = cfg  # read on every use, so changes from the settings page apply at once
        self.pending_update = lambda: ""  # version waiting to be installed; the pill shows a "!" tab
        self.proc = None
        self.events = None
        self.pending_premium = lambda: False  # the next dictation brings the "you got Premium" notice
        self.premium_sent = False
        if pill_available():
            self.events = open(EVENTS_FILE, "w", buffering=1)
            updated = os.environ.pop("HOLD_DICTATE_UPDATED", "")
            if not updated:
                _stop_stale_overlays()
            self._ensure_overlay()
            if updated and self.proc:
                # The old overlay is still showing its "installing" chip: tell it to tick and leave.
                # Older overlays do not know D, so whatever is left after the animation is stopped.
                self._send("D")
                keep = self.proc.pid
                threading.Timer(5, lambda: _stop_stale_overlays(keep)).start()

    def _ensure_overlay(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            env = dict(os.environ, HOLD_DICTATE_PYTHON=sys.executable)  # the "!" tab runs `hold-dictate update`
            self.proc = subprocess.Popen(
                ["quickshell", "-p", str(QML)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env
            )
        except OSError:
            self.events = None

    def _send(self, line):
        if self.events:
            self.events.write(line + "\n")

    def _send_look(self):
        c = self.cfg
        self._send(f"C {c.overlay_bg} {c.overlay_bars} {c.overlay_position} {c.overlay_size:.2f} {c.overlay_style}")

    def _beep(self, name):
        if not (self.cfg and self.cfg.beeps):
            return
        def play():
            try:
                import sounddevice as sd
                sd.play(TONES[name], SAMPLE_RATE)
            except Exception:
                pass
        threading.Thread(target=play, daemon=True).start()

    def recording(self):
        if self.events:
            self._ensure_overlay()
        if self.cfg:
            self._send_look()
        self._send(f"U {self.pending_update() or '-'}")
        if self.pending_premium():
            self._send("P on")
            self.premium_sent = True
        self._send("S")
        self._beep("start")

    def level(self, value):
        self._send(f"L {value:.2f}")

    def working(self):
        self._send("T")
        self._beep("stop")

    def update_failed(self):
        self._send("F")

    def premium_gone(self):
        """Premium was taken back before the notice was clicked."""
        if self.premium_sent:
            self._send("P -")
            self.premium_sent = False

    def done(self):
        self._send("Q")

    def error(self, message: str):
        self._send("Q")
        self._beep("error")
        print(f"hold-dictate: {message}", file=sys.stderr, flush=True)
        self.notify(message)

    def notify(self, message: str):
        try:
            if sys.platform == "darwin":
                safe = message.replace('"', "'")
                subprocess.run(["osascript", "-e", f'display notification "{safe}" with title "hold-dictate"'], timeout=3)
            elif sys.platform.startswith("linux") and shutil.which("notify-send"):
                subprocess.run(["notify-send", "hold-dictate", message], timeout=3)
        except Exception:
            pass
