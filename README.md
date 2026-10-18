# Discord Bot Translator

Real-time voice-translation subtitles for your whole Discord voice channel,
split across two machines so nobody's gaming GPU pays for it:

```
Discord voice channel (everyone speaks normally)
        │  per-user audio streams
        ▼
┌─────────────────────────────────────────────────────┐
│  SERVER  (any Windows PC with an NVIDIA GPU)        │
│  Discord bot joins the voice channel (/join),       │
│  transcribes each speaker (faster-whisper),         │
│  translates per viewer (NLLB-200),                  │
│  pushes subtitles over a token-authed WebSocket     │
└─────────────────────────────────────────────────────┘
        │  wss:// via Cloudflare Tunnel (or LAN)
        ▼
┌─────────────────────────────────────────────────────┐
│  CLIENT  (each player's PC — no GPU, no Python*)    │
│  the same click-through bottom overlay as before,   │
│  with speaker names. Each viewer picks their        │
│  language: Korean friends get EN→KO, you get KO→EN. │
└─────────────────────────────────────────────────────┘
   * friends just run a single TranslatorOverlay.exe
```

Everyone in the channel must be aware their voice is being transcribed —
that's what the client consent note is for. Keep it to your own friend group.

---

## Two versions (Dockerized) — pick with one double-click

Both versions serve the SAME overlay clients (nothing changes for friends).

| | **Version B — Discord bot (primary)** | **Version A — VB-Cable loopback (fallback)** |
|---|---|---|
| Audio source | the `voice-bot` sidecar container joins voice with **DAVE E2EE** and ships **per-speaker** audio (real display names, per-speaker VAD/voting) | Playwright Chrome joins as a user; audio loops through VB-Cable as one mixed stream labelled "Discord" |
| Runs where | **everything in Docker** (server + sidecar) — portable to any PC/VPS | server container + native `audio_shipper.py` + web joiner on this Windows PC |
| Start | `start_bot.bat` / `stop_bot.bat` | `start_loopback.bat` / `stop_loopback.bat` |
| Needs | bot token in the config (same setup as ever) | VB-Cable + the saved web session (`server\discord_web_profile`) |

If the bot path ever breaks upstream (DAVE changes, bot kicked), stop it and
run Version A — that's the battle-tested loopback stack, kept as the final
fallback.

### One-time Docker setup

1. `docker-data\config.json` — copy of `server\config.json` (holds your
   tokens, stays out of git) with two changes:
   ```json
   "source": "ws",
   "audio_shipper": { "url": "ws://server:8765/audio" }
   ```
   Bot token, guild/channel IDs and access/admin tokens carry over
   unchanged — `setup.bat` remains the only token setup you ever do.
   The sidecar also reads `discord_web.channel_id` + `auto_join`
   (boot-time join + 15 s watchdog) and `server.access_token`.
2. Models: if `server\models` is missing, `start_bot.bat` reuses
   `D:\discord-translator-bot\server\models` automatically; on a fresh
   machine the container downloads ~3.3 GB from HuggingFace on first start.
3. `start_bot.bat` — builds + starts both containers, waits for
   <http://localhost:8765/health>. Bot health: <http://localhost:8766/>.
   The bot auto-joins the configured voice channel; `/join` also works.
4. GPU (optional): `docker compose -f compose.yml -f compose.gpu.yml up -d --build`
   with WSL2 + an NVIDIA driver. CPU mode is fine day-to-day — Groq and
   Azure do the heavy lifting.

**Permissions — nothing new required:** the same invite as the old py-cord
bot works (scopes `bot` + `applications.commands`, perms `View Channel` +
`Connect`; the console prints the URL on login). One rule: the bot must
never be deafened — a deafened bot receives no audio (it won't self-deafen).

### Moving it to another PC (e.g. the RTX 3050 4 GB box)

Everything ships with the repo; the machine needs Docker Desktop (+ WSL2 and
an NVIDIA driver for the GPU variant — or nothing special for CPU).

1. Copy the repo over; copy `server\models` (3.3 GB) with it, or let the
   first start download it from HuggingFace.
2. Build `docker-data\config.json` as above, plus for a GPU box:
   ```json
   "asr_engine": "local_first"
   ```
   — local faster-whisper becomes the PRIMARY (loaded at startup, zero
   quota, partial subtitles stream), and Groq/Azure Speech only catch
   failures. `device/compute_type` stay `"auto"`. On 4 GB VRAM keep
   `"whisper_model": "large-v3-turbo"` if the box only translates; drop to
   `"small"` if it games at the same time.
3. Start with the GPU variant:
   `docker compose -f compose.yml -f compose.gpu.yml up -d --build`
4. Viewers reach the server at `ws://<new-pc-ip>:8765/ws` on LAN; over the
   internet, install `cloudflared` on that box and point the existing
   tunnel at its port 8765 (friends' overlay config doesn't change).
5. One bot token = one voice connection: don't run two stacks with the
   same token at once.

---

## Native setup (legacy — the original non-Docker flow)

**Requirements:** Windows, Python 3.10+, NVIDIA GPU (4 GB VRAM minimum),
~5 GB disk. `large-v3-turbo` + NLLB-1.3B fit a 4 GB card **if the PC isn't
gaming at the same time**; if VRAM is tight set `"whisper_model": "small"`
in `server/config.json`.

1. **`server\install.bat`** — venv + dependencies + models (~3.4 GB).
   If the old desktop app (`D:\Translation Discord`) is on the machine it
   offers to copy its `models\` folder instead of downloading.
2. **`server\setup.bat`** — pastes in the bot token, generates the access
   token, prints the one-line setup code for friends.
3. **Create the Discord bot** (one time):
   - <https://discord.com/developers/applications> → New Application.
   - Bot tab → Reset Token → copy (goes into setup.bat).
   - OAuth2 → URL Generator → scopes `bot` + `applications.commands`,
     perms `View Channels` + `Connect` → open the URL → pick your server.
     (The server console also prints a ready-made invite link on startup.)
4. **`server\start.bat`** — bot online. In Discord: `/join` while in a voice
   channel. Done.

### Exposing it to friends over the internet (Cloudflare Tunnel)

You have a Cloudflare account + domain. On the laptop:

```bat
winget install Cloudflare.cloudflared
cloudflared tunnel login
cloudflared tunnel create translator
cloudflared tunnel route dns translator subs.yourdomain.com
cloudflared tunnel run --url http://localhost:8765 translator
```

Run it once interactively; when it works, install it as a service so it
survives reboots: write a `config.yml` (see `cloudflared tunnel --help`)
then `cloudflared service install`. Friends' setup code becomes
`wss://subs.yourdomain.com/ws#t=TOKEN` — regenerate it with `server\setup.bat`.

The tunnel alone is **transport-encrypted but not authenticated at the
edge**: anyone who guesses/finds the hostname can reach the app token check
(and the unauthenticated `/health` endpoint). For an internet-exposed
hostname, add Zero Trust below.

### Locking it down with Zero Trust (service token) — recommended

Cloudflare Access sits **in front of** the tunnel: every request without a
valid service token is rejected at Cloudflare's edge, before it ever
reaches your laptop. Free for personal use, ~10 minutes to set up:

1. **Zero Trust dashboard**: <https://one.dash.cloudflare.com> (pick the
   free plan on first visit).
2. **Create the service token**: *Access → Service Auth → Create Service
   Token*. Name it e.g. `translator-clients`. Copy the **Client ID** and
   **Client Secret** immediately — the secret is shown only once.
3. **Create the Access application**: *Access → Applications → Add an
   application → Self-hosted*.
   - Application name: `translator`, session duration: e.g. `1 month`.
   - Application domain: `subs.yourdomain.com` (your tunnel hostname).
   - Add a policy: Action **Service Auth**, Include **Service Token** →
     `translator-clients`. (Optionally also require specific countries
     under the policy's additional settings.)
4. **Re-run `server\setup.bat`** — it now also asks for the Client ID and
   Secret (stored only so the wizard can reprint the code; the server
   itself never checks them) and prints the combined setup code:
   `wss://subs.yourdomain.com/ws#t=TOKEN&cid=CF-ID&csec=CF-SECRET`
5. Send friends the new code — the client sends all three secrets on
   connect (app token as an `Authorization: Bearer` header, the service
   token as `CF-Access-Client-Id/-Secret` headers). LAN connections with
   the old-style `#TOKEN` code keep working unchanged.

Notes:
- **Rotating access**: delete the service token (or the policy) in the
  Zero Trust dashboard and every client is locked out at the edge until
  you hand out a new code — no server restart needed.
- `/health` is also protected by the Access application once configured.
  The benchmarks poll `http://127.0.0.1:port/health` directly (localhost
  bypasses Cloudflare entirely), so they keep working.
- Defense in depth: the app token still applies at the server, so a LAN
  peer without the token gets nowhere even without Cloudflare in the path.

LAN-only testing needs no tunnel: the setup code with your PC's LAN IP
(`ws://192.168.x.x:8765/ws#t=TOKEN`) works as-is.

## Client setup (each player's PC)

**Option A — no Python at all (recommended for friends):** on your PC run
`client\build_client.bat` once and send friends the produced
`client\dist\TranslatorOverlay.zip`. They unzip, run `TranslatorOverlay.exe`,
paste the setup code, pick their language. SmartScreen will warn once
(unsigned exe) → "More info" → "Run anyway".

**Option B — classic:** send the `client\` folder; friend runs
`client\install.bat` then `client\start.bat` (asks for the setup code on
first run only, saved to `client\config.json`).

Hotkeys: `Ctrl+Alt+P` pause · `Ctrl+Alt+O` hide · `Ctrl+Alt+I` toggle
click-through · `Ctrl+Alt+Q` quit. The server drop is shown as a small
"reconnecting…" banner; reconnection is automatic.

## Running it

**One command** (repo root): `start_all.bat` launches the server, the Discord
Web auto-joiner, and the local overlay client (if `client\venv` exists — pass
`noclient` to skip it). `stop_all.bat` stops everything, including the
joiner's Chromium but never your own Chrome.

- `/join` — bot joins your voice channel and starts translating everyone.
- `/leave` — bot leaves.
- `/status` — model, viewers, latency percentiles.
- `/mode chat|game` — game mode = shorter silences, faster callouts.
  (In loopback mode, toggle with the client's Ctrl+Alt+G instead; the pinned
  language cycles with Ctrl+Alt+L: AUTO → KO → JA → VI.)

## Benchmarks (`bench\`, run with the server venv)

```bat
cd bench
..\server\venv\Scripts\python fetch_media.py            # Korean YouTube clips + captions
..\server\venv\Scripts\python bench_e2e.py --id <id>    # latency + accuracy through the WS
..\server\venv\Scripts\python bench_e2e.py --id <id> --whisper-model small
..\server\venv\Scripts\python compare_apps.py --id <id> # new pipeline vs the old desktop app
```

Reports land in `bench\bench_out\` (markdown + CSV + raw event log).
`--fast` skips realtime pacing so latency = pure processing (matches the old
app's numbers); without it you measure the true end-to-end path including
the WebSocket hop. Caption references come from YouTube auto-subs — treat
accuracy as *comparative* (A vs B), not absolute.

## Troubleshooting

- **Server starts then says CUDA probe failed → CPU** — install the two
  `nvidia-*-cu12` wheels (install.bat does) and update the GPU driver.
- **Out of VRAM / stutter while the laptop games** — set
  `"whisper_model": "small"` in `server/config.json` (and/or
  `--whisper-model small`).
- **Bot joins but no subtitles** — the bot must NOT be self-deafened
  (it never is by itself; don't deafen it manually). Check `/status`.
- **Slash commands missing** — global sync takes up to an hour; set
  `discord.guild_id` in `server/config.json` (setup.bat asks) for instant.
- **Client can't connect** — wrong token (regenerate via setup.bat), tunnel
  down, or `server.port` mismatch. `http://<host>:<port>/health` must answer
  (with Zero Trust configured, opening it in a browser shows Cloudflare's
  login page instead — that means the edge auth is active).
- **Client gets 403 from Cloudflare** — the service token is wrong/expired,
  or the Access policy doesn't include it. Regenerate the setup code via
  `server\setup.bat` after checking the Zero Trust app's policy.
- **Client gets 401 from the server** — app token mismatch (the part after
  `t=` in the setup code); server config `server.access_token`.
- **First words of each utterance missing** — raise `vad.padding_ms`.

## Layout

```
server/   bot.py (Discord), voice_receiver.py (per-user audio),
          speakers.py (per-speaker VAD), pipeline.py (ASR + fan-out),
          sub_server.py (WebSocket), segmentation/transcriber/translator.py
          (ported from the desktop app), setup/install/start bats
client/   client.py (WS + first-run), overlay.py (PyQt6 overlay),
          build_client.bat (PyInstaller exe for friends)
bench/    fetch_media.py, bench_e2e.py, compare_apps.py
```
