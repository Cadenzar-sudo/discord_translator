"""End-to-end test of the /audio shipper endpoint, no Docker needed.

    ../server/venv/Scripts/python.exe test_audio_ws.py [--port 8899]

Spawns the real server in source=ws mode (scratch config derived from the
live one, scratch port), streams bench_cache/test_ko.wav into /audio as a
fake shipper, collects /ws as a viewer, and asserts:
  - the shipper connects + is welcomed
  - a subtitle arrives with the expected speaker label
  - the subtitle text and its English translation are non-empty
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
    SERVER_PY = sys.executable  # fall back to whatever python we're running

import aiohttp  # noqa: E402


async def collect(port: int, token: str, want: dict, timeout_s: float) -> list[dict]:
    events = []
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(
                f"http://127.0.0.1:{port}/ws",
                headers={"Authorization": f"Bearer {token}"}) as ws:
            await ws.send_json({"type": "hello", "target_lang": "eng_Latn"})
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout_s and len(events) < want.get("n", 1):
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                except asyncio.TimeoutError:
                    break
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                ev = msg.json()
                if ev.get("type") == "subtitle":
                    events.append(ev)
    return events


async def ship_file(port: int, token: str, wav: Path, speaker: str):
    with wave.open(str(wav), "rb") as w:
        audio = w.readframes(w.getnframes())
    import numpy as np
    pcm = np.frombuffer(audio, np.int16).astype(np.float32) / 32767.0

    def frame(block: "np.ndarray") -> bytes:
        nb = speaker.encode("utf-8")
        return len(nb).to_bytes(2, "little") + nb + block.astype("<f4").tobytes()

    step = 16000 // 10  # 100 ms
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(
                f"http://127.0.0.1:{port}/audio",
                headers={"Authorization": f"Bearer {token}"}) as ws:
            await ws.send_json({"type": "hello", "client": "test-feeder",
                                "speaker": speaker})
            ack = json.loads((await ws.receive()).data)
            assert ack.get("type") == "welcome", f"unexpected ack: {ack}"
            print(f"[test] shipper welcomed ({ack.get('speaker')!r}) — "
                  f"streaming {len(pcm) / 16000:.1f}s")
            for i in range(0, len(pcm), step):
                await ws.send_bytes(frame(pcm[i:i + step]))
            print("[test] file shipped; closing shipper (triggers flush)")


async def amain(port: int, token: str):
    # scratch config: live keys, but source=ws and a scratch port
    cfg = json.loads((SERVER_DIR / "config.json").read_text(encoding="utf-8"))
    cfg["source"] = "ws"
    cfg["server"]["port"] = port
    cfg.setdefault("discord_web", {})["auto_join"] = False
    tmp = Path(tempfile.mkdtemp()) / "config.json"
    tmp.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")

    env = {**os.environ, "TRANSLATOR_CONFIG": str(tmp)}
    log = Path(tempfile.mkdtemp()) / "server.log"
    print(f"[test] starting server on :{port} (log: {log})")
    proc = subprocess.Popen([SERVER_PY, "-u", "main.py", "--port", str(port)],
                            cwd=str(SERVER_DIR), env=env,
                            stdout=open(log, "wb"), stderr=subprocess.STDOUT)

    async with aiohttp.ClientSession() as s:
        ok = False
        for _ in range(90):  # models load ~30 s on first run
            await asyncio.sleep(1)
            if proc.poll() is not None:
                print(f"[test] SERVER DIED:\n{log.read_text(errors='replace')[-2000:]}")
                return 1
            try:
                async with s.get(f"http://127.0.0.1:{port}/health") as r:
                    st = await r.json()
                if st.get("ready"):
                    ok = True
                    break
            except Exception:
                pass
        if not ok:
            print(f"[test] server not ready in time:\n{log.read_text(errors='replace')[-2000:]}")
            proc.terminate()
            return 1
        print(f"[test] server ready (device={st.get('device')}, "
              f"audio={st.get('audio')})")

    # viewer first, then shipper, then wait for subtitles
    viewer = asyncio.create_task(collect(port, token, {"n": 2}, timeout_s=60))
    await asyncio.sleep(1)
    await ship_file(port, token, BENCH / "bench_cache" / "test_ko.wav",
                    speaker="TestSpeaker")
    events = await viewer

    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()

    srcs = [e.get("original", "") for e in events]
    trs = [e.get("translated", "") for e in events]
    spk = {e.get("speaker") for e in events}
    print(f"[test] got {len(events)} subtitle event(s); speakers={spk or '{}'}")
    for o, t in zip(srcs, trs):
        print(f"       ko: {o!r}\n       en: {t!r}")

    problems = []
    if not events:
        problems.append("no subtitles arrived")
    else:
        if "TestSpeaker" not in spk:
            problems.append(f"speaker label wrong: {spk}")
        if not any(s.strip() for s in srcs):
            problems.append("empty transcript")
        if not any(t.strip() for t in trs):
            problems.append("empty translation")
    if problems:
        print(f"[FAIL] {problems}")
        return 1
    print("[PASS] /audio shipper -> pipeline -> /ws subtitle, correct speaker label")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    cfg_tok = ""
    try:
        cfg_tok = json.loads((SERVER_DIR / "config.json").read_text(
            encoding="utf-8")).get("server", {}).get("access_token") or ""
    except Exception:
        pass
    sys.exit(asyncio.run(amain(args.port, cfg_tok)))
