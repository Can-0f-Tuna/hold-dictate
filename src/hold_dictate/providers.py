"""Speech-to-text backends: any OpenAI-compatible HTTP API, or faster-whisper on this machine."""
import json
import re
import urllib.error
import urllib.request
import uuid

from .audio import SAMPLE_RATE, wav_bytes
from .config import HEBREW_PROMPT, LANGUAGE_NAMES, LANGUAGES, Config

SAMPLES = {code: sentence for code, _, sentence in LANGUAGES if sentence}
# Whisper reads only the last 224 tokens of a prompt (Groq, OpenAI and faster-whisper alike). Learned words
# are added only while the prompt stays under this many UTF-8 bytes, about 3 per token, which leaves room.
PROMPT_BYTES = 600


class TranscribeError(RuntimeError):
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

    One language is forced, except Hebrew in the cloud: forcing it makes Whisper write English words in
    Hebrew letters. Several languages stay on auto-detect, and the prompt holds a sentence in each of them,
    which steers the model toward them instead of a similar-sounding language."""
    codes = cfg.language_codes()
    force = codes[0] if len(codes) == 1 and not (codes[0] == "he" and cfg.provider != "local") else ""
    parts, covered = [], set()
    if "he" in codes and (codes == ["he"] or "en" in codes):
        parts, covered = [HEBREW_PROMPT], {"he", "en"}  # Hebrew with English words in Latin letters
    if not force:
        parts += [SAMPLES[c] for c in codes if c in SAMPLES and c not in covered]
    if cfg.prompt:
        parts.append(cfg.prompt)
    prompt = " ".join(parts)
    if getattr(cfg, "premium_learn", False) and getattr(cfg, "learned_terms", ""):
        prompt = with_terms(prompt, cfg.learned_terms)
    return force, prompt


def with_terms(prompt: str, learned: str) -> str:
    """The prompt plus the learned words it doesn't have yet, as many as fit in PROMPT_BYTES."""
    from .premium import terms_of
    have = prompt.lower()
    added = []
    for term in terms_of(learned):
        if re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", have):
            continue
        candidate = ", ".join(added + [term]) + "."
        if len((prompt + " " + candidate).strip().encode("utf-8")) > PROMPT_BYTES:
            break
        added.append(term)
        have += " " + term.lower()
    return (prompt + " " + ", ".join(added) + ".").strip() if added else prompt


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
    text, heard = _request(cfg, audio, force, prompt, "verbose_json" if verify else "json")
    if verify and detected_code(heard) not in ("", *codes):
        text, _ = _request(cfg, audio, codes[0], prompt, "json")
    return text


def _request(cfg: Config, audio, language: str, prompt: str, response_format: str) -> tuple[str, str]:
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
            return data["text"].strip(), str(data.get("language") or "")
    except urllib.error.HTTPError as e:
        raise TranscribeError(_explain_http_error(cfg, e)) from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise TranscribeError(f"could not reach {base}: {e}") from e


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


class LocalTranscriber:
    """faster-whisper, loaded on first use and freed after a stretch of inactivity."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.model = None
        self.loaded = ""  # name of the model in memory, so a change in the settings loads the new one

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
                    'uv tool install "hold-dictate[local]"'
                ) from e
            import os
            self.model = WhisperModel(
                self.cfg.local_model, device="cpu", compute_type="int8", cpu_threads=max(1, (os.cpu_count() or 2) // 2)
            )
            self.loaded = self.cfg.local_model
        return self.model

    def unload(self):
        self.model = None

    def transcribe(self, audio) -> str:
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
