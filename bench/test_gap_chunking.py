"""Why is bot-mode accuracy lower? A/B/C test of delivery-gap chunking.

    ../<server venv>/python test_gap_chunking.py [--port 8899]

Spawns the real server in source=ws mode (scratch config), then feeds the
SAME audio file through /audio three ways:

  A  continuous   : frames back-to-back, fast pacing (loopback-equivalent)
  B  gapped-bare  : phrase-sized groups with REAL wall-clock pauses between
                    them and NO frames during the pause  (what the voice-bot
                    sidecar does today — Discord sends no packets in silence)
  C  gapped+fill  : same delivery, but silence frames fill every pause
                    (the fix: reconstruct the real timeline)

Compares subtitle count, text length, and final-subtitle latency. If B
merges utterances (fewer, longer, slower finals) while C matches A, the
missing-silence hypothesis is proven.
"""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
SERVER_DIR = BENCH.parent / "server"
SERVER_PY = str(SERVER_DIR / "venv" / "Scripts" / "python.exe")
if not Path(SERVER_PY).exists():
    SERVER_PY = sys.executable

import aiohttp  # noqa: E402


def frames_of(pcm: "np.ndarray", step: int):
    return [pcm[i:i + step] for i in range(0, len(pcm), step)]


async def ship(session: aiohttp.ClientSession, port: int, token: str,
               mode: str, phrases: list, pause_s: float):
    """phrases: list of np arrays (each a spoken turn). A: back-to-back,
    B: real wall-clock pause between turns with NO frames (sidecar today),
    C: same pauses filled with silence frames (the fix)."""
    speaker = "GapTest"
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    import numpy as np

    async def send(c):
        await ws.send_bytes(
            len(b"GapTest").to_bytes(2, "little") + b"GapTest"
            + c.astype("<f4").tobytes())

    async with session.ws_connect(f"http://127.0.0.1:{port}/audio",
                                  headers=hdr) as ws:
        await ws.send_json({"type": "hello", "client": "gap-test",
                            "speaker": speaker})
        await ws.receive()  # welcome
        t0 = time.monotonic()
        zeros = np.zeros(1600, dtype="<f4")
        for i, phrase in enumerate(phrases):
            if i > 0 and mode in ("B", "C"):
                if mode == "B":
                    await asyncio.sleep(pause_s)
                else:
                    for _ in range(int(pause_s / 0.1)):
                        await send(zeros)
                    await asyncio.sleep(0.05)
            for j in range(0, len(phrase), 1600):
                await send(phrase[j:j + 1600])
        print(f"  [{mode}] shipped {len(phrases)} phrases "
              f"in {time.monotonic() - t0:.1f}s")
        if mode != "B":  # trailing silence closes the last utterance
            for _ in range(12):
                await send(zeros)
        await ws.close()


async def collect(session: aiohttp.ClientSession, port: int, token: str,
                  timeout_s: float) -> list[dict]:
    hdr = {"Authorization": f"Bearer {token}"} if token else {}
    events = []
    t0 = time.monotonic()
    async with session.ws_connect(f"http://127.0.0.1:{port}/ws",
                                  headers=hdr) as ws:
        await ws.send_json({"type": "hello", "target_lang": "eng_Latn"})
        while time.monotonic() - t0 < timeout_s:
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
            except asyncio.TimeoutError:
                break
            if msg.type == aiohttp.WSMsgType.TEXT:
                ev = msg.json()
                if ev.get("type") == "subtitle" and not ev.get("partial"):
                    events.append(ev)
    return events


async def run_mode(session, port, token, mode, phrases, pause_s):
    collector = asyncio.create_task(collect(session, port, token, timeout_s=50))
    await asyncio.sleep(0.5)
    await ship(session, port, token, mode, phrases, pause_s)
    events = await collector
    await asyncio.sleep(3)  # let the VAD settle before the next mode
    return events


async def amain(port: int, token: str, gaps: int):
    import numpy as np
    with wave.open(str(BENCH / "bench_cache" / "test_ko.wav"), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), np.int16) \
                .astype(np.float32) / 32767.0

    # conversation shape: 3 phrases of ~2.5s separated by 4s of real silence
    # (a person finishing a sentence and saying nothing for a while)
    phrase_chunks = int(2.5 / 0.1)
    phrases = [pcm[i * phrase_chunks * 1600:(i + 1) * phrase_chunks * 1600]
               for i in range(3)]
    phrases = [p for p in phrases if len(p) > 1600]

    cfg = json.loads((SERVER_DIR / "config.json").read_text(encoding="utf-8"))
    cfg["source"] = "ws"
    cfg["server"]["port"] = port
    cfg.setdefault("discord_web", {})["auto_join"] = False
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    log = Path(tempfile.mkdtemp()) / "server.log"
    proc = subprocess.Popen([SERVER_PY, "-u", "main.py", "--port", str(port)],
                            cwd=str(SERVER_DIR),
                            env={**os.environ, "TRANSLATOR_CONFIG": str(tmp)},
                            stdout=open(log, "wb"), stderr=subprocess.STDOUT)
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(90):
                await asyncio.sleep(1)
                if proc.poll() is not None:
                    print(f"SERVER DIED:\n{log.read_text(errors='replace')[-1500:]}")
                    return 1
                try:
                    async with session.get(f"http://127.0.0.1:{port}/health") as r:
                        if (await r.json()).get("ready"):
                            break
                except Exception:
                    pass
            print(f"server ready — 3 phrases x 2.5s, {gaps}s real pauses "
                  "between turns\n")

            results = {}
            for mode in ("A", "B", "C"):
                print(f"mode {mode}:")
                events = await run_mode(session, port, token, mode, phrases,
                                        gaps)
                finals = [e for e in events if not e.get("partial")]
                lat = [e.get("latency", 0) for e in finals]
                results[mode] = finals
                print(f"  finals={len(finals)} "
                      f"total_chars={sum(len(e.get('original','')) for e in finals)} "
                      f"max_latency={max(lat) if lat else 0:.1f}s")
                await asyncio.sleep(8)  # stay under the Groq RPM gate
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()

    a, b, c = (results.get(m, []) for m in ("A", "B", "C"))
    print("\n=== verdict ===")
    print(f"A continuous : {len(a)} finals (all 3 phrases back-to-back)")
    print(f"B gapped-bare: {len(b)} finals  <- today's sidecar behaviour")
    print(f"C gapped+fill: {len(c)} finals  <- with silence reconstruction")
    if len(b) < len(a) and len(c) >= len(a):
        print("\nPROVEN: without silence frames the turn gaps vanish — the VAD")
        print("never closes the utterance, so the next phrase MERGES into it.")
        print("Silence reconstruction (C) restores correct per-turn chunking.")
    else:
        print("\nINCONCLUSIVE — compare the transcripts above by hand.")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    ap.add_argument("--gaps", type=int, default=25,
                    help="insert a pause every N chunks (~2.5s of speech)")
    args = ap.parse_args()
    tok = ""
    try:
        tok = json.loads((SERVER_DIR / "config.json").read_text(
            encoding="utf-8")).get("server", {}).get("access_token") or ""
    except Exception:
        pass
    sys.exit(asyncio.run(amain(args.port, tok, args.gaps)))
