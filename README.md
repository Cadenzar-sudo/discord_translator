# Discord Voice Translator Overlay

Listens to your friends' voices in Discord (Korean, Japanese, or any Whisper-supported
language), transcribes them with **faster-whisper**, translates to English with
**NLLB-200** (or Whisper's built-in translate mode), and shows rolling subtitles in a
**click-through overlay** that follows whichever window you're using.

Everything runs locally on your GPU. No cloud, no mic audio leaves your PC.

```
Discord → VB-Cable → capture → Silero VAD → faster-whisper → NLLB → overlay
```

## Requirements

- Windows 10/11
- NVIDIA GPU with 8+ GB VRAM recommended (CPU works too, automatically)
- Python 3.10–3.14
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (free)

## Install on another PC

The app is portable — no Docker needed. To set it up on another Windows machine:

1. **Copy the project folder** to the new PC (USB stick, network share, zip).
   - Copy `models\` too (~2.9 GB) if you want to skip the first-run model
     download; otherwise leave it out and models re-download automatically.
   - Don't copy `venv\` or `config.json` — they are per-machine.
2. **Install Python 3.10+** on that PC if it's missing
   (`winget install Python.Python.3.12`, or python.org — tick *Add to PATH*).
3. **Double-click `install.bat`** — creates the venv and installs everything.
4. **Install VB-Cable there too** (step 2 of the setup above), then run
   **`start.bat`**.

Optional per-machine tweaks go in `config.json` (auto-created on first run):
language pin (`"language": "ko"`), device name if the cable is named differently,
hotkeys, overlay size.

## One-time setup

### 1. Install the app

```bat
py -m venv venv
venv\Scripts\pip install -r requirements.txt
```

### 2. Install VB-Cable

Download and run the installer from <https://vb-audio.com/Cable/> (run as admin),
then reboot.

### 3. Route Discord into the cable

1. **Discord → Settings → Voice & Video → Output Device** → set to
   **CABLE Input (VB-Audio Virtual Cable)**.
   Your friends' voices now go into the cable *instead of* your headphones.
2. So you can still hear them: **Windows Settings → Sound → More sound settings
   → Recording tab** → **CABLE Output** → **Properties → Listen tab** → check
   **"Listen to this device"** → **Playback through this device** → your
   headphones/speakers → OK.

Now Discord's audio flows into the cable, and Windows echoes it back to your
headphones. The app records the cable directly.

### 4. Run it

```bat
venv\Scripts\python main.py
```

First run downloads models (~4 GB total: Whisper `large-v3-turbo`, NLLB-200 1.3B,
tokenizer) — after that it starts in a few seconds. A tray icon and the
subtitle overlay appear. Speak in Discord; translations show above the bottom edge
of whatever window is focused.

## Hotkeys

| Key | Action |
|---|---|
| `Ctrl+Alt+G` | Switch **chat mode** (accurate) ↔ **game mode** (fast callouts) |
| `Ctrl+Alt+L` | Cycle language pin (e.g. Korean) ↔ auto-detect — for when a non-Korean friend joins |
| `Ctrl+Alt+P` | Pause / resume translating |
| `Ctrl+Alt+O` | Show / hide the overlay |
| `Ctrl+Alt+I` | Interaction mode (lets you click the overlay; off = click-through) |
| `Ctrl+Alt+Q` | Quit |

## Modes

- **Chat mode** (default): full accuracy — beam search, streaming partials every
  2s, sentences finalized ~1–1.5s after they end. For conversation.
- **Game mode** (Ctrl+Alt+G, or the tray menu): tuned for short callouts —
  320ms silence close, partials every 1s, greedy decode, plus a **callout
  glossary** that deterministically translates gaming slang NLLB gets wrong
  (트레이서 빨피 → "Tracer is low", 궁 준비됐어 → "ult is ready",
  힐러 하나 남았어 → "one healer left"). Callouts land **~0.9s** after they're
  said. Extend `callout_glossary` in config.json with your group's phrases —
  it only fires when the whole phrase matches, everything else still goes
  through the neural translator.

## Useful commands

```bat
:: List recording devices (find the exact CABLE name)
venv\Scripts\python main.py --list-devices

:: Test the full pipeline on an audio/video file, no capture needed
venv\Scripts\python main.py --test-file sample.mp3

:: Console-only mode (no overlay)
venv\Scripts\python main.py --no-overlay

:: Test overlay visuals with fake lines
venv\Scripts\python overlay.py --demo

:: Lower latency / less VRAM: Whisper does the translation directly
venv\Scripts\python main.py --engine whisper
```

## Configuration (`config.json`, created on first run)

| Key | Default | Notes |
|---|---|---|
| `audio_device` | `"CABLE Output"` | substring of a recording device name |
| `engine` | `"nllb"` | `nllb` = better quality; `whisper` = lowest latency, English only |
| `whisper_model` | `"large-v3-turbo"` | `small` / `medium` use less VRAM. **Caveat:** turbo is poor at `engine=whisper` (its translate task is broken upstream) — with `engine=whisper` use `large-v3` or `small` |
| `device` / `compute_type` | `auto` | auto-detects CUDA + best precision (GTX 10xx → `int8_float32`; RTX → `float16`) |
| `nllb_ct2_repo` / `nllb_tokenizer_repo` | `...1.3B-ct2-int8` | swap to `JustFrederik/nllb-200-distilled-600M-ct2-int8` for a smaller/faster model |
| `target_lang` | `"eng_Latn"` | any NLLB-200 code, e.g. `deu_Latn` (engine=`nllb` only) |
| `vad.*` | — | utterance segmentation tuning |
| `overlay.*` | — | size, timeout, hotkeys. `anchor` (`"screen"` bottom of active monitor, or `"window"` bottom of the focused window), `max_lines` (3), `max_height_frac` (0.18) cap screen coverage |

VRAM budget: `large-v3-turbo` ≈ 1.5 GB + NLLB 1.3B int8 ≈ 1.5 GB — both fit in an
8 GB card alongside a game. If you need more headroom, set
`"whisper_model": "small"` in config.json.

## Troubleshooting

- **"No input device matching CABLE Output"** — install VB-Cable (step 2) or run
  `--list-devices` and put the right name in `config.json`.
- **No translations but no errors** — check Discord's *Output Device* is
  **CABLE Input**, and that "Listen to this device" is on for **CABLE Output**.
- **DLL / CUDA errors at startup** — make sure `nvidia-cublas-cu12` and
  `nvidia-cudnn-cu12` installed (they're in requirements.txt), or install
  [CUDA Toolkit 12.x](https://developer.nvidia.com/cuda-downloads) +
  [cuDNN 9](https://developer.nvidia.com/cudnn) and add their `bin` folders to `PATH`.
- **Overlay hidden in a game** — exclusive-fullscreen games (Overwatch's
  "Fullscreen" display mode) bypass Windows composition: no overlay of any
  kind can draw over them. Set the game to *Borderless Windowed /
  Fullscreen Windowed* (negligible FPS cost) and the overlay appears.
- **Slow translations while gaming** — Whisper shares the GPU with the game and
  decode times can triple mid-match. Use game mode (`Ctrl+Alt+G`); if still
  slow, try `"whisper_model": "medium"` in config.json.
- **Wrong language detected** — Whisper detects per utterance; short phrases can
  misdetect. Say a full sentence and it locks on.
- **One stream, not per-person** — everyone in a voice channel arrives mixed
  together; there's no speaker separation in v1.

## Notes

- **Accuracy tuning**: `initial_prompts` in config.json feeds Whisper your
  group's vocabulary (hero names, gaming terms — **add your friends'
  nicknames** and the games you play). Measured effect: fixes loanword
  mis-hears (탄약→pills, 포지션→vision, ヒール→help) and improves noisy-speech
  transcription by ~6 chrF. It only applies once the language is locked, so
  detection stays unbiased.
- **Where errors still come from**: (1) Whisper mis-hears slang/fast speech —
  pin `"language"`, keep `initial_prompts` updated, and have friends enable
  Discord's *Noise Suppression* so game audio doesn't bleed into their mics;
  (2) NLLB translates literally and can mangle idioms. A bigger translator
  (NLLB-3.3B CT2) fits your GPU but needs a ~6.6 GB download — swap
  `nllb_ct2_repo` in config.json if you want it. Beam search and the `small`
  Whisper model were benchmarked and do **not** help accuracy.
- **Streaming subtitles**: while someone is still talking, a provisional
  translation (dimmed, with a trailing `…`) appears ~2s after they *start*
  speaking and refreshes every ~2s (`vad.partial_interval_s`). When they pause
  or hit the 9s cap, the line is finalized and polished in place. You read
  along as they talk instead of waiting for the sentence to end.
- **Latency**: finals land ~1–1.5s after a sentence ends. To trim further:
  pin the language (`"language": "ko"` — skips Whisper's detection pass), or
  lower `vad.partial_interval_s` / `vad.min_silence_ms` (choppier but faster).
- Whisper's `task=translate` only targets English; use the NLLB engine for other
  target languages.
- NLLB language codes map from Whisper's detection automatically (ja→jpn_Jpan,
  ko→kor_Hang, zh→zho_Hans, …; see `translator.py`).
- Long unpunctuated run-ons (Whisper often omits punctuation in Japanese) can get
  compressed by NLLB to the leading sentence — sentences with punctuation
  translate fully.
