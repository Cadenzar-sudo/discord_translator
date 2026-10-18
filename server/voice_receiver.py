"""Discord voice receiving (py-cord): per-user opus packets -> 16 kHz blocks.

py-cord (>= 2.6) implements Discord's DAVE end-to-end voice encryption, which
is mandatory on regular voice channels since 2026 — this is why the bot runs
on py-cord instead of discord.py. The sink receives fully decrypted, opus-
decoded PCM per user (is_opus() = False); we downmix to mono and resample to
the 16 kHz float32 blocks the VadSegmenter contract expects.
"""

import time

import numpy as np
import soxr
from discord import sinks

DISCORD_RATE = 48000  # Discord voice opus output rate
TARGET_RATE = 16000


def _patch_opus_error_tolerance():
    """One invalid opus packet must not kill the packet router (it would
    permanently deafen the bot). Drop bad packets instead, and capture the
    first ~300 raw payloads for offline decoding with an independent decoder."""
    import struct as _struct
    from discord import opus as dopus

    original = dopus.PacketDecoder._decode_packet
    fails = {"n": 0, "f": None}

    def tolerant_decode(self, packet):
        try:
            return original(self, packet)
        except Exception as e:
            fails["n"] += 1
            dd = getattr(packet, "decrypted_data", b"") or b""
            if fails["f"] is None:
                from pathlib import Path
                d = Path(__file__).resolve().parent / "debug_audio"
                d.mkdir(exist_ok=True)
                fails["f"] = open(d / "payloads.opus", "wb", buffering=0)
            try:
                fails["f"].write(_struct.pack("<H", len(dd)) + dd)
            except Exception:
                pass
            if fails["n"] <= 5 or fails["n"] % 100 == 0:
                print(f"[voice] dropped undecodable opus packet "
                      f"(seq={getattr(packet, 'sequence', '?')}): {e} "
                      f"(total {fails['n']})")
            return packet, b""

    dopus.PacketDecoder._decode_packet = tolerant_decode


def _patch_chain_probe():
    """Count datagrams at each stage of the receive chain so a dead link
    announces itself: raw callback -> decrypt (incl. DAVE) -> sink.write."""
    from discord.voice.receive import reader as recv_mod

    reader_cls = recv_mod.AudioReader
    orig_callback = reader_cls.callback

    def callback(self, packet_data):
        stats = self.__dict__.setdefault("_probe", {"raw": 0, "t": 0.0,
                                                    "hexed": 0})
        stats["raw"] += 1
        if stats["hexed"] < 3:
            stats["hexed"] += 1
            print(f"[chain] datagram #{stats['raw']}: len={len(packet_data)} "
                  f"hex={packet_data[:16].hex()}")
        now = time.monotonic()
        if now - stats["t"] > 10.0:
            stats["t"] = now
            dave = getattr(self.client._connection, "dave_session", None)
            ka = getattr(self, "keep_alive", None)
            print(f"[chain] raw={stats['raw']} "
                  f"dave_session={'yes' if dave else 'no'} "
                  f"dave_ready={getattr(dave, 'ready', '?')} "
                  f"keepalive_sent={getattr(ka, 'counter', '?')} "
                  f"endpoint={getattr(self.client._connection, 'endpoint_ip', '?')}:"
                  f"{getattr(self.client._connection, 'voice_port', '?')}")
        return orig_callback(self, packet_data)

    reader_cls.callback = callback

    dec_cls = recv_mod.PacketDecryptor
    orig_dec = dec_cls.decrypt_rtp

    from discord.voice.packets.core import OPUS_SILENCE as _OPUS_SILENCE

    def decrypt_rtp(self, packet):
        state = self.client._connection
        dave = state.dave_session
        uid = state.ssrc_user_map.get(packet.ssrc) if dave else None
        out = orig_dec(self, packet)
        stats = self.__dict__.setdefault(
            "_probe", {"dec": 0, "noready": 0, "nouid": 0, "ok": 0,
                       "silence": 0, "t": 0.0})
        stats["dec"] += 1
        if dave is None or not getattr(dave, "ready", False):
            stats["noready"] += 1
        elif not uid:
            stats["nouid"] += 1
        elif out == _OPUS_SILENCE:
            stats["silence"] += 1
        else:
            stats["ok"] += 1
        now = time.monotonic()
        if now - stats["t"] > 10.0:
            stats["t"] = now
            print(f"[chain] decryptor: total={stats['dec']} ok={stats['ok']} "
                  f"dave_not_ready={stats['noready']} no_uid={stats['nouid']} "
                  f"silence_substituted={stats['silence']} "
                  f"ssrc_map={dict(state.ssrc_user_map)}")
        return out

    dec_cls.decrypt_rtp = decrypt_rtp


def _patch_keepalive_interval():
    """py-cord 2.8 bug: UDPKeepAlive.delay = 5000 is fed straight to
    time.sleep() (which takes SECONDS), so the keepalive thread sends one
    packet and sleeps for 83 minutes. Discord's media servers stop
    forwarding audio to a source that never sends — the bot joins, gets a
    few DTX frames, then goes deaf. Discord expects ~one keepalive per 5s."""
    from discord.voice.receive.reader import UDPKeepAlive
    UDPKeepAlive.delay = 5


_patch_keepalive_interval()

_patch_chain_probe()

_patch_opus_error_tolerance()


class DiscordSink(sinks.Sink):
    """write() fires on the reader thread — keep it cheap, never block."""

    # py-cord's SinkEventRouter expects this even on sinks that don't use
    # its speaking-state events (its own built-in sinks never define it)
    __sink_listeners__ = ()

    def __init__(self, speaker_manager, dump_raw: bool = False):
        super().__init__()
        self.manager = speaker_manager
        self._resamplers: dict[int, "soxr.ResampleStream"] = {}
        # traffic diagnostics: if subtitles don't appear, these counters tell
        # whether Discord is sending us anything at all
        self._n = self._n_nopcm = self._n_unmapped = 0
        self._last_log = 0.0
        self._dump_raw = dump_raw
        self._dump_files: dict[int, any] = {}

    def is_opus(self) -> bool:
        """False = the reader decodes opus to PCM before calling write()."""
        return False

    def walk_children(self):
        """The router walks child sinks for event registration; we have none.
        (py-cord 2.8's Sink base lacks this method entirely.)"""
        return iter(())

    def write(self, data, user):
        try:
            self._n += 1
            # heartbeat prints even when everything fails below, so "no
            # lines at all" reliably means "no packets at all"
            now = time.monotonic()
            if now - self._last_log > 10.0:
                self._last_log = now
                print(f"[voice] traffic: {self._n} packets, "
                      f"{self._n_nopcm} undecodable, {self._n_unmapped} unmapped, "
                      f"{len(self._resamplers)} speaker stream(s)")
            pcm = getattr(data, "pcm", None)
            if not pcm:
                self._n_nopcm += 1
                return
            member = getattr(data, "source", None) or user
            if member is None:
                # SSRC not yet mapped to a member (brief startup window)
                self._n_unmapped += 1
                return
            uid = member.id
            name = getattr(member, "display_name", None) or member.name or "Unknown"
            rs = self._resamplers.get(uid)
            if rs is None:
                rs = soxr.ResampleStream(DISCORD_RATE, TARGET_RATE, 1,
                                         dtype="float32")
                self._resamplers[uid] = rs
            arr = np.frombuffer(pcm, dtype=np.int16)
            if len(arr) % 2 == 0:
                arr = arr.reshape(-1, 2).mean(axis=1)  # stereo -> mono
            mono = arr.astype(np.float32) / 32768.0
            out = rs.resample_chunk(mono)
            if len(out):
                block = np.asarray(out, dtype=np.float32)
                if self._dump_raw:
                    self._dump(uid, name, block)
                self.manager.push(uid, name, block)
        except Exception as e:
            print(f"[voice] Sink write failed: {e}")

    def _dump(self, uid, name, block):
        """Append the 16 kHz stream we actually heard to a wav (ground truth
        for 'is the audio itself broken?')."""
        import wave
        f = self._dump_files.get(uid)
        if f is None:
            from pathlib import Path
            d = Path(__file__).resolve().parent / "debug_audio"
            d.mkdir(exist_ok=True)
            safe = "".join(c if c.isalnum() else "_" for c in str(name))[:24]
            path = d / f"raw_{safe}_{int(time.time())}.wav"
            f = self._dump_files[uid] = wave.open(str(path), "wb")
            f.setnchannels(1); f.setsampwidth(2); f.setframerate(TARGET_RATE)
            print(f"[voice] dumping raw audio -> {path.name}")
        try:
            f.writeframes((np.clip(block, -1, 1) * 32767).astype(np.int16).tobytes())
        except Exception:
            self._dump_files.pop(uid, None)

    def cleanup(self):
        self.finished = True
        self._resamplers.clear()
        self.manager.stop_all()
