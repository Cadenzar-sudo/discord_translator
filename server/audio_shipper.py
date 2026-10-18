"""Windows VB-Cable audio shipper: the fallback audio source for source="ws".

Captures the loopback device (VB-Cable) with the same AudioCapture the native
server uses and streams blocks to the translator server's /audio endpoint as
[2B uid-len][utf8 uid][2B name-len][utf8 name][float32-LE samples] binary
frames — the same protocol the Node voice-bot sidecar speaks. The mixed
stream is one speaker: uid is "loopback-<name>", name the display label.
Auto-reconnects every 2 s; when the connection is down, live frames are
dropped (the fallback only matters while the server is reachable anyway).

Usage (server venv, on the Windows host with VB-Cable):
  python audio_shipper.py                       # live capture
  python audio_shipper.py --list-devices
  python audio_shipper.py --file clip.wav       # stream a file (testing)
  python audio_shipper.py --launch-joiner       # also spawn the web joiner

Reads url/token/device from the same config.json as everything else.
"""

import argparse
import asyncio
import queue
import sys
import time
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from config import load_config  # noqa: E402

TARGET_RATE = 16000


def build_frame(uid: str, name: str, block) -> bytes:
    ub = uid.encode("utf-8")[:64]
    nb = name.encode("utf-8")[:128]
    return (len(ub).to_bytes(2, "little") + ub
            + len(nb).to_bytes(2, "little") + nb
            + block.astype("<f4").tobytes())


async def ship_ws(url: str, token: str, hello: dict, get_block, stop_event):
    """One connection attempt; raises on failure so the caller retries."""
    import aiohttp

    full = url + (("&" if "?" in url else "?") + "token=" + token if token else "")
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(
                full, headers=({"Authorization": f"Bearer {token}"}
                               if token else {}),
                heartbeat=25.0) as ws:
            await ws.send_json(hello)
            ack = await asyncio.wait_for(ws.receive(), timeout=10)
            if ack.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"no welcome from server ({ack.type})")
            print(f"[shipper] connected to {url} — streaming")
            loop = asyncio.get_running_loop()
            while not stop_event.is_set():
                block = await loop.run_in_executor(None, get_block)
                if block is None:
                    break
                if len(block) == 0:
                    continue  # capture stall: don't ship an empty frame
                await ws.send_bytes(build_frame(
                    f"loopback-{hello['speaker']}", hello["speaker"], block))
            await ws.close()


async def amain(cfg, args):
    stop_event = asyncio.Event()
    hello = {"type": "hello", "client": "loopback",
             "speaker": args.speaker or cfg.get("speaker_name", "Discord")}

    q: "queue.Queue" = queue.Queue(maxsize=400)
    capture = None

    if args.file:
        with wave.open(str(Path(args.file)), "rb") as w:
            rate, ch, n = (w.getframerate(), w.getnchannels(), w.getnframes())
            raw = w.readframes(n)
        import numpy as np
        audio = np.frombuffer(raw, np.int16).astype(np.float32) / 32767.0
        if ch > 1:
            audio = audio.reshape(-1, ch).mean(axis=1)
        assert rate == TARGET_RATE, f"{args.file} must be {TARGET_RATE} Hz"
        step = TARGET_RATE // 10  # 100 ms
        chunks = [audio[i:i + step] for i in range(0, len(audio), step)]
        idx = 0
        print(f"[shipper] file mode: {args.file} ({len(audio) / rate:.1f}s)")

        def get_block():
            nonlocal idx
            if idx >= len(chunks):
                return None
            c = chunks[idx]
            idx += 1
            if not args.fast:  # realtime pacing keeps VAD timing honest
                time.sleep(step / TARGET_RATE)
            return c
    else:
        from audio_capture import AudioCapture
        capture = AudioCapture(args.device or cfg["audio_device"], q,
                               capture_rate=int(cfg.get("capture_sample_rate") or 0))
        capture.start()
        print(f"[shipper] capturing '{capture.device_name}' as speaker "
              f"'{hello['speaker']}'")

        def get_block():
            try:
                return q.get(timeout=1.0)
            except queue.Empty:
                return b""

    if args.launch_joiner and not args.file:
        import subprocess
        joiner = subprocess.Popen(
            [sys.executable, "-u", str(APP_DIR / "discord_web_joiner.py")],
            cwd=str(APP_DIR))
        print(f"[shipper] web joiner launched (pid {joiner.pid})")

    url = args.url or (cfg.get("audio_shipper") or {}).get("url") or \
        f"ws://127.0.0.1:{int(cfg['server']['port'])}/audio"
    token = args.token if args.token is not None else \
        cfg["server"].get("access_token") or ""

    backoff = 2.0
    try:
        while True:
            try:
                await ship_ws(url, token, hello, get_block, stop_event)
                if args.file:
                    break  # file fully shipped
            except Exception as e:
                msg = str(e).splitlines()[0] if str(e) else repr(e)
                print(f"[shipper] connection failed ({msg}) — retry in "
                      f"{backoff:.0f}s")
                if args.file:
                    raise
            await asyncio.sleep(backoff)
    except KeyboardInterrupt:
        pass
    finally:
        if capture is not None:
            capture.stop()
        print("[shipper] stopped")


def main():
    ap = argparse.ArgumentParser(description="VB-Cable audio shipper")
    ap.add_argument("--url", help="override audio_shipper.url")
    ap.add_argument("--token", help="override server.access_token")
    ap.add_argument("--device", help="override audio_device substring")
    ap.add_argument("--speaker", help="override speaker_name label")
    ap.add_argument("--file", help="stream a 16 kHz mono WAV instead of live capture")
    ap.add_argument("--fast", action="store_true",
                    help="with --file: no realtime pacing")
    ap.add_argument("--launch-joiner", action="store_true",
                    help="also start discord_web_joiner.py")
    ap.add_argument("--list-devices", action="store_true")
    args = ap.parse_args()

    if args.list_devices:
        from audio_capture import list_input_devices
        for d in list_input_devices():
            print(f"  [{d['index']}] {d['name']} "
                  f"({d['inputs']} in, {d['rate']:.0f} Hz)")
        return

    asyncio.run(amain(load_config(), args))


if __name__ == "__main__":
    main()
