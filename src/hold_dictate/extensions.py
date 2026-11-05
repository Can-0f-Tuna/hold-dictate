"""Extensions: other packages that add to hold-dictate. It ships with none and works the same without them.

A package joins through the entry point group `hold_dictate.extensions`. The entry point names something to call
with no arguments, and what that returns is the extension: any object with the methods below that it needs.
A method that raises is logged and skipped, so a broken extension never stops a dictation.

Dictating:
  before_recording(dictation) -> str          runs at each press that would record; text says why it must not
  second_hotkey(cfg, hotkey) -> Hotkey | None a second shortcut beside `hotkey`; asked at start and after each save
  second_recording(dictation)                 at that shortcut's press: a function that takes the finished recording,
                                              None for a plain dictation, False to do nothing
  rewriter(dictation) -> callable | None      (text, app) -> text before pasting; while there is one, nothing is typed live
  after_dictation(dictation, audio, raw, cleaned, final, app)
                                              once the text went out, also when the app's rule is to do nothing. Not
                                              after a text hook drops it. Typed live, raw and cleaned are `final` too
Transcribing:
  transcribe(cfg, speech, force, prompt, live) -> str | None
                                              the text of `speech` (16 kHz float32 samples), in place of the
                                              service in the settings; None leaves it to that service. `force`
                                              (a language code or '') and `prompt` come from the languages you
                                              picked; `live` is a pass of live typing. A failure is logged and
                                              the service in the settings takes the clip
  transcribes(cfg) -> bool                    True when transcribe() takes dictations now, so no API key is needed
Text:
  adjust_prompt(prompt, cfg) -> str           the spelling hint sent to the transcriber
  adjust_words(overrides, cfg) -> dict        the word replacements used in clean-up
  review_transcript(text, cfg, heard, sure, again) -> str
                                              several languages: `heard` is the language code, `sure` its average
                                              logprob or None, `again(language)` asks once more -> (text, heard, sure)
The running app:
  started(dictation, server, detector)        everything is built; called once, before the hotkey is listened to
  tick()                                      every few seconds
  pill_lines() -> list[str]                   event lines for the overlay, sent before each recording shows
  notices() -> list[str]                      paths of QML notices for the overlay; see notices/README.md
Settings and commands:
  settings_fields() -> dict                   settings it owns, name -> default; they live in Config.extra
  settings_state(cfg) -> dict                 added to what the settings page gets
  settings_page() -> (html, script)           sections for the page, and a <script> that uses pageHooks in settings.html
  settings_routes(server) -> dict             "POST /path" -> f() answered as JSON behind the token, or
                                              "GET /path" -> f(handler, query), before any token check
  check_settings(new, changes)                may fix `new` before a save; ValueError refuses the save
  doctor_lines() -> list                      (good, text, fix) rows for `hold-dictate doctor`. Not on the settings
                                              page or with --offline, which stay on this computer
  extend_cli(commands)                        gets the argparse subparsers to add to
Updates:
  update_source() -> source                   where updates come from instead of GitHub. source.latest() gives the
                                              version tag and raises when it can't tell; source.install_args(tag,
                                              extras) gives what follows `uv tool install --force --python X`, with
                                              the app as updater.install_spec(tag, extras). An update, and installing
                                              the offline engine, replace the whole install, so an extension must
                                              name itself in those arguments. A source whose install_args takes only
                                              the tag still works, with the extras installed now
"""
import sys
from importlib.metadata import entry_points

GROUP = "hold_dictate.extensions"
FAILED = object()

_loaded = None
_broken = False  # an installed extension didn't load


def loaded() -> list:
    """The extensions, found and started on first use."""
    global _loaded, _broken
    if _loaded is None:
        found = []
        for point in entry_points(group=GROUP):
            try:
                found.append(point.load()())
            except Exception as e:
                _broken = True
                print(f"hold-dictate: the extension {point.name} didn't load ({type(e).__name__})", file=sys.stderr, flush=True)
        _loaded = found
    return _loaded


def _hooks(name: str) -> list:
    return [getattr(extension, name) for extension in loaded() if hasattr(extension, name)]


def _call(hook, *args, passes=()):
    """`passes`: exceptions that are part of the hook's answer. Others are the extension's own failure."""
    try:
        return hook(*args)
    except passes:
        raise
    except Exception as e:  # the type only: a message could hold dictated text
        print(f"hold-dictate: an extension failed in {getattr(hook, '__name__', 'its rewriter')} ({type(e).__name__})", file=sys.stderr, flush=True)
        return FAILED


def _all(name: str, *args, passes=()) -> list:
    results = [_call(hook, *args, passes=passes) for hook in _hooks(name)]
    return [result for result in results if result is not FAILED]


def _first(name: str, *args):
    return next((result for result in _all(name, *args) if result is not None), None)


def _through(name: str, value, *args, passes=()):
    """Passes `value` through every extension in turn."""
    for hook in _hooks(name):
        result = _call(hook, value, *args, passes=passes)
        if result is not FAILED:
            value = result
    return value


def _merged(name: str, *args) -> dict:
    return {key: value for part in _all(name, *args) for key, value in part.items()}


def before_recording(dictation) -> str:
    return next((problem for problem in _all("before_recording", dictation) if problem), "")


def second_hotkey(cfg, hotkey):
    return _first("second_hotkey", cfg, hotkey)


def second_recording(dictation):
    return _first("second_recording", dictation)


def rewriter(dictation):
    """A function (text, app) -> text, or None. If it fails, the text stays as it is."""
    rewrite = _first("rewriter", dictation)
    if rewrite is None:
        return None

    def safely(text: str, app: str) -> str:
        result = _call(rewrite, text, app)
        return text if result is FAILED else result
    return safely


def after_dictation(dictation, audio, raw: str, cleaned: str, final: str, app: str):
    _all("after_dictation", dictation, audio, raw, cleaned, final, app)


def transcribe(cfg, speech, hints, live: bool = False):
    """The text from the first extension that takes the clip, or None for the service in the settings.
    `hints(cfg)` gives (force, prompt), asked only when an extension transcribes."""
    hooks = _hooks("transcribe")
    if not hooks:
        return None
    force, prompt = hints(cfg)
    for hook in hooks:
        text = _call(hook, cfg, speech, force, prompt, live)
        if isinstance(text, str):
            return text
    return None


def transcribes(cfg) -> bool:
    return any(result is True for result in _all("transcribes", cfg))


def adjust_prompt(prompt: str, cfg) -> str:
    return _through("adjust_prompt", prompt, cfg)


def adjust_words(overrides: dict, cfg) -> dict:
    return _through("adjust_words", overrides, cfg)


def review_transcript(text: str, cfg, heard: str, sure, again, passes=()) -> str:
    return _through("review_transcript", text, cfg, heard, sure, again, passes=passes)


def started(dictation, server, detector):
    _all("started", dictation, server, detector)


def tick():
    _all("tick")


def pill_lines() -> list:
    return [line for lines in _all("pill_lines") for line in lines]


def notices() -> list:
    return [path for paths in _all("notices") for path in paths]


def settings_fields() -> dict:
    return _merged("settings_fields")


def settings_state(cfg) -> dict:
    return _merged("settings_state", cfg)


def settings_page() -> tuple:
    parts = _all("settings_page")
    return "".join(html for html, _ in parts), "".join(script for _, script in parts)


def settings_routes(server) -> dict:
    return _merged("settings_routes", server)


def check_settings(new, changes: dict):
    _all("check_settings", new, changes, passes=ValueError)


def update_source():
    """The source of an extension, or None for GitHub. False when an extension is broken: an update from GitHub
    would remove it, so it is safer to stay on this version until it is fixed."""
    results = [_call(hook) for hook in _hooks("update_source")]
    if _broken or FAILED in results:
        return False
    return next((source for source in results if source is not None), None)


def doctor_lines() -> list:
    return [line for lines in _all("doctor_lines") for line in lines]


def extend_cli(commands):
    _all("extend_cli", commands)
