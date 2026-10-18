"""Client configuration: defaults, config.json load/save, first-run helpers."""

import copy
import json
import sys
from pathlib import Path

# Frozen exe (PyInstaller onefile): keep config next to the exe, not the
# temp extraction dir.
APP_DIR = (Path(sys.executable).resolve().parent
           if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
CONFIG_PATH = APP_DIR / "config.json"

# Friendly names kept in sync with the server's translator.py choices.
TARGET_CHOICES = [
    ("eng_Latn", "English"),
    ("kor_Hang", "한국어 (Korean)"),
    ("vie_Latn", "Tiếng Việt (Vietnamese)"),
]

DEFAULTS = {
    # filled in by first-run setup (or --setup): wss://subs.example.com/ws
    # plus the access token and (optionally) the Cloudflare Access service
    # token when the server is locked down with Zero Trust. admin_token is
    # only for the server owner's own client: it unlocks the mode/language
    # hotkeys (Ctrl+Alt+G / Ctrl+Alt+L); friends' clients don't have it.
    "server": {"url": "", "token": "", "cf_client_id": "", "cf_client_secret": "",
               "admin_token": ""},
    "target_lang": "eng_Latn",
    # Ctrl+Alt+L cycles the server's pinned language through these
    # (whisper codes; "auto" = per-utterance detection)
    "language_cycle": ["auto", "ko", "ja", "vi"],
    "overlay": {
        "max_lines": 3,
        "max_height_frac": 0.18,        # never cover more than this fraction of the screen height
        "anchor": "screen",             # "screen" = bottom of active monitor | "window" = bottom of focused window
        "width_frac": 0.62,             # fraction of the active window's width
        "margin": 28,                   # gap above the window's bottom edge
        "font_size": 15,
        "bg_alpha": 175,                # 0-255 background opacity
        "timeout_s": 9,                 # hide a line after this many seconds
        "show_original": True,          # also show the dim original-language line
        "show_speakers": True,          # prefix each line with the speaker's name
        "hotkeys": {
            "pause": "ctrl+alt+p",
            "toggle": "ctrl+alt+o",
            "interact": "ctrl+alt+i",   # toggle click-through (to reach the tray menu)
            "mode": "ctrl+alt+g",       # toggle chat/game on the server
            "language": "ctrl+alt+l",   # cycle the server's pinned language
            "quit": "ctrl+alt+q",
        },
    },
}


def _deep_update(base: dict, extra: dict) -> dict:
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_update(base[k], v)
        else:
            base[k] = v
    return base


def load_config(path: Path = CONFIG_PATH) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                _deep_update(cfg, user)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] Warning: could not read {path} ({e}); using defaults")
    return cfg


def save_config(cfg: dict, path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False),
                    encoding="utf-8")


def parse_setup_code(code: str) -> dict:
    """Accepts either setup-code shape:
         wss://host/ws#TOKEN                        (app token only)
         wss://host/ws#t=TOKEN&cid=ID&csec=SECRET   (with Cloudflare Access)
    Returns {"url", "token", "cf_client_id", "cf_client_secret"} — anything
    absent in the code is left empty."""
    code = code.strip()
    if not code.startswith(("ws://", "wss://")):
        raise ValueError("That doesn't look like a setup code — it should "
                         "start with wss:// (or ws://).")
    url, _, frag = code.partition("#")
    out = {"url": url, "token": "", "cf_client_id": "", "cf_client_secret": ""}
    if not frag:
        return out
    if "=" in frag:  # key=value form: t=...&cid=...&csec=...
        for part in frag.split("&"):
            k, _, v = part.partition("=")
            k = k.strip().lower()
            if k == "t":
                out["token"] = v.strip()
            elif k == "cid":
                out["cf_client_id"] = v.strip()
            elif k == "csec":
                out["cf_client_secret"] = v.strip()
    else:  # legacy plain token
        out["token"] = frag
    return out
