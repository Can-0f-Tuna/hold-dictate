"""Premium features, client side: edit by voice, writing that fits the app, and a dictionary that learns.

The work happens on the hold-dictate server, with your own Groq key: this module only sends the text
involved and hands back the answer. It never logs the key or the text.
"""
import difflib
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request

from . import __version__

TIMEOUTS = {"edit": 15.0, "style": 6.0, "learn": 15.0}
MAX_TEXT, MAX_INSTRUCTION, MAX_APP = 8000, 1000, 200
MAX_CORRECTIONS, MAX_TERMS, MAX_FIXES, MAX_WORD = 20, 100, 100, 60
LEARN_EVERY = 60.0  # seconds between learn calls; transcripts in between are sent together
SIMILAR = 0.7  # a copied text this close to the last dictation is a corrected version of it
SIGN_IN, PREMIUM_ONLY = "Sign in first.", "This is a Premium feature."
NO_GROQ_KEY = "Premium features use a Groq key. Switch the service to Groq, or set GROQ_API_KEY."


class PremiumError(RuntimeError):
    """A short message for the user."""


def groq_key(cfg) -> str:
    return (cfg.saved_key() if cfg.provider == "groq" else "") or os.environ.get("GROQ_API_KEY", "")


def terms_of(text: str) -> list[str]:
    """'Kubernetes, Vercel,, kubernetes' -> ['Kubernetes', 'Vercel']: trimmed, no repeats in any case."""
    out, seen = [], set()
    for part in str(text or "").split(","):
        term = part.strip()[:MAX_WORD]
        if term and term.lower() not in seen:
            seen.add(term.lower())
            out.append(term)
    return out


def merged_fixes(learned: dict, own: dict) -> dict:
    """The learned fixes under your own word replacements: on the same word (in any case), yours win."""
    if not isinstance(learned, dict) or not learned:
        return own
    mine = {str(k).strip().lower() for k in own}
    return {**{k: v for k, v in learned.items() if str(k).strip().lower() not in mine}, **own}


class Client:
    """Talks to {site}/api/premium/*. Raises PremiumError with a sentence for the user."""

    def __init__(self, cfg, account):
        self.cfg, self.account = cfg, account

    def problem(self) -> str:
        """'' when premium features can run now, else why not."""
        if self.account is None or not self.account.signed_in():
            return SIGN_IN
        if not self.account.premium():
            return PREMIUM_ONLY
        if not groq_key(self.cfg):
            return NO_GROQ_KEY
        return ""

    def _refresh_account(self):
        """Signed out or Premium turned off on the server: let the account notice, without waiting for it."""
        threading.Thread(target=lambda: self.account.check(max_age=0, timeout=5), daemon=True).start()

    def _post(self, kind: str, body: dict) -> dict:
        problem = self.problem()
        if problem:
            raise PremiumError(problem)
        headers = {
            "Authorization": f"Bearer {self.account.data().get('token', '')}", "X-Groq-Key": groq_key(self.cfg),
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"hold-dictate/{__version__}",
        }
        request = urllib.request.Request(f"{self.account.site}/api/premium/{kind}", data=json.dumps(body).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUTS[kind]) as response:
                reply = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            try:
                error = str((json.loads(e.read() or b"{}") or {}).get("error", ""))
            except Exception:
                error = ""
            raise PremiumError(self._explain(e.code, error)) from None
        except (TimeoutError, OSError) as e:  # URLError is an OSError
            slow = isinstance(e, TimeoutError) or "timed out" in str(e)
            raise PremiumError("The hold-dictate server took too long to answer." if slow
                               else "Couldn't reach the hold-dictate server. Check the internet connection.") from None
        except ValueError:
            raise PremiumError("The hold-dictate server sent an answer hold-dictate doesn't understand.") from None
        if not isinstance(reply, dict):
            raise PremiumError("The hold-dictate server sent an answer hold-dictate doesn't understand.")
        return reply

    def _explain(self, status: int, error: str) -> str:
        if status == 401:
            self._refresh_account()
            return "You're signed out. Sign in again on the settings page."
        if status == 403:
            self._refresh_account()
            return "Premium is off for this account."
        if status == 400:
            return "Groq didn't accept your key. Check it in the settings." if error == "groq key" else "The server couldn't use this text."
        if status == 429:
            return "Groq is busy. Try again in a moment."
        if status == 502:
            return "Groq didn't answer. Try again."
        return "The hold-dictate server is busy. Try again in a moment."

    def _text(self, reply: dict) -> str:
        text = reply.get("text")
        if not isinstance(text, str):
            raise PremiumError("The hold-dictate server sent an answer hold-dictate doesn't understand.")
        return text

    def edit(self, text: str, instruction: str, app: str = "") -> str:
        if not text.strip():
            raise PremiumError("Select some text first.")
        if len(text) > MAX_TEXT:
            raise PremiumError(f"That's too much text to edit at once ({MAX_TEXT} characters at most).")
        return self._text(self._post("edit", {"text": text, "instruction": instruction[:MAX_INSTRUCTION],
                                              "app": app[:MAX_APP], "languages": self.cfg.language_codes()}))

    def style(self, text: str, app: str = "") -> str:
        if len(text) > MAX_TEXT:
            raise PremiumError("too long to restyle")
        return self._text(self._post("style", {"text": text, "app": app[:MAX_APP], "languages": self.cfg.language_codes()}))

    def learn(self, transcript: str, corrections: list, terms: list, fixes: dict) -> tuple[list, dict]:
        reply = self._post("learn", {
            "transcript": transcript[-MAX_TEXT:],
            "corrections": [c for c in corrections if len(c["heard"]) <= MAX_WORD and len(c["written"]) <= MAX_WORD][-MAX_CORRECTIONS:],
            "terms": terms[-MAX_TERMS:],
            "fixes": dict(list(fixes.items())[-MAX_FIXES:]),
        })
        new_terms, new_fixes = reply.get("terms"), reply.get("fixes")
        if not isinstance(new_terms, list) or not isinstance(new_fixes, dict):
            raise PremiumError("The hold-dictate server sent an answer hold-dictate doesn't understand.")
        return ([str(t) for t in new_terms if isinstance(t, str)],
                {str(k): str(v) for k, v in new_fixes.items() if isinstance(k, str) and isinstance(v, str)})


# ---------- a dictionary that learns ----------

_EDGE = re.compile(r"^[\"'“”‘’«»()\[\]{}.,!?;:…]+|[\"'“”‘’«»()\[\]{}.,!?;:…]+$")


def _words(text: str) -> list[str]:
    return [w for w in (_EDGE.sub("", w) for w in text.split()) if w]


def corrections(dictated: str, edited: str) -> list[dict]:
    """The words you changed in a dictation: [{heard, written}], short ones only. Empty when `edited` isn't
    a corrected version of `dictated` (the same text, or too different to be one)."""
    if not dictated or not edited or edited == dictated:
        return []
    if difflib.SequenceMatcher(None, dictated, edited, autojunk=False).ratio() < SIMILAR:
        return []
    a, b = _words(dictated), _words(edited)
    found = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, [w.lower() for w in a], [w.lower() for w in b], autojunk=False).get_opcodes():
        if op != "replace" or i2 - i1 > 3 or j2 - j1 > 3:
            continue
        heard, written = " ".join(a[i1:i2]), " ".join(b[j1:j2])
        if heard.lower() != written.lower() and len(heard) <= MAX_WORD and len(written) <= MAX_WORD:
            found.append({"heard": heard, "written": written})
    return found


class Learner:
    """Collects dictations and corrections, and sends them to `learn` at most once a minute, in the
    background. `save(terms, fixes)` stores what came back."""

    def __init__(self, cfg, client: Client, save, every: float = LEARN_EVERY):
        self.cfg, self.client, self.save, self.every = cfg, client, save, every
        self.lock = threading.Lock()
        self.transcripts: list[str] = []
        self.fixed: list[dict] = []
        self.last_sent = -1e9
        self.timer = None
        self.busy = False

    def enabled(self) -> bool:
        return bool(self.cfg.premium_learn) and not self.client.problem()

    def add(self, transcript: str = "", fixed: list | None = None):
        if not self.enabled():
            return
        with self.lock:
            if transcript:
                self.transcripts.append(transcript)
                while len(self.transcripts) > 1 and sum(len(t) + 1 for t in self.transcripts) > MAX_TEXT:
                    self.transcripts.pop(0)
            self.fixed = (self.fixed + list(fixed or []))[-MAX_CORRECTIONS:]
            self._schedule()

    def _schedule(self):
        if self.timer is not None or self.busy or not (self.transcripts or self.fixed):
            return
        delay = max(0.0, self.last_sent + self.every - time.monotonic())
        self.timer = threading.Timer(delay, self.flush)
        self.timer.daemon = True
        self.timer.start()

    def flush(self):
        with self.lock:
            self.timer = None
            if self.busy or not (self.transcripts or self.fixed):
                return
            transcript, fixed = "\n".join(self.transcripts)[-MAX_TEXT:], self.fixed
            self.transcripts, self.fixed, self.busy = [], [], True
            self.last_sent = time.monotonic()
        terms, fixes = terms_of(self.cfg.learned_terms), dict(self.cfg.learned_fixes or {})
        try:
            if self.enabled():
                new_terms, new_fixes = self.client.learn(transcript, fixed, terms, fixes)
                self.save(*self._apply(terms, fixes, new_terms, new_fixes))
        except PremiumError as e:
            print(f"hold-dictate: the dictionary didn't learn this time: {e}", file=sys.stderr, flush=True)
        except Exception as e:
            print(f"hold-dictate: the dictionary didn't learn this time ({type(e).__name__})", file=sys.stderr, flush=True)
        finally:
            with self.lock:
                self.busy = False
                self._schedule()  # what came in meanwhile

    def _apply(self, sent_terms, sent_fixes, new_terms, new_fixes) -> tuple[list, dict]:
        """Applies only what the server changed to the lists as they are now, so a word you removed in the
        settings while the request was out stays removed."""
        now_terms, now_fixes = terms_of(self.cfg.learned_terms), dict(self.cfg.learned_fixes or {})
        sent_lower, new_lower = {t.lower() for t in sent_terms}, {t.lower() for t in new_terms}
        terms = [t for t in now_terms if t.lower() in new_lower or t.lower() not in sent_lower]
        have = {t.lower() for t in terms}
        terms += [t for t in terms_of(",".join(new_terms)) if t.lower() not in sent_lower and t.lower() not in have]
        fixes = {k: v for k, v in now_fixes.items() if k in new_fixes or k not in sent_fixes}
        fixes.update({k: v for k, v in new_fixes.items() if sent_fixes.get(k) != v and k.strip() and len(k) <= MAX_WORD and len(v) <= MAX_WORD})
        return terms[-MAX_TERMS:], dict(list(fixes.items())[-MAX_FIXES:])
