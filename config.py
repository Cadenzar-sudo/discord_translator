"""Configuration: defaults, config.json load/save, CLI override merge."""

import json
import copy
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"
MODEL_DIR = APP_DIR / "models"

DEFAULTS = {
    # --- audio ---
    # Substring matched (case-insensitive) against recording devices.
    # With VB-Cable, Discord's output lands on the "CABLE Output" recording device.
    "audio_device": "CABLE Output",
    "capture_sample_rate": 0,  # 0 = use the device's default rate (VB-Cable: 48000)
    # --- models ---
    "whisper_model": "large-v3-turbo",  # faster-whisper model name or local path
    "language": None,                   # pin Whisper language, e.g. "ko"; null = auto-detect with voting
    "device": "auto",                   # auto | cuda | cpu
    "compute_type": "auto",             # auto | float16 | int8_float16 | int8
    "cpu_threads": 4,
    "beam_size": 1,                     # greedy decode; 2 buys little quality and costs latency
    # --- modes: chat = accurate conversation, game = fast short callouts ---
    "profile": "chat",                  # "chat" | "game"; switch live with Ctrl+Alt+G
    "profiles": {
        "chat": {
            "beam_size": 2,
            "vad": {"min_silence_ms": 480, "partial_interval_s": 2.0,
                    "partial_min_s": 1.2, "max_utterance_s": 9.0},
        },
        "game": {
            "beam_size": 1,
            "vad": {"min_silence_ms": 320, "partial_interval_s": 1.0,
                    "partial_min_s": 0.8, "max_utterance_s": 6.0},
            # extra callout vocabulary appended to initial_prompts in game mode
            "prompt_add": {
                "ko": ("짧은 콜아웃: 빨피, 노피, 원힐, 힐 없음, 빠져, 뒤에, 뒤에서, 옆에서, 궁, 궁 준비됐어, 궁 써, "
                       "라인, 백라인, 밀어, 집결, 나노, 리퍼 뒤, 트레이서 뒤, 적 하나 남았어."),
                "ja": ("短いコールアウト: 残り一人、フラット、裏取り、後ろから、横から、アルティメット準備完了、"
                       "ライン、下がって、押して、集合、敵一人残り。"),
            },
        },
    },
    # --- translation ---
    "engine": "nllb",                   # "nllb" (better quality) | "whisper" (lowest latency)
    "target_lang": "eng_Latn",          # NLLB language code (engine=whisper is English-only)
    "nllb_ct2_repo": "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
    "nllb_tokenizer_repo": "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
    # --- output filtering ---
    "min_text_len": 2,                  # drop transcriptions shorter than this
    # Domain vocabulary fed to Whisper as a hint — measurably fixes gaming
    # loanword errors (탄약/포지션/ヒール/リロード...). Applied when the language
    # is pinned or vote-locked; skipped on detection audits to stay unbiased.
    # Add your friends' nicknames and the games you play.
    "initial_prompts": {
        "ko": ("게임 음성 채팅. 오버워치: 아나, 키리코, 라인하르트, 바스티온, 레퍼, 윈스턴, 디바, 겐지, "
               "트레이서, 메이, 루시우, 젠야타, 궁극기, 탱커, 힐러, 딜러, 탄약, 리로드, 한타, "
               "오브젝트, 포지션, 돌진, 후퇴, 수비, 공격."),
        "ja": ("ゲームボイスチャット。オーバーウォッチ: アナ、キリコ、ラインハルト、バスティオン、リーパー、"
               "ウィンストン、D.Va、ゲンジ、トレーサー、ルシオ、ゼニヤッタ、アルティメット、タンク、ヒーラー、"
               "弾薬、リロード、味方、敵、高台、前線、撤退。"),
    },
    # Deterministic callout table — beats NLLB on gaming slang (빨피/궁/뒤에서...)
    # and costs zero latency. Fires only when the WHOLE phrase is covered;
    # everything else goes to the neural translator. Extend with your group's
    # vocabulary. Applies in both modes.
    "callout_glossary": {
        "ko": {
            "names": {
                "트레이서": "Tracer", "리퍼": "Reaper", "레퍼": "Reaper", "겐지": "Genji",
                "윈스턴": "Winston", "디바": "D.Va", "둠피스트": "Doomfist", "라인하르트": "Reinhardt",
                "자리야": "Zarya", "오리사": "Orisa", "아나": "Ana", "키리코": "Kiriko",
                "루시우": "Lucio", "젠야타": "Zenyatta", "메르시": "Mercy", "모이라": "Moira",
                "바스티온": "Bastion", "메이": "Mei", "솔저": "Soldier", "애쉬": "Ashe",
                "위도우": "Widow", "한조": "Hanzo", "정크랫": "Junkrat", "솜브라": "Sombra",
                "시그마": "Sigma", "라마트라": "Ramatra", "정커퀸": "Junker Queen",
            },
            "patterns": [
                [r"(\w+)\s*빨피[야이이다가요을]?.?", r"\1 is low"],
                [r"궁\s*준비\s*됐[어습니다]?.?", "ult is ready"],
                [r"궁\s*써[\.?!]?", "popping ult"],
                [r"힐러?\s*하나\s*남았[어다]?.?", "one healer left"],
                [r"적?\s*하나\s*남았[어다]?.?", "one enemy left"],
                [r"뒤에서?\s*(?:오|와|있)[어느는데]?.?", "enemy behind us"],
            ],
        },
        "ja": {
            "names": {
                "トレーサー": "Tracer", "リーパー": "Reaper", "ゲンジ": "Genji",
                "ウィンストン": "Winston", "D.Va": "D.Va", "ラインハルト": "Reinhardt",
                "アナ": "Ana", "キリコ": "Kiriko", "ルシオ": "Lucio", "ゼニヤッタ": "Zenyatta",
                "バスティオン": "Bastion", "マーシー": "Mercy",
            },
            "patterns": [
                [r"(\w+)\s*フラット[ですだ]?.?", r"\1 is low"],
                [r"アルティメット\s*準備\s*完了[ですだ]?.?", "ult is ready"],
                [r"残り\s*一人[ですだ]?.?", "one enemy left"],
                [r"後ろから\s*(?:来|い)[るます].?", "enemy behind us"],
            ],
        },
    },
    "vad": {
        "on_threshold": 0.5,            # speech prob to start an utterance
        "off_threshold": 0.35,          # speech prob counting as silence
        "min_speech_ms": 250,           # ignore blips shorter than this
        "min_silence_ms": 480,          # close the utterance after this much silence (latency)
        "max_utterance_s": 9.0,         # hard-cap length (emits mid-speech when hit)
        "partial_interval_s": 2.0,      # while speech continues, publish a partial this often
        "partial_min_s": 1.2,           # first partial only after this much speech
        "padding_ms": 120,              # audio padding included on both sides
    },
    # --- overlay ---
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
        "hotkeys": {
            "pause": "ctrl+alt+p",
            "toggle": "ctrl+alt+o",
            "interact": "ctrl+alt+i",   # toggle click-through so the tray/menu is reachable
            "mode": "ctrl+alt+g",       # switch chat <-> game mode
            "language": "ctrl+alt+l",   # cycle pinned language <-> auto-detect
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


def apply_profile(cfg: dict) -> dict:
    """Fold the active profile's beam/vad/prompt settings into cfg (in place,
    so a live mode switch reaches the running threads through the same dict)."""
    prof = cfg.get("profiles", {}).get(cfg.get("profile", "chat"), {})
    if "beam_size" in prof:
        cfg["beam_size"] = prof["beam_size"]
    _deep_update(cfg["vad"], prof.get("vad", {}))
    return cfg


def persist_key(key: str, value) -> None:
    """Best-effort: save one setting to config.json so it survives restarts."""
    try:
        import json
        path = CONFIG_PATH
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[key] = value
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"[config] Could not persist {key}: {e}")


def create_default_config(path: Path = CONFIG_PATH) -> None:
    path.write_text(json.dumps(DEFAULTS, indent=2), encoding="utf-8")


def load_config(overrides: dict | None = None, path: Path = CONFIG_PATH) -> dict:
    """Load defaults, overlay config.json, then apply CLI overrides."""
    cfg = copy.deepcopy(DEFAULTS)
    if path.exists():
        try:
            user = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(user, dict):
                _deep_update(cfg, user)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[config] Warning: could not read {path} ({e}); using defaults")
    else:
        create_default_config(path)
        print(f"[config] Created default config at {path}")
    if overrides:
        _deep_update(cfg, {k: v for k, v in overrides.items() if v is not None})
    return cfg
