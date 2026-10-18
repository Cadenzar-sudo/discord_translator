"""Discord bot (py-cord): joins voice channels on /join, feeds per-user audio
into the SpeakerManager, exposes /leave /status /mode.

py-cord implements Discord's DAVE end-to-end voice encryption (mandatory on
regular voice channels since 2026) — discord.py does not, which is why this
bot runs on py-cord. The davey package must be installed for DAVE.

Slash-only. The bot must NOT self-deafen: a deafened bot receives no audio.
"""

import asyncio
import time

import discord
from discord.ext import commands  # noqa: F401  (py-cord requires it installed)

from config import apply_profile


class TranslatorBot(discord.Bot):
    def __init__(self, cfg: dict, speaker_manager, worker, registry):
        super().__init__(intents=discord.Intents.default())
        self.cfg = cfg
        self.speakers = speaker_manager
        self.worker = worker
        self.registry = registry
        self.started_at = time.time()
        self._synced_guilds = False
        self._register_commands()

    def _register_commands(self):
        @self.slash_command(name="join",
                            description="Join your voice channel and translate everyone")
        async def _join(ctx: discord.ApplicationContext):
            voice = ctx.author.voice if ctx.author else None
            if not voice or not voice.channel:
                await ctx.respond("Join a voice channel first, then run /join.",
                                  ephemeral=True)
                return
            await ctx.defer(ephemeral=True)
            # one channel at a time: drop any existing connection
            for vc in list(self.voice_clients):
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
            self.speakers.stop_all()
            print(f"[bot] /join: connecting to '{voice.channel.name}'…")
            try:
                # py-cord defaults to NOT self-deafened, which we require:
                # a deafened bot receives no audio at all
                vc = await voice.channel.connect(timeout=30.0, reconnect=True)
            except Exception as e:
                print(f"[bot] /join FAILED: {e!r}")
                await ctx.respond(f"Could not join: {e}", ephemeral=True)
                return
            print(f"[bot] /join: voice handshake complete "
                  f"(DAVE={'yes' if getattr(vc, 'is_dave_connection', lambda: False)() else 'no'})")
            from voice_receiver import DiscordSink
            sink = DiscordSink(self.speakers,
                               dump_raw=bool(self.cfg.get("debug_save_audio")))
            # py-cord's start_listening (an alias of start_recording) never
            # calls sink.init(vc), so sink.client stays None and the packet
            # router's `assert self.sink.client` kills receive on the first
            # packet — the core of pycord issue #3139. Wire it up ourselves.
            sink.init(vc)
            vc.start_listening(sink)
            print(f"[bot] Voice sink attached in '{voice.channel.name}' — "
                  f"waiting for audio packets")
            self._start_voice_watch(vc)
            await ctx.respond(
                f"Listening to **{voice.channel.name}** — everyone in this "
                f"channel can now run the overlay client.", ephemeral=True)

        @self.slash_command(name="leave",
                            description="Stop translating and leave")
        async def _leave(ctx: discord.ApplicationContext):
            if not self.voice_clients:
                self.speakers.stop_all()
                await ctx.respond("I'm not in a voice channel.", ephemeral=True)
                return
            for vc in list(self.voice_clients):
                try:
                    await asyncio.wait_for(vc.disconnect(force=True), timeout=10.0)
                except Exception as e:
                    print(f"[bot] /leave: disconnect issue: {e!r}")
            self.speakers.stop_all()
            await ctx.respond("Left the channel.", ephemeral=True)

        @self.slash_command(name="status",
                            description="Pipeline status and latency")
        async def _status(ctx: discord.ApplicationContext):
            await ctx.respond(self.status_text(), ephemeral=True)

        @self.slash_command(name="mode",
                            description="Switch VAD/decoding profile")
        @discord.option("profile", description="chat = accurate conversation, "
                                               "game = fast short callouts",
                        choices=["chat", "game"])
        async def _mode(ctx: discord.ApplicationContext, profile: str):
            self.cfg["profile"] = profile
            apply_profile(self.cfg)
            if self.worker.transcriber:  # may be unloaded under Groq ASR
                self.worker.transcriber.beam_size = int(self.cfg["beam_size"])
            await ctx.respond(
                f"{profile.upper()} mode "
                f"(silence {self.cfg['vad']['min_silence_ms']}ms, "
                f"beam {self.cfg['beam_size']})", ephemeral=True)

    def _start_voice_watch(self, vc):
        """Log raw datagram flow + the reader's silent error slot, so a dead
        packet router can't fail invisibly."""
        import threading
        state = {"n": 0}

        def on_datagram(msg):
            state["n"] += 1

        try:
            vc._connection.add_socket_listener(on_datagram)
        except Exception:
            pass

        def watch():
            while True:
                try:
                    time.sleep(5)
                    if not vc.is_connected():
                        return
                    r = getattr(vc, "_reader", None)
                    if r is not None:
                        print(f"[voice-watch] raw_dgrams={state['n']} "
                              f"reader_active={r.active} reader_error={r.error!r} "
                              f"decoders={len(r.packet_router.decoders)}")
                except Exception:
                    return  # client torn down; nothing to watch anymore

        threading.Thread(target=watch, daemon=True, name="voice-watch").start()

    def status_text(self) -> str:
        st = self.worker.stats() if self.worker else {}
        vc = self.voice_clients[0] if self.voice_clients else None
        lines = [
            f"**Channel:** {vc.channel.name if vc else 'not connected'}",
            f"**Speakers seen:** {', '.join(self.speakers.names()) or '—'}",
            f"**Viewers:** {self.registry.viewer_count()}",
            f"**Model:** `{self.cfg['whisper_model']}` on "
            f"`{self.worker.device}/{self.worker.compute_type}`"
            if self.worker else "**Model:** loading…",
            f"**Mode:** {self.cfg.get('profile', 'chat')} · uptime "
            f"{int(time.time() - self.started_at) // 60} min",
            f"**Latency (finals):** p50 {st.get('final_p50_s', 0)}s · "
            f"p95 {st.get('final_p95_s', 0)}s over "
            f"{st.get('utterances_final', 0)} utterances",
        ]
        return "\n".join(lines)

    async def setup_hook(self):
        print("[bot] setup: commands registered, waiting for guilds")

    async def on_ready(self):
        perms = discord.Permissions(view_channel=True, connect=True)
        url = discord.utils.oauth_url(
            self.application_id, permissions=perms,
            scopes=("bot", "applications.commands"))
        print(f"[bot] Logged in as {self.user} — invite: {url}")
        if not self._synced_guilds:
            self._synced_guilds = True
            # per-guild registration appears instantly (global sync can lag ~1h)
            for guild in self.guilds:
                try:
                    await self.sync_commands(guild_ids=[guild.id])
                    print(f"[bot] /join /leave /status /mode ready in "
                          f"'{guild.name}'")
                except Exception as e:
                    print(f"[bot] Guild command sync failed for "
                          f"{guild.name}: {e}")

    async def on_voice_state_update(self, member, before, after):
        if member.id == self.user.id:
            if not after.channel:
                if not any(vc.is_connected() for vc in self.voice_clients):
                    self.speakers.stop_all()
            return
        me = next((vc for vc in self.voice_clients
                   if vc.channel and member in vc.channel.members), None)
        if me and not after.channel:
            self.speakers.remove(member.id, flush=True)

    def status_dict(self) -> dict:
        st = self.worker.stats() if self.worker else {}
        return {
            "mode": self.cfg.get("profile", "chat"),
            "channel": (self.voice_clients[0].channel.name
                        if self.voice_clients else None),
            "speakers": self.speakers.names(),
            "viewer_count": self.registry.viewer_count(),
            "model": self.cfg.get("whisper_model"),
            "device": getattr(self.worker, "device", None),
            "compute_type": getattr(self.worker, "compute_type", None),
            "ready": bool(self.worker and
                          (self.worker.transcriber or self.worker.groq)),
            "uptime_s": int(time.time() - self.started_at),
            **st,
        }
