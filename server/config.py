"""Server configuration: defaults, config.json load/save, live profile switch."""

import copy
import json
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.json"

DEFAULTS = {
    # --- audio source ---
    # "loopback": capture a normal Discord client's output via VB-Cable on
    #   this PC (works with Discord's DAVE end-to-end encryption; no bot; the
    #   whole channel arrives as ONE mixed stream, so no per-speaker labels).
    # "bot": a py-cord bot joins the channel and receives per-user audio.
    #   Broken while py-cord's DAVE voice receive is unfinished (#3139) —
    #   kept for when that ships.
    "source": "loopback",
    "audio_device": "CABLE Output",  # recording device substring (loopback mode)
    "capture_sample_rate": 0,        # 0 = use the device's default rate
    "speaker_name": "Discord",       # label for the mixed loopback stream
    # --- discord web auto-join (loopback mode) ---
    # Drives discord.com in a Playwright Chrome so the loopback always has
    # audio without anyone RDP-ing in. Log in once with --setup.
    # ToS caveat: automating a user account breaks Discord's ToS — use a
    # dedicated burner account, not your main.
    "discord_web": {
        "guild": "",          # server name as shown in the guild sidebar ("" = whatever is open)
        "channel": "Lobby",   # voice channel name to join
        "auto_join": True,    # launch the joiner with the server
    },
    # --- discord bot (source = "bot" only) ---
    "discord": {
        "token": "",          # bot token from the Developer Portal (setup.bat sets this)
        "guild_id": None,     # optional: your server's ID -> slash commands appear instantly
    },
    # --- subtitle server ---
    "server": {
        "host": "0.0.0.0",    # bind address (cloudflared connects from this machine)
        "port": 8765,
        "access_token": "",   # shared secret; empty = no auth (LAN testing only)
        # When set, only clients presenting this token may change the mode or
        # pinned language (the owner's client). Empty = anyone may.
        "admin_token": "",
        # Public wss:// URL friends connect to, e.g. "subs.example.com".
        # Used by setup.bat to print the one-line client setup code.
        "public_host": "",
    },
    # --- cloudflare zero trust (optional, strongly recommended for tunnels) ---
    # The server itself NEVER checks these — Access blocks unauthenticated
    # traffic at Cloudflare's edge before it reaches this machine. They are
    # stored only so setup.bat can (re)print the client setup code.
    "cloudflare": {
        "client_id": "",      # CF-Access-Client-Id
        "client_secret": "",  # CF-Access-Client-Secret (shown once at creation)
    },
    # --- speech-to-text ---
    # "asr_engine": "groq" = cloud Whisper large-v3-turbo via Groq (free tier,
    #   ~20 requests/minute — fits the mixed-stream finals; partial subtitles
    #   are skipped to stay under the gate). Local whisper loads lazily only
    #   if Groq fails, so the GPU VRAM stays free. Needs groq.key.
    # "asr_engine": "local" = faster-whisper on this machine (default).
    "asr_engine": "local",
    "groq": {
        "key": "",            # console.groq.com -> API Keys
        "model": "whisper-large-v3-turbo",
        "skip_partials": True,  # true = no partial subtitles on Groq (saves requests)
    },
    # --- Azure Speech-to-Text fallback ---
    # When set, Azure Speech catches Groq failures/quota (free tier 20 RPM)
    # before the pipeline falls back to local whisper. Create a "Speech"
    # resource in portal.azure.com (student subscription works) and paste
    # its key + region. Needs the utterance language to be known.
    "azure_speech": {
        "key": "",
        "region": "",   # e.g. "koreacentral" — the Speech resource's region
    },
    # Optional SECOND (paid/S1) Speech resource: used only when the free one
    # is exhausted — billed against your student credits, keeps quality
    # identical instead of dropping to local whisper.
    "azure_speech_paid": {
        "key": "",
        "region": "",
    },
    # Optional paid/S1 Azure Translator resource: catches the free
    # Translator's 2M chars/month cap (falls back to local NLLB without it).
    "azure_translator_paid": {
        "key": "",
        "region": "",
    },
    # --- models ---
    "whisper_model": "large-v3-turbo",  # faster-whisper model name or local path
    # On a 4 GB GPU (RTX 3050 mobile) running hot, fall back to "small".
    "language": None,                   # pin Whisper language for ALL speakers; null = per-speaker voting
    "debug_save_audio": False,          # dump each utterance to debug_audio/ (level/language checks)
    "device": "auto",                   # auto | cuda | cpu
    "compute_type": "auto",             # auto | float16 | int8_float16 | int8_float32 | int8
    "cpu_threads": 4,
    "beam_size": 1,                     # greedy decode; 2 buys little quality and costs latency
    # --- modes: chat = accurate conversation, game = fast short callouts ---
    "profile": "chat",                  # "chat" | "game"; switch live with /mode
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
    # "engine": "azure" = Azure Translator (cloud, 2M chars/month free on F0,
    #   needs azure.key) with automatic fallback to local NLLB on any failure.
    # "engine": "nllb" = local NLLB only (offline, unlimited).
    "engine": "nllb",
    "azure": {
        "key": "",     # Translator resource key (portal.azure.com)
        "region": "",  # e.g. "koreacentral" — the resource's region
        "endpoint": "api.cognitive.microsofttranslator.com",
    },
    # Each viewer picks their own target; this is used when nobody is connected
    # (keeps the pipeline exercised) and as the /health default.
    "default_target_lang": "eng_Latn",  # NLLB language code
    "nllb_ct2_repo": "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
    "nllb_tokenizer_repo": "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
    # --- output filtering ---
    "min_text_len": 2,                  # drop transcriptions shorter than this
    # Backlog guard: when the transcription queue runs deeper than backlog_max
    # (cloud fallback loading or lagging), older utterances are dropped and
    # only the newest backlog_keep are processed — stale subtitles are useless
    "backlog_max": 12,
    "backlog_keep": 3,
    # Domain vocabulary fed to Whisper as a hint — measurably fixes gaming
    # loanword errors (탄약/포지션/ヒール/リロード...). Applied when the language
    # is pinned or vote-locked; skipped on detection audits to stay unbiased.
    "initial_prompts": {
        "ko": ("게임 음성 채팅. 오버워치: 아나, 키리코, 라인하르트, 바스티온, 레퍼, 윈스턴, 디바, 겐지, "
               "트레이서, 메이, 루시우, 젠야타, 궁극기, 탱커, 힐러, 딜러, 탄약, 리로드, 한타, "
               "오브젝트, 포지션, 돌진, 후퇴, 수비, 공격."),
        "ja": ("ゲームボイスチャット。オーバーウォッチ: アナ、キリコ、ラインハルト、バスティオン、リーパー、"
               "ウィンストン、D.Va、ゲンジ、トレーサー、ルシオ、ゼニヤッタ、アルティメット、タンク、ヒーラー、"
               "弾薬、リロード、味方、敵、高台、前線、撤退。"),
    },
    # Deterministic callout table — beats NLLB on gaming slang (빨피/궁/뒤에서...)
    # and costs zero latency. Fires only when the WHOLE phrase is covered and
    # the target is English; everything else goes to the neural translator.
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
    so a live /mode switch reaches the running threads through the same dict)."""
    prof = cfg.get("profiles", {}).get(cfg.get("profile", "chat"), {})
    if "beam_size" in prof:
        cfg["beam_size"] = prof["beam_size"]
    _deep_update(cfg["vad"], prof.get("vad", {}))
    return cfg


def persist_key(key: str, value) -> None:
    """Best-effort: save one setting to config.json so it survives restarts."""
    try:
        path = CONFIG_PATH
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data[key] = value
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"[config] Could not persist {key}: {e}")


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
        path.write_text(json.dumps(DEFAULTS, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        print(f"[config] Created default config at {path}")
    if overrides:
        _deep_update(cfg, {k: v for k, v in overrides.items() if v is not None})
    return cfg
