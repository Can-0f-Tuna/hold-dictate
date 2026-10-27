"""Settings: one TOML file in the per-user config directory, plus environment overrides for API keys."""
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

APP = "hold-dictate"

HEBREW_PROMPT = "שלום, אני כותב בעברית ומשלב מילים באנגלית כמו pull request, GitHub ו-deploy."

# Cloud providers that speak the OpenAI audio-transcription API.
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3",
        "keys_url": "https://console.groq.com/keys",
        "env": "GROQ_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-transcribe",
        "keys_url": "https://platform.openai.com/api-keys",
        "env": "OPENAI_API_KEY",
    },
    "custom": {
        "label": "Custom (any OpenAI-compatible server)",
        "base_url": "",
        "model": "whisper-1",
        "keys_url": "",
        "env": "HOLD_DICTATE_API_KEY",
    },
}


def config_dir() -> Path:
    override = os.environ.get("HOLD_DICTATE_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / APP


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Config:
    provider: str = "groq"  # groq | openai | custom | local
    api_key: str = ""
    base_url: str = ""  # only for provider = "custom"; overrides the preset otherwise
    model: str = ""  # empty = the provider's default
    language: str = ""  # ISO code such as "en"; empty = auto-detect
    prompt: str = ""  # optional hint that nudges spelling and language mix
    hotkey: str = "alt+space"
    local_model: str = "small"  # faster-whisper model name or Hugging Face repo
    local_fallback: bool = True  # use the local model when the cloud request fails (if installed)
    sounds: bool = True  # short beeps on start, stop and error
    min_hold: float = 0.3  # taps shorter than this (seconds) are ignored
    timeout: float = 15.0  # seconds before a cloud request is given up
    idle_unload_seconds: float = 900.0  # free the local model after this long unused

    def preset(self) -> dict:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])

    def effective_key(self) -> str:
        return self.api_key or os.environ.get(self.preset()["env"], "") or os.environ.get("HOLD_DICTATE_API_KEY", "")

    def effective_base_url(self) -> str:
        return (self.base_url or self.preset()["base_url"]).rstrip("/")

    def effective_model(self) -> str:
        return self.model or self.preset()["model"]


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        known = {f.name: f.type for f in fields(Config)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
    return cfg


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def save(cfg: Config, path: Path | None = None) -> Path:
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# hold-dictate settings. Edit by hand or run `hold-dictate setup`.", ""]
    lines += [f"{k} = {_toml_value(v)}" for k, v in asdict(cfg).items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)  # may hold an API key
    return path
