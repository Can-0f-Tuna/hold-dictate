"""The dictation loop: hotkey down -> record, hotkey up (or the next tap) -> transcribe -> clean -> paste."""
import signal
import sys
import threading
import time

from . import audio, extensions, history, paste, textproc
from .ducking import Ducker
from .config import Config
from .feedback import Feedback
from .hotkey import HoldDetector, choose_backend, parse_hotkey
from .live import LiveTyper
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


TAP_SECONDS = 0.4  # auto mode: a press shorter than this keeps recording until the next press
NO_SPEECH_SECONDS = 8.0  # with silence_stop on, a recording where nothing is said stops after this
MIC_WAIT = 1.5  # the start cue comes when the mic is live, or after this long if it never says so
TAIL_SECONDS = 0.2  # recording goes on this long after the release: people let go before the last word ends
TAIL_GRACE = 0.04  # a stalled stream gets this much more; with audio.CLOSE_WAIT, a clip ends within tail + 100 ms
PRUNE_SECONDS = 3600  # how often old history is cleared out, for days with no dictation


class Tail:
    """The end of a recording: the key is up and the mic stays on until `run_out` is done."""

    def __init__(self, recorder, seconds: float):
        self.recorder = recorder
        self.released = recorder.samples  # where the key came up
        self.wanted = self.released + int(seconds * audio.SAMPLE_RATE)
        self.ends = time.monotonic() + seconds
        self.limit = self.ends + TAIL_GRACE if seconds else self.ends
        self.cut = threading.Event()  # the next press: stop now
        self.stopped = threading.Event()

    def run_out(self):
        while True:
            now = time.monotonic()
            if now >= self.limit or (now >= self.ends and self.recorder.samples >= self.wanted):
                break
            if self.cut.wait(0.01):
                break
        try:
            return self.recorder.stop()
        finally:
            self.stopped.set()


class Dictation:
    def __init__(self, cfg: Config, feedback: Feedback, recorder_factory=None, paster=None, on_no_key=None, ducker=None,
                 focus=None, copier=None):
        self.cfg = cfg
        self.ducker = ducker or Ducker(cfg)
        self.on_no_key = on_no_key
        self.fb = feedback
        self.local = LocalTranscriber(cfg)
        self.recorder_factory = recorder_factory or audio.Recorder
        self.paster = paster or paste.paste_text
        self.focus = focus or paste.focused_app
        self.copier = copier or paste.copy_text
        self.second_work = None  # what takes a recording made with the second hotkey; None for a dictation
        self.recorder = None
        self.tail = None  # the last recording, while it runs out
        self.live = None  # types while you speak, when turned on
        self.live_app = ""
        self.latched = False  # auto and toggle: the key is up but the recording goes on
        self.cued = False  # the pill and the start sound have gone out for this recording
        self.voice = audio.VoiceMeter()
        self.pressed_at = 0.0
        self.last_used = time.monotonic()
        self.last_text = ""  # the last dictation, kept in memory only, for paste-last and copy-last
        self.last_app = ""  # the app it went to, offered on the settings page for a per-app rule
        self.lock = threading.RLock()
        self.paste_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.pruned = time.monotonic() - PRUNE_SECONDS  # the first tick does it; the clock can start at boot

    # Called by the hotkey backend: "start" when the hotkey goes down, "stop" when it comes up, and
    # "second:start" and "second:stop" for the second hotkey, if an extension added one.
    def on_action(self, action: str):
        which, _, action = action.rpartition(":")
        with self.lock:
            mode = self.cfg.hotkey_mode
            if action == "start":
                if self.recorder is None:
                    work = extensions.second_recording(self) if which == "second" else None
                    if work is False:
                        return
                    self.begin(work)
                elif mode != "hold":
                    self.finish()
            elif action == "stop" and self.recorder is not None:
                held = time.monotonic() - self.pressed_at
                if mode not in ("auto", "toggle") or (mode == "auto" and held >= TAP_SECONDS):
                    self.finish()
                else:
                    self.latched = True

    def _level(self, value: float):
        self.fb.level(value)
        self.voice.feed(value, time.monotonic())

    def begin(self, second_work=None):
        problem = extensions.before_recording(self)
        if problem:
            self.fb.error(problem)
            if self.on_no_key:
                self.on_no_key()
            return
        if self.cfg.provider != "local" and not self.cfg.effective_key():
            self.fb.error("no API key yet. Add one in the settings.")
            if self.on_no_key:
                self.on_no_key()
            return
        self._cut_tail()
        self.voice = audio.VoiceMeter()
        try:
            self.recorder = self.recorder_factory(self._level)
        except audio.MicError as e:
            self.fb.error(str(e), e.brief, e.icon)
            return
        self.latched, self.second_work, self.cued = False, second_work, False
        self.ducker.duck()
        self.pressed_at = self.last_used = time.monotonic()
        threading.Thread(target=self._cue_when_live, args=(self.recorder,), daemon=True).start()
        if self._types_live():
            app = self.live_app  # the next recording has its own
            self.live = LiveTyper(self.recorder, self._transcribe_live, lambda raw: textproc.clean(raw, self.cfg), lambda text: self._type_live(text, app),
                                  ready=lambda: sys.platform.startswith("linux") or self.latched,
                                  **({"gap": 1.0, "per_minute": 0} if self.cfg.provider == "local" else {}))
            self.live.start()
        if self.cfg.hotkey_mode != "hold" and self.cfg.silence_stop > 0:
            threading.Thread(target=self._watch_silence, args=(self.recorder,), daemon=True).start()

    def _cue_when_live(self, recorder):
        """The pill and the start sound mean "speak now", so they wait for the mic: words said into a Bluetooth
        headset before its link is up never arrive, and Whisper turns what is left into another language."""
        live = getattr(recorder, "live", None)
        if live is not None:
            live.wait(MIC_WAIT)
        with self.lock:
            if self.recorder is recorder:
                self._cue()

    def _cue(self):
        if not self.cued:
            self.cued = True
            self.fb.recording()

    def _watch_silence(self, recorder):
        """While a tapped recording runs, stops it once you have been quiet for silence_stop seconds."""
        while True:
            time.sleep(0.1)
            with self.lock:
                if self.recorder is not recorder:
                    return
                if not self.latched:
                    continue
                limit = self.cfg.silence_stop if self.voice.heard else max(self.cfg.silence_stop, NO_SPEECH_SECONDS)
                if time.monotonic() - (self.voice.last if self.voice.heard else self.pressed_at) >= limit:
                    self.finish(tail=False)  # it stopped because you went quiet: no word is left to catch
                    return

    def finish(self, tail: bool = True):
        """Ends the recording, if any. What you hear and see happens now; the mic stays on for the tail in the
        background, then the clip is transcribed there."""
        with self.lock:
            if self.recorder is None:
                return
            recorder, live = self.recorder, self.live
            self.recorder, self.live, self.latched = None, None, False
            if live:
                live.stop()  # the last pass has the rest
            self.ducker.restore()
            if time.monotonic() - self.pressed_at < self.cfg.min_hold:
                recorder.stop()
                self.fb.done()
                return
            self._cue()  # let go before the mic was live: the pill still opens, to show the work
            self.fb.working()
            recorder.on_level = None
            self.tail = Tail(recorder, TAIL_SECONDS if tail else 0)
            worker = threading.Thread(target=self._run_out, args=(self.tail, live, self.second_work, self.live_app), daemon=True)
            worker.start()
            self.threads.append(worker)

    def _cut_tail(self):
        """A new press: the old clip ends here, whole, and the new one starts after it."""
        if self.tail:
            self.tail.cut.set()
            self.tail.stopped.wait(1)

    def _run_out(self, tail, live, second, app):
        recording = tail.run_out()
        with self.lock:  # after `stopped` is set, so _cut_tail can wait for it while holding the lock
            if self.tail is tail:
                self.tail = None
        if live:
            self.process_live(recording, live, tail.released, app)
            return
        recording = audio.without_empty_tail(recording, tail.released)
        if second:
            self.process_second(second, recording)
        else:
            self.process(recording)

    def transcribe(self, speech) -> tuple[str, str]:
        return transcribe(self.cfg, self.local, speech)

    def _transcribe_live(self, speech) -> str:
        """No fallback here: if a request fails, typing waits for the last pass, which has it."""
        return self.local.transcribe(speech) if self.cfg.provider == "local" else transcribe_api(self.cfg, speech)

    def _types_live(self) -> bool:
        """Live typing needs text that is final as it is spoken, and a place to type it. Anything that changes the
        whole text afterwards, or keeps it out of the app, gets the normal paste at the end."""
        if self.second_work or not self.cfg.live_text or self.cfg.post_hook.strip() or extensions.rewriter(self):
            return False
        self.live_app = self.focus()
        return paste.rule_for(self.cfg.app_rules, self.live_app) not in ("off", "clipboard")

    def _type_live(self, text: str, app: str) -> str:
        rule = paste.rule_for(self.cfg.app_rules, app)
        with self.paste_lock:
            return self.paster(text, app=app, shift={"shift": True, "paste": False}.get(rule), submit=False)

    def process_live(self, recording, live, released, app):
        """The end of a recording typed live: the rest of the words, then what a paste would have done."""
        started = time.monotonic()
        try:
            text = live.finish(recording, lambda speech: self.transcribe(speech)[0], released)
        except Exception as e:
            self._fail(e)
            return
        self.last_used = time.monotonic()
        print(f"typed live, the end took {self.last_used - started:.1f}s: {text!r}", flush=True)
        if text:
            self.last_text, self.last_app = text, app
        if live.note:
            self.copier(text)
            self.fb.error(live.note)
            return
        if not text and audio.is_dead(recording[:released]):
            self.fb.error("The microphone is silent. Check that it isn't muted.", icon="mic")
            return
        self.fb.done()
        if not text:
            return
        self.fb.clear_problem()
        rule = paste.rule_for(self.cfg.app_rules, app)
        if rule == "submit" or (self.cfg.auto_submit and rule != "paste"):
            paste.press_enter()
        self._remember(text, text, app)
        extensions.after_dictation(self, recording, text, text, text, app)

    def _fail(self, e: Exception):
        self.fb.error(str(e), getattr(e, "brief", ""), getattr(e, "icon", "alert"))

    def process(self, recording):
        started = time.monotonic()
        try:
            speech = audio.speech_only(recording)
            raw, source = ("", "-") if speech is None else self.transcribe(speech)
        except Exception as e:
            self._fail(e)
            return
        if speech is None and audio.is_dead(recording):
            self.fb.error("The microphone is silent. Check that it isn't muted.", icon="mic")
            return
        self.last_used = time.monotonic()
        text = textproc.clean(raw, self.cfg)
        print(f"transcribed ({source}) in {self.last_used - started:.1f}s: {raw!r}" + (f" -> {text!r}" if text != raw.strip() else ""), flush=True)
        if not text:
            self.fb.done()
            return
        app = self.focus()
        dictated = text
        rewrite = extensions.rewriter(self)
        if rewrite:
            text = rewrite(text, app)
        self.fb.done()
        if self.cfg.post_hook.strip():
            text = textproc.run_hook(self.cfg.post_hook, text, {"HOLD_DICTATE_APP": app, "HOLD_DICTATE_PROVIDER": source})
            if not text:
                print("hold-dictate: the text hook asked not to paste this", flush=True)
                return
        self.last_text, self.last_app = text, app
        if self.deliver(text, app):
            self._remember(text, raw, app)
        extensions.after_dictation(self, recording, raw, dictated, text, app)

    def _remember(self, text: str, raw: str, app: str):
        try:
            history.add(self.cfg, text, raw, app)
        except Exception as e:  # the text is already where it belongs
            print(f"hold-dictate: could not save to the history: {e}", file=sys.stderr, flush=True)

    def process_second(self, work, recording):
        """The second hotkey's recording, in the hands of the extension that added it."""
        try:
            work(recording)
        except Exception as e:
            self._fail(e)

    def deliver(self, text: str, app: str, submit_allowed: bool = True) -> bool:
        """Returns True when the text was pasted into the app."""
        rule = paste.rule_for(self.cfg.app_rules, app)
        if rule == "off":
            return False
        if rule == "clipboard":
            note = self.copier(text)
        else:
            shift = {"shift": True, "paste": False}.get(rule)  # None: terminals get Ctrl+Shift+V
            submit = submit_allowed and (rule == "submit" or (self.cfg.auto_submit and rule != "paste"))
            with self.paste_lock:
                note = self.paster(text, app=app, shift=shift, submit=submit)
        if note:
            self.fb.error(note)
        else:
            self.fb.clear_problem()
        return not note and rule != "clipboard"

    def paste_last(self) -> str:
        """Pastes the last dictation again into the focused app (never presses Enter)."""
        if not self.last_text:
            return "nothing dictated yet"
        app = self.focus()
        shift = {"shift": True, "paste": False}.get(paste.rule_for(self.cfg.app_rules, app))
        with self.paste_lock:
            return self.paster(self.last_text, app=app, shift=shift, submit=False)

    def copy_last(self) -> str:
        if not self.last_text:
            return "nothing dictated yet"
        return self.copier(self.last_text)

    def idle(self) -> bool:
        with self.lock:  # a silence stop finishes on another thread; never see it between stop and worker
            self.threads = [t for t in self.threads if t.is_alive()]
            return self.recorder is None and not self.threads

    def tick(self):
        """Frees the local model after a long idle stretch, and clears out history that has reached its age."""
        if time.monotonic() - self.pruned >= PRUNE_SECONDS:
            self.pruned = time.monotonic()
            history.prune(self.cfg)
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
    hotkey_problem = ""
    try:
        hotkey = parse_hotkey(cfg.hotkey)
    except ValueError as e:  # fall back to the default so the settings page can fix it
        hotkey_problem = f"The shortcut {cfg.hotkey!r} in the settings file doesn't work ({e}), so Alt + Space is used for now."
        print(f"hold-dictate: {hotkey_problem}", file=sys.stderr, flush=True)
        cfg.hotkey = Config().hotkey  # otherwise every save on the settings page would fail on it
        hotkey = parse_hotkey(cfg.hotkey)

    feedback = Feedback(cfg)
    detector = HoldDetector(hotkey, extensions.second_hotkey(cfg, hotkey))
    dictation = Dictation(cfg, feedback)
    updater = Updater(cfg, notify=feedback.notify, is_idle=dictation.idle)
    feedback.pending_update = lambda: updater.latest if updater.available() and not updater.ready else ""
    updater.on_failed = feedback.update_failed
    updater.announce = not feedback.events  # the pill ticks instead

    def on_saved(new_cfg: Config):
        new_hotkey = parse_hotkey(new_cfg.hotkey)
        server.hotkey_problem = ""  # a saved shortcut is always a valid one
        if str(new_hotkey) != str(detector.hotkey) and (detector.set_hotkey(new_hotkey) or dictation.latched):
            dictation.finish()
        new_second = extensions.second_hotkey(new_cfg, new_hotkey)
        if str(new_second) != str(detector.second) and (detector.set_second_hotkey(new_second) or (dictation.latched and dictation.second_work)):
            dictation.finish()

    server = SettingsServer(cfg, on_saved=on_saved, updater=updater, session=dictation).start()
    server.problem, server.hotkey_problem = problem, hotkey_problem
    dictation.on_no_key = server.open
    extensions.started(dictation, server, detector)

    def tick():
        dictation.tick()
        updater.tick()
        extensions.tick()

    def listen():
        if backend is None:
            while not done.wait(5):
                tick()
            return
        try:
            backend.run(detector, dictation.on_action, tick)
        finally:
            done.set()

    done = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: done.set())  # a clean exit, so other apps get their volume back
    threading.Thread(target=listen, daemon=True).start()
    if backend:
        print(f"hold-dictate: {'hold' if cfg.hotkey_mode == 'hold' else 'tap'} {hotkey} to dictate ({cfg.provider}, keyboard via {backend.name})", flush=True)
    print(f"hold-dictate: settings at {server.url}", flush=True)
    try:
        if tray.available():
            try:
                icon = tray.Tray(on_settings=server.open, on_check_updates=lambda: (updater.check(), server.open()),
                                 on_copy_last=lambda: feedback.notify(dictation.copy_last() or "Copied the last dictation"), on_quit=done.set)
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
