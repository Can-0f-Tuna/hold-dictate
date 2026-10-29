"""The dictation loop: hotkey down -> record, hotkey up -> transcribe -> paste."""
import signal
import sys
import threading
import time

from . import audio
from .ducking import Ducker
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
    def __init__(self, cfg: Config, feedback: Feedback, recorder_factory=None, paster=None, on_no_key=None, ducker=None):
        self.cfg = cfg
        self.ducker = ducker or Ducker(cfg)
        self.on_no_key = on_no_key
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
            if self.cfg.provider != "local" and not self.cfg.effective_key():
                self.fb.error("no API key yet. Add one in the settings.")
                if self.on_no_key:
                    self.on_no_key()
                return
            try:
                self.recorder = self.recorder_factory(self.fb.level)
            except audio.MicError as e:
                self.fb.error(str(e))
                return
            self.fb.recording()
            self.ducker.duck()
            self.pressed_at = self.last_used = time.monotonic()
        elif action == "stop" and self.recorder is not None:
            recording, self.recorder = self.recorder.stop(), None
            self.ducker.restore()
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

    def idle(self) -> bool:
        self.threads = [t for t in self.threads if t.is_alive()]
        return self.recorder is None and not self.threads

    def tick(self):
        """Frees the local model after a long idle stretch."""
        if self.local.model is not None and self.recorder is None:
            if time.monotonic() - self.last_used >= self.cfg.idle_unload_seconds:
                self.local.unload()


def run(cfg: Config) -> int:
    from . import tray
    from .settings_ui import SettingsServer, running_url
    from .updater import Updater

    if running_url():
        print("hold-dictate is already running. Open its settings with `hold-dictate settings`.", flush=True)
        return 0
    backend, problem = choose_backend()
    if backend is None:  # keep running so the settings page still works; the hotkey waits for a restart
        print(f"hold-dictate: {problem}", file=sys.stderr, flush=True)
    try:
        hotkey = parse_hotkey(cfg.hotkey)
    except ValueError as e:  # fall back to the default so the settings page can fix it
        problem = problem or f"the hotkey {cfg.hotkey!r} in the config is invalid ({e}); using alt+space"
        print(f"hold-dictate: {problem}", file=sys.stderr, flush=True)
        hotkey = parse_hotkey(Config().hotkey)

    feedback = Feedback(cfg)
    detector = HoldDetector(hotkey)
    dictation = Dictation(cfg, feedback)
    updater = Updater(cfg, notify=feedback.notify, is_idle=dictation.idle)

    def on_saved(new_cfg: Config):
        new_hotkey = parse_hotkey(new_cfg.hotkey)
        if str(new_hotkey) != str(detector.hotkey) and detector.set_hotkey(new_hotkey):
            dictation.on_action("stop")

    server = SettingsServer(cfg, on_saved=on_saved, updater=updater).start()
    server.problem = problem
    dictation.on_no_key = server.open

    def tick():
        dictation.tick()
        updater.tick()

    def listen():
        if backend is None:
            while not done.wait(5):
                updater.tick()
            return
        try:
            backend.run(detector, dictation.on_action, tick)
        finally:
            done.set()

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())  # a clean exit, so other apps get their volume back
    threading.Thread(target=listen, daemon=True).start()
    if backend:
        print(f"hold-dictate: hold {hotkey} to dictate ({cfg.provider}, keyboard via {backend.name})", flush=True)
    print(f"hold-dictate: settings at {server.url}", flush=True)
    try:
        if tray.available():
            try:
                icon = tray.Tray(on_settings=server.open, on_check_updates=lambda: (updater.check(), server.open()), on_quit=done.set)
                threading.Thread(target=lambda: (done.wait(), icon.stop()), daemon=True).start()
                icon.run()
            except Exception as e:  # no tray is no reason to stop dictating
                print(f"hold-dictate: tray icon unavailable: {e}", file=sys.stderr, flush=True)
        while not done.wait(1):
            pass
    except KeyboardInterrupt:
        pass
    server.stop()
    return 0
