"""Settings: one TOML file in the per-user config directory, plus environment overrides for API keys."""
import os
import sys
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

APP = "hold-dictate"

HEBREW_PROMPT = "שלום, אני כותב בעברית ומשלב מילים באנגלית כמו pull request, GitHub ו-deploy."

# Languages Whisper knows: code, English name, and a short sentence in the language. The sentences go into
# the transcription prompt, which nudges the model toward the languages you picked.
LANGUAGES = [
    ("en", "English", "Hello, I'm dictating in English."),
    ("he", "Hebrew", "שלום, אני מכתיב בעברית."),
    ("ar", "Arabic", "مرحبا، أنا أتحدث بالعربية."),
    ("ru", "Russian", "Привет, я говорю по-русски."),
    ("es", "Spanish", "Hola, estoy dictando en español."),
    ("fr", "French", "Bonjour, je dicte en français."),
    ("de", "German", "Hallo, ich diktiere auf Deutsch."),
    ("it", "Italian", "Ciao, sto dettando in italiano."),
    ("pt", "Portuguese", "Olá, estou ditando em português."),
    ("nl", "Dutch", "Hallo, ik dicteer in het Nederlands."),
    ("pl", "Polish", "Cześć, dyktuję po polsku."),
    ("uk", "Ukrainian", "Привіт, я диктую українською."),
    ("tr", "Turkish", "Merhaba, Türkçe dikte ediyorum."),
    ("el", "Greek", "Γεια σας, υπαγορεύω στα ελληνικά."),
    ("hi", "Hindi", "नमस्ते, मैं हिंदी में बोल रहा हूँ।"),
    ("zh", "Chinese", "你好，我在用中文听写。"),
    ("ja", "Japanese", "こんにちは、日本語で話しています。"),
    ("ko", "Korean", "안녕하세요, 한국어로 말하고 있어요."),
    ("fa", "Persian", "سلام، من فارسی صحبت می‌کنم."),
    ("ro", "Romanian", "Bună, dictez în română."),
    ("cs", "Czech", "Ahoj, diktuji česky."),
    ("sv", "Swedish", "Hej, jag dikterar på svenska."),
    ("hu", "Hungarian", "Szia, magyarul diktálok."),
    ("vi", "Vietnamese", "Xin chào, tôi đang nói tiếng Việt."),
    ("th", "Thai", "สวัสดี ฉันกำลังพูดภาษาไทย"),
    ("id", "Indonesian", "Halo, saya sedang mendikte dalam bahasa Indonesia."),
    ("da", "Danish", "Hej, jeg dikterer på dansk."),
    ("fi", "Finnish", "Hei, sanelen suomeksi."),
    ("no", "Norwegian", "Hei, jeg dikterer på norsk."),
    ("bg", "Bulgarian", "Здравейте, диктувам на български."),
    ("ca", "Catalan", "Hola, estic dictant en català."),
    ("hr", "Croatian", "Bok, diktiram na hrvatskom."),
    ("sk", "Slovak", "Ahoj, diktujem po slovensky."),
    ("sr", "Serbian", "Здраво, диктирам на српском."),
    ("lt", "Lithuanian", "Labas, diktuoju lietuviškai."),
    ("ur", "Urdu", "ہیلو، میں اردو میں بول رہا ہوں۔"),
    ("bn", "Bengali", "হ্যালো, আমি বাংলায় বলছি।"),
    ("ta", "Tamil", "வணக்கம், நான் தமிழில் பேசுகிறேன்."),
    ("ms", "Malay", "Helo, saya bercakap dalam bahasa Melayu."),
    ("yi", "Yiddish", "שלום, איך רעד אויף ייִדיש."),
]
# The rest of Whisper's languages, without a prompt sentence.
LANGUAGES += [(code, name, "") for code, name in [
    ("af", "Afrikaans"), ("am", "Amharic"), ("hy", "Armenian"), ("as", "Assamese"), ("az", "Azerbaijani"),
    ("ba", "Bashkir"), ("eu", "Basque"), ("be", "Belarusian"), ("bs", "Bosnian"), ("br", "Breton"),
    ("my", "Burmese"), ("et", "Estonian"), ("fo", "Faroese"), ("gl", "Galician"), ("ka", "Georgian"),
    ("gu", "Gujarati"), ("ht", "Haitian Creole"), ("ha", "Hausa"), ("is", "Icelandic"), ("kn", "Kannada"),
    ("kk", "Kazakh"), ("km", "Khmer"), ("lo", "Lao"), ("la", "Latin"), ("lv", "Latvian"), ("ln", "Lingala"),
    ("lb", "Luxembourgish"), ("mk", "Macedonian"), ("mg", "Malagasy"), ("ml", "Malayalam"), ("mt", "Maltese"),
    ("mi", "Maori"), ("mr", "Marathi"), ("mn", "Mongolian"), ("ne", "Nepali"), ("nn", "Nynorsk"),
    ("oc", "Occitan"), ("ps", "Pashto"), ("pa", "Punjabi"), ("sa", "Sanskrit"), ("sn", "Shona"),
    ("sd", "Sindhi"), ("si", "Sinhala"), ("sl", "Slovenian"), ("so", "Somali"), ("su", "Sundanese"),
    ("sw", "Swahili"), ("tl", "Tagalog"), ("tg", "Tajik"), ("tt", "Tatar"), ("bo", "Tibetan"), ("tk", "Turkmen"),
    ("uz", "Uzbek"), ("cy", "Welsh"), ("yo", "Yoruba"),
]]
LANGUAGE_NAMES = {code: name for code, name, _ in LANGUAGES}

# Cloud providers that speak the OpenAI audio-transcription API.
PROVIDERS = {
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "model": "whisper-large-v3",
        "models": [
            ["whisper-large-v3", "Most accurate, best for Hebrew and mixed languages"],
            ["whisper-large-v3-turbo", "Faster and cheaper, a little less accurate"],
        ],
        "keys_url": "https://console.groq.com/keys",
        "env": "GROQ_API_KEY",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o-transcribe",
        "models": [
            ["gpt-4o-transcribe", "Most accurate"],
            ["gpt-4o-mini-transcribe", "Faster and cheaper"],
            ["whisper-1", "The older Whisper model"],
        ],
        "keys_url": "https://platform.openai.com/api-keys",
        "env": "OPENAI_API_KEY",
    },
    "custom": {
        "label": "Custom (any OpenAI-compatible server)",
        "base_url": "",
        "model": "whisper-1",
        "models": [],  # any name the server knows, typed in by hand
        "keys_url": "",
        "env": "HOLD_DICTATE_API_KEY",
    },
}


# Models for provider = "local" (faster-whisper). The first one is what Automatic uses.
LOCAL_MODELS = [
    ["small", "Good balance of speed and accuracy (about 500 MB)"],
    ["large-v3-turbo", "More accurate, slower on most computers (about 1.6 GB)"],
    ["medium", "Accurate, slow (about 1.5 GB)"],
    ["base", "Fast, less accurate (about 150 MB)"],
    ["tiny", "Fastest, least accurate (about 75 MB)"],
]

DUCK_MODES = ("lower", "mute", "off")  # what happens to other apps' sound while you dictate
OVERLAY_STYLES = ("dots", "lines")  # dots: the website's halftone bars; lines: the original thin bars


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
    api_key_service: str = ""  # the provider the key was saved for; a Groq key means nothing to OpenAI
    base_url: str = ""  # only for provider = "custom"; overrides the preset otherwise
    model: str = ""  # empty = Automatic: the provider's recommended model
    languages: str = ""  # the languages you speak, comma-separated codes such as "he,en"; empty = any language
    prompt: str = ""  # optional spelling hint: words and names the transcriber should spell your way
    hotkey: str = "alt+space"
    local_model: str = "small"  # faster-whisper model name or Hugging Face repo; "small" is Automatic
    local_fallback: bool = True  # use the local model when the cloud request fails (if installed)
    beeps: bool = field(default_factory=lambda: not pill_available())  # short sounds on start, stop and error; off where the pill shows it
    background_audio: str = "lower"  # lower | mute | off: other apps' sound while you dictate
    min_hold: float = 0.3  # taps shorter than this (seconds) are ignored
    timeout: float = 15.0  # seconds before a cloud request is given up
    idle_unload_seconds: float = 900.0  # free the local model after this long unused
    auto_update: bool = True  # install new releases from GitHub (checked every 10 minutes), while idle
    overlay_style: str = "dots"  # dots | lines
    overlay_bg: str = "#0a0a0b"  # recording pill background (Linux overlay)
    overlay_bars: str = "#6b80ff"  # recording pill dot colour
    overlay_position: str = "bottom"  # bottom | top
    overlay_size: float = 1.0  # scale of the pill, 0.75 to 1.5

    def preset(self) -> dict:
        return PROVIDERS.get(self.provider, PROVIDERS["custom"])

    def saved_key(self) -> str:
        """The key from the config file, if it was saved for the provider in use."""
        return self.api_key if self.api_key_service in ("", self.provider) else ""

    def effective_key(self) -> str:
        return self.saved_key() or os.environ.get(self.preset()["env"], "") or os.environ.get("HOLD_DICTATE_API_KEY", "")

    def effective_base_url(self) -> str:
        return (self.base_url or self.preset()["base_url"]).rstrip("/")

    def effective_model(self) -> str:
        return self.model or self.preset()["model"]

    def language_codes(self) -> list[str]:
        return [c for c in (part.strip().lower() for part in self.languages.split(",")) if c]


def pill_available() -> bool:
    """True where the recording pill runs (Linux on Wayland, with quickshell). It shows what beeps would."""
    return sys.platform.startswith("linux") and bool(os.environ.get("WAYLAND_DISPLAY")) and bool(shutil.which("quickshell"))


def load(path: Path | None = None) -> Config:
    path = path or config_path()
    cfg = Config()
    if path.exists():
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        known = {f.name: f.type for f in fields(Config)}
        for key, value in data.items():
            if key in known:
                setattr(cfg, key, value)
        if "languages" not in data:
            migrate_language(cfg, data)
        if "sounds" in data and "beeps" not in data:
            # Before 0.5.2 `sounds` never played while the recording pill was showing, so it starts off there.
            cfg.beeps = bool(data["sounds"]) and not pill_available()
        if cfg.api_key and "api_key_service" not in data:
            cfg.api_key_service = cfg.provider  # before 0.5 the key always belonged to the saved provider
    return cfg


def migrate_language(cfg: Config, data: dict):
    """Before 0.5 there was one `language`, and Hebrew was stored as auto-detect plus a Hebrew prompt."""
    if cfg.prompt == HEBREW_PROMPT:
        cfg.prompt, cfg.languages = "", "he,en"
    elif data.get("language"):
        cfg.languages = str(data["language"])


def clean_languages(value) -> str:
    """'he, EN,he' or ['he', 'en'] -> 'he,en'. Raises ValueError for a code Whisper does not know."""
    parts = value if isinstance(value, list) else str(value).split(",")
    codes = []
    for part in parts:
        code = str(part).strip().lower()
        if not code:
            continue
        if code not in LANGUAGE_NAMES:
            raise ValueError(f"unknown language {code}")
        if code not in codes:
            codes.append(code)
    return ",".join(codes)


def coerce(name: str, value):
    """Converts a value from the settings page to the type of the Config field, or raises ValueError."""
    if name == "languages":
        return clean_languages(value)
    default = getattr(Config(), name)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be true or false")
        return value
    if isinstance(default, float):
        return float(value)
    return str(value).strip()


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
    lines = ["# hold-dictate settings. Edit by hand, or run `hold-dictate settings`.", ""]
    lines += [f"{k} = {_toml_value(v)}" for k, v in asdict(cfg).items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if sys.platform != "win32":
        path.chmod(0o600)  # may hold an API key
    return path
