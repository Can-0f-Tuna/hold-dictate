"""Speech-to-text backends: any OpenAI-compatible HTTP API, or faster-whisper on this machine."""
import json
import os
import sys
import urllib.error
import urllib.request
import threading
import uuid

from . import extensions
from .audio import SAMPLE_RATE, Problem, wav_bytes
from .config import HEBREW_PROMPT, LANGUAGE_NAMES, LANGUAGES, Config

SAMPLES = {code: sentence for code, _, sentence in LANGUAGES if sentence}


class TranscribeError(Problem):
    pass


def multipart(fields: dict, filename: str, content: bytes, content_type: str):
    boundary = uuid.uuid4().hex
    body = b"".join(
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode("utf-8")
        for k, v in fields.items()
    )
    body += (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def _brief_http_error(cfg: Config, code: int) -> str:
    label = cfg.preset()["label"]
    return {
        401: "The API key was rejected. Enter it again in the settings.",
        403: "This API key isn't allowed to use speech-to-text.",
        404: "The speech service doesn't know this model. Check the settings.",
        429: f"Too many requests to {label}. Wait a minute and try again.",
    }.get(code, f"{label} had a problem (error {code}). Try again.")


def _explain_http_error(cfg: Config, e: urllib.error.HTTPError) -> str:
    try:
        detail = json.load(e)["error"]["message"]
    except Exception:
        detail = e.reason
    hints = {
        401: "the API key was rejected. Enter it again in the settings (`hold-dictate settings`).",
        403: "access denied. Check that the key is allowed to use speech-to-text.",
        404: "endpoint or model not found. Check base_url and model in your config.",
        429: "rate limit or quota reached. Wait a bit, or switch provider.",
    }
    return f"{cfg.preset()['label']} returned {e.code}: {hints.get(e.code, detail)} ({detail})"


def language_hints(cfg: Config) -> tuple[str, str]:
    """(language to force or '', prompt) for the languages you picked.

    One language is forced. Hebrew left to auto-detect in the cloud came out in other scripts on short clips
    ("בדיקה" as "Бдика" or "Bidika"), and forcing it cost the English words in mixed sentences nothing.
    Several languages stay on auto-detect, and the prompt holds a sentence in each of them, which steers
    the model toward them instead of a similar-sounding language."""
    codes = cfg.language_codes()
    force = codes[0] if len(codes) == 1 else ""
    parts, covered = [], set()
    if "he" in codes and (codes == ["he"] or "en" in codes):
        parts, covered = [HEBREW_PROMPT], {"he", "en"}  # Hebrew with English words in Latin letters
    if not force:
        parts += [SAMPLES[c] for c in codes if c in SAMPLES and c not in covered]
    if cfg.prompt:
        parts.append(cfg.prompt)
    return force, extensions.adjust_prompt(" ".join(parts), cfg)


def detected_code(name: str) -> str:
    """'hebrew', 'Hebrew' or 'he' -> 'he'; '' when unknown."""
    name = (name or "").strip().lower()
    if name in LANGUAGE_NAMES:
        return name
    return next((code for code, full in LANGUAGE_NAMES.items() if full.lower() == name), "")


def transcribe_api(cfg: Config, audio) -> str:
    """Several languages: Whisper models report what they heard, and a language outside your list is
    transcribed again in your first language (Hebrew misheard as Arabic or Yiddish, for example)."""
    force, prompt = language_hints(cfg)
    codes = cfg.language_codes()
    verify = len(codes) > 1 and cfg.provider in ("groq", "openai") and "whisper" in cfg.effective_model()
    text, heard, sure = _request(cfg, audio, force, prompt, "verbose_json" if verify else "json")
    if not verify:
        return text
    code = detected_code(heard)
    if code not in ("", *codes):
        return _request(cfg, audio, codes[0], prompt, "json")[0]
    again = lambda language: _request(cfg, audio, language, prompt, "verbose_json")
    return extensions.review_transcript(text, cfg, code, sure, again, passes=TranscribeError)


def _request(cfg: Config, audio, language: str, prompt: str, response_format: str) -> tuple[str, str, float | None]:
    """(text, the language it reports, its average avg_logprob or None)."""
    key, base = cfg.effective_key(), cfg.effective_base_url()
    if not key:
        raise TranscribeError("no API key set. Add one in the settings (`hold-dictate settings`).")
    if not base:
        raise TranscribeError("base_url is not set for the custom provider.")
    fields = {"model": cfg.effective_model(), "response_format": response_format, "temperature": "0"}
    if language:
        fields["language"] = language
    if prompt:
        fields["prompt"] = prompt
    body, content_type = multipart(fields, "speech.wav", wav_bytes(audio), "audio/wav")
    request = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type, "User-Agent": "hold-dictate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            data = json.load(response)
            sure = [s["avg_logprob"] for s in data.get("segments") or [] if "avg_logprob" in s]
            return data["text"].strip(), str(data.get("language") or ""), sum(sure) / len(sure) if sure else None
    except urllib.error.HTTPError as e:
        raise TranscribeError(_explain_http_error(cfg, e), _brief_http_error(cfg, e.code)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TranscribeError(f"could not reach {base}: {e}", f"Can't reach {cfg.preset()['label']}. Check your internet connection.", "net") from e


def check_api(cfg: Config) -> str:
    """Cheap authenticated call used by `setup` and `doctor`. Returns '' when fine, else a message."""
    key, base = cfg.effective_key(), cfg.effective_base_url()
    if not key:
        return "no API key set"
    request = urllib.request.Request(
        f"{base}/models", headers={"Authorization": f"Bearer {key}", "User-Agent": "hold-dictate"}
    )
    try:
        urllib.request.urlopen(request, timeout=cfg.timeout).close()
        return ""
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return _explain_http_error(cfg, e)
        return ""  # some servers have no /models route; the key was not refused
    except Exception as e:
        return f"could not reach {base}: {e}"


def physical_cores(cpuinfo: str = "/proc/cpuinfo") -> int:
    """Cores, not hardware threads: CTranslate2 gains nothing from a core's second thread, and loses when it gets half
    the cores of a computer that has none (Apple silicon, Intel Core Ultra 200). Measured in the accuracy harness
    (eval/speed.py): on 8 cores without SMT, 8 threads took a pass from 4.6 s to 3.1 s, the same text."""
    import subprocess
    logical = os.cpu_count() or 2
    try:
        if sys.platform == "darwin":
            found = int(subprocess.run(["sysctl", "-n", "hw.physicalcpu"], capture_output=True, text=True, timeout=2).stdout)
        else:
            with open(cpuinfo, encoding="utf-8") as f:
                cores, current = set(), {}
                for line in f:
                    key, _, value = line.partition(":")
                    key = key.strip()
                    if key in ("physical id", "core id"):
                        current[key] = value.strip()
                    elif not key:
                        cores.add((current.get("physical id"), current.get("core id")))
                        current = {}
                cores.add((current.get("physical id"), current.get("core id")))
                found = len({c for c in cores if c[1] is not None})
    except (OSError, ValueError, subprocess.SubprocessError):
        found = 0
    return max(1, min(found, logical) if found else logical // 2)


class LocalTranscriber:
    """faster-whisper, loaded on first use or ahead of it, and freed after a stretch of inactivity."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = None
        self.loaded = ""  # name of the model in memory, so a change in the settings loads the new one
        self.lock = threading.Lock()  # the live words and the final text can ask at the same time

    def preload(self):
        """Loads the model in the background, so the first dictation doesn't wait for it. A dictation that comes
        first waits for the load in progress instead of starting another."""
        def run():
            try:
                with self.lock:
                    self.load()
            except Exception as e:  # the dictation reports it
                print(f"hold-dictate: the local model didn't load yet: {e}", file=sys.stderr, flush=True)
        threading.Thread(target=run, daemon=True).start()

    @staticmethod
    def available() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
        if self.model is not None and self.loaded != self.cfg.local_model:
            self.model = None
        if self.model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as e:
                raise TranscribeError(
                    "local transcription is not installed. Reinstall with the `local` extra: "
                    'uv tool install "hold-dictate[local]"',
                    "Local transcription isn't installed. Pick another service in the settings.",
                ) from e
            self.model = WhisperModel(self.cfg.local_model, device="cpu", compute_type="int8", cpu_threads=physical_cores())
            self.loaded = self.cfg.local_model
        return self.model

    def unload(self):
        self.model = None

    def transcribe(self, audio) -> str:
        with self.lock:
            return self._transcribe(audio)

    def _transcribe(self, audio) -> str:
        model, (force, prompt), codes = self.load(), language_hints(self.cfg), self.cfg.language_codes()
        run = lambda language: model.transcribe(
            audio, language=language or None, beam_size=5, vad_filter=False, initial_prompt=prompt or None,
        )
        segments, info = run(force)
        if not force and codes and info.language not in codes:
            # Detection happens up front and the segments are lazy, so switching costs only the detection.
            probs = dict(getattr(info, "all_language_probs", None) or [])
            segments, _ = run(max(codes, key=lambda c: probs.get(c, 0.0)))
        return " ".join(s.text.strip() for s in segments).strip()
