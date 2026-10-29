import numpy as np
import pytest

from hold_dictate import audio, config
from hold_dictate.providers import TranscribeError, check_api, transcribe_api


def test_config_roundtrip(tmp_path):
    cfg = config.Config(provider="openai", api_key='k"ey', prompt="שלום\nworld", min_hold=0.5, sounds=False)
    path = config.save(cfg, tmp_path / "c.toml")
    assert config.load(path) == cfg


def test_config_defaults_and_unknown_keys(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('provider = "local"\nbogus = 1\n')
    cfg = config.load(p)
    assert cfg.provider == "local" and cfg.hotkey == "alt+space"


def test_env_key_fallback(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert config.Config(provider="groq").effective_key() == "from-env"
    assert config.Config(provider="groq", api_key="file").effective_key() == "file"


def tone(seconds, freq=220, amp=0.3):
    t = np.arange(int(audio.SAMPLE_RATE * seconds)) / audio.SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * amp).astype(np.float32)


def test_vad_drops_silence_and_keeps_speech():
    quiet = (np.random.default_rng(0).standard_normal(audio.SAMPLE_RATE * 2) * 0.0005).astype(np.float32)
    assert audio.speech_only(quiet) is None
    clip = np.concatenate([quiet, tone(1.0), quiet])
    speech = audio.speech_only(clip)
    assert speech is not None and 0.9 * audio.SAMPLE_RATE < len(speech) < 2.0 * audio.SAMPLE_RATE


def test_wav_roundtrip(tmp_path):
    p = tmp_path / "a.wav"
    p.write_bytes(audio.wav_bytes(tone(0.5)))
    back = audio.read_audio_file(str(p))
    assert len(back) == int(audio.SAMPLE_RATE * 0.5)


def make_cfg(api, **kw):
    return config.Config(provider="custom", base_url=api.url, api_key="secret", model="m1", **kw)


def test_api_request_shape(api):
    api.text = "  שלום world "
    out = transcribe_api(make_cfg(api, languages="fr", prompt="hint"), tone(0.5))
    assert out == "שלום world"
    method, path, auth, body = api.requests[-1]
    assert path == "/v1/audio/transcriptions" and auth == "Bearer secret"
    for needle in (b'name="model"\r\n\r\nm1', b'name="language"\r\n\r\nfr', b'name="prompt"\r\n\r\nhint', b"RIFF"):
        assert needle in body


@pytest.mark.parametrize("status,word", [(401, "key"), (429, "rate limit")])
def test_api_errors_are_explained(api, status, word):
    api.status = status
    with pytest.raises(TranscribeError, match=word):
        transcribe_api(make_cfg(api), tone(0.5))


def test_missing_key_and_unreachable():
    with pytest.raises(TranscribeError, match="API key"):
        transcribe_api(config.Config(provider="groq"), tone(0.5))
    dead = config.Config(provider="custom", base_url="http://127.0.0.1:9/v1", api_key="k", timeout=2)
    with pytest.raises(TranscribeError, match="could not reach"):
        transcribe_api(dead, tone(0.5))


def test_check_api(api):
    assert check_api(make_cfg(api)) == ""
    api.status = 401
    assert "key" in check_api(make_cfg(api))


def test_language_hints():
    from hold_dictate.config import HEBREW_PROMPT
    from hold_dictate.providers import language_hints
    assert language_hints(config.Config(languages="fr")) == ("fr", "")
    assert language_hints(config.Config(languages="he")) == ("", HEBREW_PROMPT)  # forced Hebrew mangles English
    assert language_hints(config.Config(languages="he", provider="local")) == ("he", HEBREW_PROMPT)
    assert language_hints(config.Config(languages="he,en")) == ("", HEBREW_PROMPT)
    force, prompt = language_hints(config.Config(languages="he,en,ru", prompt="Kubernetes"))
    assert force == "" and prompt.startswith(HEBREW_PROMPT) and "Привет" in prompt and prompt.endswith("Kubernetes")
    assert language_hints(config.Config()) == ("", "")


def test_language_outside_the_list_is_transcribed_again(api, monkeypatch):
    from hold_dictate import providers
    answers = iter([("مرحبا", "arabic"), ("שלום", "")])
    sent = []
    monkeypatch.setattr(providers, "_request", lambda cfg, audio, lang, prompt, fmt: sent.append((lang, fmt)) or next(answers))
    cfg = config.Config(provider="groq", api_key="k", languages="he,en")
    assert transcribe_api(cfg, tone(0.5)) == "שלום"
    assert sent == [("", "verbose_json"), ("he", "json")]
