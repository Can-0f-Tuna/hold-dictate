"""What the user sees and hears while dictating: the Linux overlay pill, beeps, notifications."""
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

QML = Path(__file__).with_name("overlay.qml")
RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
EVENTS_FILE = os.path.join(RUNTIME, "hold-dictate.events")


def _tone(freqs, seconds=0.08, volume=0.15):
    t = np.arange(int(SAMPLE_RATE * seconds)) / SAMPLE_RATE
    parts = [np.sin(2 * np.pi * f * t) * np.hanning(len(t)) for f in freqs]
    return (np.concatenate(parts) * volume).astype(np.float32)


TONES = {"start": _tone([660]), "stop": _tone([520]), "error": _tone([300, 220], 0.12)}


class Feedback:
    def __init__(self, cfg=None):
        self.cfg = cfg  # read on every use, so changes from the settings page apply at once
        self.proc = None
        self.events = None
        if sys.platform.startswith("linux") and os.environ.get("WAYLAND_DISPLAY") and shutil.which("quickshell"):
            self.events = open(EVENTS_FILE, "w", buffering=1)
            self._ensure_overlay()

    def _ensure_overlay(self):
        if self.proc and self.proc.poll() is None:
            return
        try:
            self.proc = subprocess.Popen(
                ["quickshell", "-p", str(QML)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            self.events = None

    def _send(self, line):
        if self.events:
            self.events.write(line + "\n")

    def _beep(self, name):
        # The overlay already signals state on Linux with quickshell; other systems get a beep.
        if (self.cfg and not self.cfg.sounds) or self.events:
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
            c = self.cfg
            self._send(f"C {c.overlay_bg} {c.overlay_bars} {c.overlay_position} {c.overlay_size:.2f} {c.overlay_style}")
        self._send("S")
        self._beep("start")

    def level(self, value):
        self._send(f"L {value:.2f}")

    def working(self):
        self._send("T")
        self._beep("stop")

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
