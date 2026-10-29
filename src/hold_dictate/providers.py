"""Speech-to-text backends: any OpenAI-compatible HTTP API, or faster-whisper on this machine."""
import json
import urllib.error
import urllib.request
import uuid

from .audio import SAMPLE_RATE, wav_bytes
from .config import Config


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


def transcribe_api(cfg: Config, audio) -> str:
    key, base = cfg.effective_key(), cfg.effective_base_url()
    if not key:
        raise TranscribeError("no API key set. Add one in the settings (`hold-dictate settings`).")
    if not base:
        raise TranscribeError("base_url is not set for the custom provider.")
    fields = {"model": cfg.effective_model(), "response_format": "json", "temperature": "0"}
    if cfg.language:
        fields["language"] = cfg.language
    if cfg.prompt:
        fields["prompt"] = cfg.prompt
    body, content_type = multipart(fields, "speech.wav", wav_bytes(audio), "audio/wav")
    request = urllib.request.Request(
        f"{base}/audio/transcriptions",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": content_type, "User-Agent": "hold-dictate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
            return json.load(response)["text"].strip()
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

    @staticmethod
    def available() -> bool:
        try:
            import faster_whisper  # noqa: F401
            return True
        except ImportError:
            return False

    def load(self):
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
        return self.model

    def unload(self):
        self.model = None

    def transcribe(self, audio) -> str:
        segments, _ = self.load().transcribe(
            audio, language=self.cfg.language or None, beam_size=5, vad_filter=False,
            initial_prompt=self.cfg.prompt or None,
        )
        return " ".join(s.text.strip() for s in segments).strip()
