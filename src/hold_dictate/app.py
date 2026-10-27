"""The dictation loop: hotkey down -> record, hotkey up -> transcribe -> paste."""
import sys
import threading
import time

from . import audio
from .config import Config
from .feedback import Feedback
from .hotkey import HoldDetector, choose_backend, parse_hotkey
from .paste import paste_text
from .providers import LocalTranscriber, TranscribeError, transcribe_api


def transcribe(cfg: Config, local: LocalTranscriber, speech) -> tuple[str, str]:
    """Returns (text, source). Cloud first; the local model takes over if the cloud fails and it is installed."""
    if cfg.provider == "local":
        return local.transcribe(speech), "local"
    try:
        return transcribe_api(cfg, speech), cfg.provider
    except TranscribeError as e:
        if cfg.local_fallback and LocalTranscriber.available():
            print(f"hold-dictate: {e}; using the local model", file=sys.stderr, flush=True)
            return local.transcribe(speech), "local"
        raise


class Dictation:
    def __init__(self, cfg: Config, feedback: Feedback, recorder_factory=None, paster=None):
        self.cfg = cfg
        self.fb = feedback
        self.local = LocalTranscriber(cfg)
        self.recorder_factory = recorder_factory or audio.Recorder
        self.paster = paster or paste_text
        self.recorder = None
        self.pressed_at = 0.0
        self.last_used = time.monotonic()
        self.paste_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    # Called by the hotkey backend.
    def on_action(self, action: str):
        if action == "start" and self.recorder is None:
            try:
                self.recorder = self.recorder_factory(self.fb.level)
            except audio.MicError as e:
                self.fb.error(str(e))
                return
            self.fb.recording()
            self.pressed_at = self.last_used = time.monotonic()
        elif action == "stop" and self.recorder is not None:
            recording, self.recorder = self.recorder.stop(), None
            if time.monotonic() - self.pressed_at < self.cfg.min_hold:
                self.fb.done()
                return
            self.fb.working()
            worker = threading.Thread(target=self.process, args=(recording,), daemon=True)
            worker.start()
            self.threads.append(worker)

    def transcribe(self, speech) -> tuple[str, str]:
        return transcribe(self.cfg, self.local, speech)

    def process(self, recording):
        started = time.monotonic()
        try:
            speech = audio.speech_only(recording)
            text, source = ("", "-") if speech is None else self.transcribe(speech)
        except Exception as e:
            self.fb.error(str(e))
            return
        self.last_used = time.monotonic()
        print(f"transcribed ({source}) in {self.last_used - started:.1f}s: {text!r}", flush=True)
        self.fb.done()
        if text:
            with self.paste_lock:
                note = self.paster(text)
            if note:
                self.fb.notify(note)

    def tick(self):
        """Frees the local model after a long idle stretch."""
        if self.local.model is not None and self.recorder is None:
            if time.monotonic() - self.last_used >= self.cfg.idle_unload_seconds:
                self.local.unload()


def run(cfg: Config) -> int:
    try:
        hotkey = parse_hotkey(cfg.hotkey)
    except ValueError as e:
        print(f"hold-dictate: bad hotkey in config: {e}", file=sys.stderr)
        return 2
    backend, problem = choose_backend()
    if backend is None:
        print(f"hold-dictate: {problem}", file=sys.stderr)
        return 1
    if cfg.provider != "local" and not cfg.effective_key():
        print("hold-dictate: no API key yet. Run `hold-dictate setup`.", file=sys.stderr)
        return 1
    dictation = Dictation(cfg, Feedback(cfg.sounds))
    print(f"hold-dictate: hold {hotkey} to dictate ({cfg.provider}, keyboard via {backend.name})", flush=True)
    try:
        backend.run(HoldDetector(hotkey), dictation.on_action, dictation.tick)
    except KeyboardInterrupt:
        pass
    return 0
