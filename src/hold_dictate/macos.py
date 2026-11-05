"""macOS only: the permissions, through the system's own calls. Nothing here runs on another system."""
import sys
import threading


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
