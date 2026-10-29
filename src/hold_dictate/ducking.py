"""Turns other apps' sound down while you dictate, so music from the speakers does not reach the microphone.

Linux lowers each playing stream (PipeWire or PulseAudio, through pactl), Windows each app's session
(pycaw), macOS the output volume. Everything is put back the way it was when the hotkey is released."""
import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading

LOWER = 0.2  # "lower" keeps 20% of the volume, about -14 dB


# Each backend: lower(factor) -> saved state, restore(saved state).

def _pulse_lower(factor):
    out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True, text=True, timeout=3, check=True)
    saved = []
    for stream in json.loads(out.stdout or "[]"):
        if stream.get("properties", {}).get("application.process.id") == str(os.getpid()):
            continue  # our own beeps
        values = [ch["value"] for ch in stream.get("volume", {}).values()]
        if not values:
            continue
        index = str(stream["index"])
        subprocess.run(["pactl", "set-sink-input-volume", index, *[str(int(v * factor)) for v in values]], timeout=3)
        saved.append((index, values))
    return saved


def _pulse_restore(saved):
    for index, values in saved:  # a stream that has ended since then just fails quietly
        subprocess.run(["pactl", "set-sink-input-volume", index, *map(str, values)], capture_output=True, timeout=3)


def _windows_lower(factor):
    import comtypes
    from pycaw.pycaw import AudioUtilities

    comtypes.CoInitialize()  # once per thread; the worker thread is always the same one
    saved = []
    for session in AudioUtilities.GetAllSessions():
        if session.Process is None or session.ProcessId == os.getpid():
            continue  # system sounds, and our own beeps
        volume = session.SimpleAudioVolume
        before = volume.GetMasterVolume()
        volume.SetMasterVolume(before * factor, None)
        saved.append((volume, before))
    return saved


def _windows_restore(saved):
    for volume, before in saved:
        try:
            volume.SetMasterVolume(before, None)
        except Exception:
            pass  # the app has closed


def _osascript(script):
    return subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=3, check=True).stdout.strip()


def _mac_lower(factor):
    muted = _osascript("output muted of (get volume settings)")
    before = _osascript("output volume of (get volume settings)")
    if muted == "true" or not before.isdigit():  # muted already, or a device without volume control
        return None
    _osascript(f"set volume output volume {round(int(before) * factor)}")
    return int(before)


def _mac_restore(before):
    _osascript(f"set volume output volume {before}")


def backend():
    if sys.platform.startswith("linux"):
        return (_pulse_lower, _pulse_restore) if shutil.which("pactl") else None
    if sys.platform == "darwin":
        return _mac_lower, _mac_restore
    if sys.platform == "win32":
        return _windows_lower, _windows_restore
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
        factor = 0.0 if mode == "mute" else LOWER
        self._submit(lambda: self._duck(factor))

    def _duck(self, factor):
        if self.saved is None:
            self.saved = self.backend[0](factor) or None

    def restore(self):
        if self.thread is not None:
            self._submit(self._restore)

    def _restore(self):
        saved, self.saved = self.saved, None
        if saved is not None:
            self.backend[1](saved)

    def close(self, timeout=3.0):
        """Puts the volume back before the program exits."""
        if self.thread is None:
            return
        finished = threading.Event()
        self.restore()
        self._submit(finished.set)
        finished.wait(timeout)
