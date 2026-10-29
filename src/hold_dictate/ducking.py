"""Turns other apps' sound down while you dictate, so music from the speakers does not reach the microphone.

The sound fades down when the hotkey is pressed and back up when it is released.
Linux lowers each playing stream (PipeWire or PulseAudio, through pactl), Windows each app's session
(pycaw), macOS the output volume."""
import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time

LOWER_DB = -14.0  # "lower": music clearly in the background, but still there
FADE_OUT = 0.3  # seconds; short, so the music is down by the time you start talking
FADE_IN = 0.8
STEP = 0.04


def gain_at(progress: float, start_db: float, end_db: float) -> float:
    """Amplitude between two levels, moved evenly in decibels so the fade sounds even to the ear."""
    db = start_db + (end_db - start_db) * min(1.0, max(0.0, progress))
    return 0.0 if db <= -60 else 10 ** (db / 20)


# Each backend: snapshot() -> saved state, apply(saved, gain) sets every app to gain x its own volume
# (gain is an amplitude: 1.0 = as it was, 0.2 = -14 dB, 0.0 = silent).

def _pulse_snapshot():
    out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True, text=True, timeout=3, check=True)
    saved = []
    for stream in json.loads(out.stdout or "[]"):
        if stream.get("properties", {}).get("application.process.id") == str(os.getpid()):
            continue  # our own beeps
        values = [ch["value"] for ch in stream.get("volume", {}).values()]
        if values:
            saved.append((str(stream["index"]), values))
    return saved


def _pulse_apply(saved, gain):
    # PulseAudio and PipeWire volumes are cubic: 20% on their slider is -42 dB, not -14 dB.
    scale = gain ** (1 / 3)
    for index, values in saved:  # a stream that has ended since then just fails quietly
        subprocess.run(["pactl", "set-sink-input-volume", index, *[str(round(v * scale)) for v in values]], capture_output=True, timeout=3)


def _windows_snapshot():
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()  # once per thread; the worker thread is always the same one
    saved = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process is None or session.ProcessId == os.getpid():
            continue  # system sounds, and our own beeps
        volume = session.SimpleAudioVolume
        saved.append((volume, volume.GetMasterVolume()))
    return saved


def _windows_apply(saved, gain):
    for volume, before in saved:
        try:
            volume.SetMasterVolume(before * gain, None)
        except Exception:
            pass  # the app has closed


def _osascript(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3, check=True).stdout.strip()


def _mac_snapshot():
    muted = _osascript("output muted of (get volume settings)")
    before = _osascript("output volume of (get volume settings)")
    if muted == "true" or not before.isdigit():  # muted already, or a device without volume control
        return None
    return int(before)


def _mac_apply(before, gain):
    _osascript(f"set volume output volume {round(before * gain)}")


def backend():
    if sys.platform.startswith("linux"):
        return (_pulse_snapshot, _pulse_apply) if shutil.which("pactl") else None
    if sys.platform == "darwin":
        return _mac_snapshot, _mac_apply
    if sys.platform == "win32":
        return _windows_snapshot, _windows_apply
    return None


def available() -> bool:
    return backend() is not None


class Ducker:
    """duck() when recording starts, restore() when it stops. Both return at once: the work runs in order
    on one background thread, so a slow audio system never delays the recording."""

    def __init__(self, cfg, backend_pair=None):
        self.cfg = cfg  # read on every use, so changes from the settings page apply at once
        self.backend = backend_pair if backend_pair is not None else backend()
        self.saved = None
        self.ducked_db = 0.0
        self.jobs: queue.Queue = queue.Queue()
        self.thread = None
        self.warned = False
        atexit.register(self.close)

    def _submit(self, job):
        if self.thread is None:
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()
        self.jobs.put(job)

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                job()
            except Exception as e:  # never worth stopping a dictation for
                if not self.warned:
                    self.warned = True
                    print(f"hold-dictate: could not change the volume of other apps: {e}", file=sys.stderr, flush=True)

    def duck(self):
        mode = getattr(self.cfg, "background_audio", "off")
        if self.backend is None or mode not in ("lower", "mute"):
            return
        self._submit(lambda: self._duck(-60.0 if mode == "mute" else LOWER_DB))

    def _duck(self, target_db):
        if self.saved is not None:
            return
        self.saved = self.backend[0]() or None
        if self.saved is not None:
            self.ducked_db = target_db
            self._fade(0.0, target_db, FADE_OUT)

    def _fade(self, start_db, end_db, seconds):
        """Steps by the clock, so a slow backend takes fewer steps instead of a longer fade."""
        began = time.monotonic()
        while True:
            progress = (time.monotonic() - began) / seconds if seconds > 0 else 1.0
            self.backend[1](self.saved, gain_at(progress, start_db, end_db))
            if progress >= 1.0:
                return
            time.sleep(STEP)

    def restore(self, fade=True):
        if self.thread is not None:
            self._submit(lambda: self._restore(fade))

    def _restore(self, fade):
        if self.saved is None:
            return
        try:
            self._fade(self.ducked_db, 0.0, FADE_IN if fade else 0.0)
        finally:
            self.saved = None

    def close(self, timeout=3.0):
        """Puts the volume back before the program exits."""
        if self.thread is None:
            return
        finished = threading.Event()
        self.restore(fade=False)
        self._submit(finished.set)
        finished.wait(timeout)
