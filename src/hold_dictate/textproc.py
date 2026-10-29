"""Cleans a transcription before it is pasted: drops phrases Whisper invents from noise, applies your
word replacements, removes filler words, turns spoken punctuation into symbols, then runs your own
command on it if you set one. Each step is a setting; only the hallucination filter is on by default."""
import os
import re
import subprocess
import sys

# Whisper makes these up from noise or near-silence. Only a transcription that is nothing but one of them
# is dropped. "Thank you" and "תודה רבה" are left out: they are also things people really dictate.
HALLUCINATIONS = {
    "you", "bye", "thanks for watching", "thank you for watching", "thanks for watching and see you next time",
    "thank you for watching and see you next time", "please subscribe", "like and subscribe",
    "blank audio", "silence", "no speech", "music",
    "תודה שצפיתם", "תודה על הצפייה", "תודה רבה שצפיתם", "תודה רבה על הצפייה",
}
# Subtitle credits from the videos Whisper learned on ("Subtitles by the Amara.org community", "תורגם על ידי ...").
CREDITS = re.compile(
    r"^\W*(?:(?:subtitles|subtitled|captions|translated|transcribed)\s+by\b|(?:כתוביות|תרגום|תורגם|תמלול)\S*\s*(?::|על ידי|ע\"י))",
    re.IGNORECASE,
)

FILLERS = (
    "um", "umm", "uh", "uhh", "uhm", "er", "erm", "ah", "eh", "hmm", "hm", "mm", "mhm",
    "אה", "אהה", "אמ", "אממ", "אמממ", "המ", "הממ", "אהמ",
)

# Spoken name -> symbol, longest phrases first so "new paragraph" wins over "new".
SPOKEN = [
    ("new paragraph", "\n\n"), ("פסקה חדשה", "\n\n"), ("new line", "\n"), ("שורה חדשה", "\n"),
    ("question mark", "?"), ("סימן שאלה", "?"),
    ("exclamation mark", "!"), ("exclamation point", "!"), ("סימן קריאה", "!"),
    ("full stop", "."), ("period", "."), ("נקודה", "."),
    ("semicolon", ";"), ("colon", ":"), ("נקודתיים", ":"),
    ("comma", ","), ("פסיק", ","),
    ("open paren", "("), ("close paren", ")"),
]
HEBREW_PREFIXES = "ובלהמשכ"  # one-letter words Hebrew writes joined to the next word
HOOK_DROP = 77  # a hook that exits with this consumes the text: nothing is pasted
HOOK_TIMEOUT = 5.0


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


def is_hallucination(text: str) -> bool:
    stripped = text.strip()
    if stripped.startswith("♪"):
        return True
    words = _norm(stripped)
    if not words:
        return bool(stripped)  # nothing but punctuation or symbols
    if words in HALLUCINATIONS or "amara org" in words:
        return True
    return bool(CREDITS.match(stripped)) and len(words.split()) <= 12


def _tidy(text: str) -> str:
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"[ \t]+([,.?!:;)])", r"\1", text)
    return re.sub(r"[ \t]*\n[ \t]*", "\n", text).strip(" \t")


def _phrase(words: str) -> str:
    """Regex for a phrase as a whole word: spaces match any run of spaces, and a side that ends in a
    letter or digit must not touch another one ("c++" still matches before a space)."""
    body = r"\s+".join(map(re.escape, words.split()))
    left = r"(?<!\w)" if words[0].isalnum() else ""
    right = r"(?!\w)" if words[-1].isalnum() else ""
    return left + body + right


def apply_overrides(text: str, overrides: dict) -> str:
    """Your replacements, longest first. An empty replacement deletes the word. A Hebrew word also matches
    with a joined prefix letter, so "ורסל" -> "Vercel" turns "בורסל" into "ב-Vercel"."""
    if not isinstance(overrides, dict):
        return text
    pairs = {str(k).strip(): str(v) for k, v in overrides.items() if str(k).strip()}
    changed = text
    for heard in sorted(pairs, key=len, reverse=True):
        written = pairs[heard]
        pattern = _phrase(heard)
        hebrew = "א" <= heard[0] <= "ת" and len(heard) >= 3
        if hebrew:
            pattern = rf"(?<!\w)(?P<pre>[{HEBREW_PREFIXES}]{{0,2}})" + pattern[len(r"(?<!\w)"):]

        def replace(m, written=written, hebrew=hebrew):
            pre = m.group("pre") if hebrew else ""
            joiner = "-" if pre and written[:1].isascii() and written[:1].isalpha() else ""
            return pre + joiner + written

        changed = re.sub(pattern, replace, changed, flags=re.IGNORECASE)
    return _tidy(changed) if changed != text else text


def filler_list(custom: str) -> list[str]:
    words = [w.strip() for w in str(custom or "").split(",") if w.strip()]
    return words or list(FILLERS)


def remove_fillers(text: str, fillers) -> str:
    """Drops filler words with the commas or ellipsis around them; keeps sentence-ending punctuation.
    A sentence that started with a capital keeps one, and a sentence of nothing but fillers goes."""
    if not fillers:
        return text
    words = "|".join(_phrase(f) for f in sorted(fillers, key=len, reverse=True))
    pattern = re.compile(rf"(?:,[ \t]*)?(?:{words})(?:\.\.\.|…|,|-)*", re.IGNORECASE)
    parts = re.split(r"(?<=[.!?])(\s+)", text)
    out, capital = [], False  # capital: a dropped sentence before this one started with a capital
    for i, part in enumerate(parts):
        if i % 2:  # the whitespace between sentences
            out.append(part)
            continue
        cleaned = _tidy(pattern.sub("", part)).lstrip(" ,")
        first = re.search(r"\w", part)
        capital = capital or bool(first and first.group().isupper())
        if not re.search(r"\w", cleaned):
            out.append("")
            continue
        if capital and cleaned[0].islower():
            cleaned = cleaned[0].upper() + cleaned[1:]
        capital = False
        out.append(cleaned)
    result = _tidy("".join(out))
    return result if result != _tidy(text) else text


def spoken_punctuation(text: str) -> str:
    """"hello comma world question mark" -> "hello, world?". Whisper often writes its own punctuation next to
    the spoken word ("you? Question mark"); that one is swallowed so the symbol is not doubled."""
    changed = text
    for words, symbol in SPOKEN:
        pattern = rf"[ \t]*[,.?!:;]?[ \t]*{_phrase(words)}[,.!?]?[ \t]*(?P<next>\w)?"

        def replace(m, symbol=symbol):
            following = m.group("next") or ""
            if following and symbol in (".", "?", "!", "\n", "\n\n"):
                following = following.upper()
            space = " " if symbol in ",.?!:;)" and following else ""
            before = " " if symbol == "(" and m.start() > 0 else ""
            return before + symbol + space + following

        changed = re.sub(pattern, replace, changed, flags=re.IGNORECASE)
    return _tidy(changed) if changed != text else text


def clean(text: str, cfg) -> str:
    """The text to paste, or '' when there is nothing worth pasting."""
    text = text.strip()
    if not text or (cfg.drop_hallucinations and is_hallucination(text)):
        return ""
    if cfg.word_overrides:
        text = apply_overrides(text, cfg.word_overrides)
    if cfg.remove_fillers:
        text = remove_fillers(text, filler_list(cfg.filler_words))
    if cfg.spoken_punctuation:
        text = spoken_punctuation(text)
    return text.strip(" \t")


def run_hook(command: str, text: str, env: dict | None = None) -> str | None:
    """Pipes the text through your command. Its output replaces the text; empty output keeps it (so a hook
    can just log). Exit code 77 means "do not paste": returns None. On any failure the text goes through
    unchanged and the reason is logged."""
    try:
        result = subprocess.run(
            command, shell=True, input=text, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=HOOK_TIMEOUT, env=dict(os.environ, **(env or {})),
        )
    except subprocess.TimeoutExpired:
        print(f"hold-dictate: the text hook took over {HOOK_TIMEOUT:.0f}s; pasting the text unchanged", file=sys.stderr, flush=True)
        return text
    except OSError as e:
        print(f"hold-dictate: the text hook could not run ({e}); pasting the text unchanged", file=sys.stderr, flush=True)
        return text
    if result.returncode == HOOK_DROP:
        return None
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()[-1:] or [""]
        print(f"hold-dictate: the text hook failed (exit {result.returncode}) {detail[0]}; pasting the text unchanged", file=sys.stderr, flush=True)
        return text
    out = result.stdout.rstrip("\r\n")
    return out if out.strip() else text
