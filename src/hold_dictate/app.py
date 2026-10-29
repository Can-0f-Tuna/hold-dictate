"""The dictation loop: hotkey down -> record, hotkey up (or the next tap) -> transcribe -> clean -> paste."""
import signal
import sys
import threading
import time

from . import audio, config as cfgmod, paste, premium as premiummod, textproc
from .ducking import Ducker
from .config import Config
from .feedback import Feedback
from .hotkey import HoldDetector, choose_backend, parse_hotkey
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


class Dictation:
    def __init__(self, cfg: Config, feedback: Feedback, recorder_factory=None, paster=None, on_no_key=None, ducker=None,
                 focus=None, copier=None, account=None, grab=None, clipboard=None, premium=None, save_settings=None):
        self.cfg = cfg
        self.account = account  # None in tests; the app passes its Account, and dictation waits for a sign-in
        self.ducker = ducker or Ducker(cfg)
        self.on_no_key = on_no_key
        self.fb = feedback
        self.local = LocalTranscriber(cfg)
        self.recorder_factory = recorder_factory or audio.Recorder
        self.paster = paster or paste.paste_text
        self.focus = focus or paste.focused_app
        self.copier = copier or paste.copy_text
        self.grab = grab or paste.grab_selection  # app -> (selected text, put the clipboard back)
        self.clipboard = clipboard or paste.clipboard_text
        self.premium = premium or premiummod.Client(cfg, account)
        self.save_settings = save_settings or self._save_in_memory  # the app saves through the settings server
        self.learner = premiummod.Learner(cfg, self.premium, self._save_learned)
        self.keys_up = lambda: True  # the app waits here until the shortcut's keys are let go
        self.which = ""  # what the recording is for: "" dictation, "edit" edit by voice
        self.watch = ""  # the last pasted dictation, compared once with the clipboard to spot your corrections
        self.recorder = None
        self.latched = False  # auto and toggle: the key is up but the recording goes on
        self.voice = audio.VoiceMeter()
        self.pressed_at = 0.0
        self.last_used = time.monotonic()
        self.last_text = ""  # the last dictation, kept in memory only, for paste-last and copy-last
        self.last_app = ""  # the app it went to, offered on the settings page for a per-app rule
        self.lock = threading.RLock()
        self.paste_lock = threading.Lock()
        self.threads: list[threading.Thread] = []

    # Called by the hotkey backend: "start" when the hotkey goes down, "stop" when it comes up, and
    # "edit:start" and "edit:stop" for the edit-by-voice hotkey.
    def on_action(self, action: str):
        which, _, action = action.rpartition(":")
        with self.lock:
            mode = self.cfg.hotkey_mode
            if action == "start":
                self._check_correction()
                if self.recorder is None:
                    if which == "edit" and not self._edit_allowed():
                        return
                    self.begin(which)
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

    def _edit_allowed(self) -> bool:
        problem = self.premium.problem()
        if problem and (self.account is None or self.account.signed_in()):  # signed out: begin() says so
            self.fb.error("Edit by voice is a Premium feature." if problem == premiummod.PREMIUM_ONLY else problem)
            return False
        return True

    def begin(self, which: str = ""):
        if self.account is not None and not self.account.signed_in():
            self.fb.error("sign in first. The settings page is opening.")
            if self.on_no_key:
                self.on_no_key()
            return
        if self.cfg.provider != "local" and not self.cfg.effective_key():
            self.fb.error("no API key yet. Add one in the settings.")
            if self.on_no_key:
                self.on_no_key()
            return
        self.voice = audio.VoiceMeter()
        try:
            self.recorder = self.recorder_factory(self._level)
        except audio.MicError as e:
            self.fb.error(str(e))
            return
        self.latched, self.which = False, which
        self.fb.recording()
        self.ducker.duck()
        self.pressed_at = self.last_used = time.monotonic()
        if self.cfg.hotkey_mode != "hold" and self.cfg.silence_stop > 0:
            threading.Thread(target=self._watch_silence, args=(self.recorder,), daemon=True).start()

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
                    self.finish()
                    return

    def finish(self):
        """Stops the recording, if any, and transcribes it in the background."""
        with self.lock:
            if self.recorder is None:
                return
            recording, self.recorder, self.latched = self.recorder.stop(), None, False
            self.ducker.restore()
            if time.monotonic() - self.pressed_at < self.cfg.min_hold:
                self.fb.done()
                return
            self.fb.working()
            worker = threading.Thread(target=self.process_edit if self.which == "edit" else self.process, args=(recording,), daemon=True)
            worker.start()
            self.threads.append(worker)

    def transcribe(self, speech) -> tuple[str, str]:
        return transcribe(self.cfg, self.local, speech)

    def process(self, recording):
        started = time.monotonic()
        try:
            speech = audio.speech_only(recording)
            raw, source = ("", "-") if speech is None else self.transcribe(speech)
        except Exception as e:
            self.fb.error(str(e))
            return
        self.last_used = time.monotonic()
        text = textproc.clean(raw, self.cfg)
        print(f"transcribed ({source}) in {self.last_used - started:.1f}s: {raw!r}" + (f" -> {text!r}" if text != raw.strip() else ""), flush=True)
        if not text:
            self.fb.done()
            return
        app = self.focus()
        dictated = text
        if self.cfg.premium_style and not self.premium.problem():
            text = self._styled(text, app)
        self.fb.done()
        if self.cfg.post_hook.strip():
            text = textproc.run_hook(self.cfg.post_hook, text, {"HOLD_DICTATE_APP": app, "HOLD_DICTATE_PROVIDER": source})
            if not text:
                print("hold-dictate: the text hook asked not to paste this", flush=True)
                return
        self.last_text, self.last_app = text, app
        self.deliver(text, app)
        if self.learner.enabled():
            self.watch = text
            self.learner.add(dictated)  # sent in the background, at most once a minute

    def _styled(self, text: str, app: str) -> str:
        """The text rewritten to fit the app; on any failure the text as dictated: a dictation is never lost."""
        try:
            styled = self.premium.style(text, app)
            return styled.strip() or text
        except Exception as e:
            reason = str(e) if isinstance(e, premiummod.PremiumError) else type(e).__name__
            print(f"hold-dictate: pasting the text as dictated ({reason})", file=sys.stderr, flush=True)
            return text

    def process_edit(self, recording):
        """Edit by voice: the selected text, changed the way you said, pasted over the selection."""
        started = time.monotonic()
        app = self.focus()
        self.keys_up()  # a copy shortcut pressed while Alt and Shift are still down would be another shortcut
        with self.paste_lock:
            selected, restore = self.grab(app)
        if not selected.strip():
            restore()
            self.fb.error("Select some text first.")
            return
        try:
            speech = audio.speech_only(recording)
            raw, source = ("", "-") if speech is None else self.transcribe(speech)
            instruction = textproc.clean(raw, self.cfg)
            if not instruction:
                restore()
                self.fb.done()
                return
            result = self.premium.edit(selected, instruction, app)
            if not result.strip():
                raise premiummod.PremiumError("The server sent back no text, so the selection is left as it was.")
        except Exception as e:
            restore()
            self.fb.error(str(e))
            return
        self.last_used = time.monotonic()
        print(f"edited by voice ({source}) in {self.last_used - started:.1f}s", flush=True)  # never the text
        self.fb.done()
        rule = paste.rule_for(self.cfg.app_rules, app)
        self.deliver(result, app, submit_allowed=False)
        if rule != "clipboard":  # copy-only leaves the result on the clipboard for you to paste
            restore()

    # ---------- a dictionary that learns ----------

    def _check_correction(self):
        """At the next press after a dictation, looks once at the clipboard: a copied, corrected version
        of what was pasted tells which words were wrong."""
        last, self.watch = self.watch, ""
        if not last or not self.learner.enabled():
            return

        def run():
            copied = self.clipboard()
            fixed = premiummod.corrections(last, copied) if copied else []
            if fixed:
                self.learner.add(fixed=fixed)
        threading.Thread(target=run, daemon=True).start()

    def _save_learned(self, terms: list, fixes: dict):
        self.save_settings({"learned_terms": ", ".join(terms), "learned_fixes": fixes})

    def _save_in_memory(self, changes: dict):
        with self.lock:
            for name, value in changes.items():
                setattr(self.cfg, name, cfgmod.coerce(name, value))

    def deliver(self, text: str, app: str, submit_allowed: bool = True):
        rule = paste.rule_for(self.cfg.app_rules, app)
        if rule == "off":
            return
        if rule == "clipboard":
            note = self.copier(text)
        else:
            shift = {"shift": True, "paste": False}.get(rule)  # None: terminals get Ctrl+Shift+V
            submit = submit_allowed and (rule == "submit" or (self.cfg.auto_submit and rule != "paste"))
            with self.paste_lock:
                note = self.paster(text, app=app, shift=shift, submit=submit)
        if note:
            self.fb.notify(note)

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
        """Frees the local model after a long idle stretch."""
        if self.local.model is not None and self.recorder is None:
            if time.monotonic() - self.last_used >= self.cfg.idle_unload_seconds:
                self.local.unload()


def run(cfg: Config) -> int:
    from . import tray
    from .account import Account
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

    edit_hotkey = None
    if cfg.edit_hotkey.strip():
        try:
            edit_hotkey = parse_hotkey(cfg.edit_hotkey)
            if str(edit_hotkey) == str(hotkey):
                raise ValueError("it is the same as the dictation shortcut")
        except ValueError as e:  # turned off for now, so the settings page can still save
            print(f"hold-dictate: the edit-by-voice shortcut {cfg.edit_hotkey!r} doesn't work ({e}); it is off for now", file=sys.stderr, flush=True)
            cfg.edit_hotkey, edit_hotkey = "", None

    feedback = Feedback(cfg)
    detector = HoldDetector(hotkey, edit_hotkey)
    account = Account()
    dictation = Dictation(cfg, feedback, account=account)
    dictation.keys_up = lambda: detector.wait_released(1.0)
    updater = Updater(cfg, notify=feedback.notify, is_idle=dictation.idle)
    feedback.pending_update = lambda: updater.latest if updater.available() and not updater.ready else ""
    updater.on_failed = feedback.update_failed
    updater.announce = not feedback.events  # the pill ticks instead

    def on_saved(new_cfg: Config):
        new_hotkey = parse_hotkey(new_cfg.hotkey)
        server.hotkey_problem = ""  # a saved shortcut is always a valid one
        if str(new_hotkey) != str(detector.hotkey) and (detector.set_hotkey(new_hotkey) or dictation.latched):
            dictation.finish()
        new_edit = parse_hotkey(new_cfg.edit_hotkey) if new_cfg.edit_hotkey else None
        if str(new_edit) != str(detector.edit) and (detector.set_edit_hotkey(new_edit) or (dictation.latched and dictation.which == "edit")):
            dictation.finish()

    server = SettingsServer(cfg, on_saved=on_saved, updater=updater, session=dictation, account=account).start()
    server.problem, server.hotkey_problem = problem, hotkey_problem
    dictation.on_no_key = server.open
    dictation.save_settings = server.save  # under the server's lock, on top of the settings as they are then

    def tick():
        dictation.tick()
        updater.tick()
        account.tick()

    def listen():
        if backend is None:
            while not done.wait(5):
                updater.tick()
                account.tick()
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
    if not account.signed_in():
        print("hold-dictate: sign in on the settings page to start dictating", flush=True)
        server.open()
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
