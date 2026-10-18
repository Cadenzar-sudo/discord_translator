"""Measure the full caption pipeline through the Cloudflare tunnel:
inject a clip into CABLE Input -> server captures -> Groq ASR -> Azure MT ->
WebSocket through the tunnel -> this collector records precise arrival times.

Stage breakdown comes from the events' stage_ts (server-side monotonic clock;
the collector runs on the same machine, so clocks are comparable):
  asr   = stage_ts.asr  - stage_ts.vad     (Groq transcription)
  mt    = stage_ts.trans - stage_ts.asr    (Azure translation)
  sent  = stage_ts.sent - stage_ts.trans  (server send)
  tunnel= recv - stage_ts.sent            (Cloudflare edge -> viewer)
  total = recv - stage_ts.vad             (utterance closed -> subtitle)
"""

import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
CACHE = BENCH / "bench_cache"

CLIP = CACHE / "mbc_180s.wav"
SECONDS = 60          # how much of the clip to inject
IDLE_AFTER = 15       # stop collecting this many seconds after playback ends


def find_cable_inputs() -> list[int]:
    """Playback devices that feed the virtual cable, best-first."""
    import sounddevice as sd
    scored = []
    for i, d in enumerate(sd.query_devices()):
        name = d["name"].lower()
        if d["max_output_channels"] == 0:
            continue
        for prio, frag in enumerate(("cable input", "speakers (vb-audio",
                                     "cable in")):
            if frag in name:
                scored.append((prio, i, d["name"]))
                break
    scored.sort()
    return [i for _, i, _ in scored]


def play_to_cable(audio: np.ndarray, rate: int):
    import sounddevice as sd
    last = None
    for dev in find_cable_inputs():
        try:
            sd.play(audio, rate, device=dev)
            print(f"[t] playing clip on device {dev}")
            sd.wait()
            return
        except Exception as e:
            last = e
            print(f"[t] device {dev} unavailable ({e}); trying next")
    raise RuntimeError(f"no cable playback device could be opened: {last}")


def load_clip_48k() -> tuple[np.ndarray, int]:
    import av
    import soxr
    r = av.AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    with av.open(str(CLIP)) as c:
        for frame in c.decode(audio=0):
            if frame.time and frame.time > SECONDS:
                break
            for rf in r.resample(frame):
                a = rf.to_ndarray()
                chunks.append(a.mean(axis=0) if a.ndim > 1 else a)
    audio16 = np.concatenate(chunks).astype(np.float32)
    audio48 = soxr.resample(audio16, 16000, 48000)
    return audio48, 48000


async def collector(port_open: asyncio.Event, results: list, stop: asyncio.Event):
    import aiohttp
    full = json.loads((BENCH.parent / "server" / "config.json")
                      .read_text(encoding="utf-8"))
    srv = full["server"]
    cf = full.get("cloudflare") or {}
    host = srv.get("public_host")
    url = f"wss://{host}/ws"
    headers = {"Authorization": f"Bearer {srv['access_token']}"}
    if cf.get("client_id"):
        headers["CF-Access-Client-Id"] = cf["client_id"]
        headers["CF-Access-Client-Secret"] = cf["client_secret"]
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(url, headers=headers,
                                heartbeat=None) as ws:
            await ws.send_json({"type": "hello", "target_lang": "eng_Latn"})
            port_open.set()
            while not stop.is_set():
                try:
                    msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                if msg.type != aiohttp.WSMsgType.TEXT:
                    break
                ev = msg.json()
                if ev.get("type") == "subtitle":
                    ev["_recv_mono"] = time.monotonic()
                    results.append(ev)


async def main():
    import sounddevice as sd
    results: list = []
    port_open = asyncio.Event()
    stop = asyncio.Event()

    collect_task = asyncio.create_task(
        collector(port_open, results, stop))
    await port_open.wait()
    print("[t] collector connected through the tunnel")

    audio48, rate = load_clip_48k()

    # probe each cable playback device with an 8s slice; the right one is
    # whichever the server actually hears (this box has several VB-Cables)
    def play_mono(dev: int, mono: np.ndarray, rate: int):
        """Play mono into a device, replicating to all its channels (the
        16-ch VB-Cable device rejects mono streams)."""
        ch = int(sd.query_devices(dev)["max_output_channels"])
        data = np.ascontiguousarray(np.tile(mono[:, None], (1, ch)))
        sd.play(data, rate, device=dev)

    probe_len = int(rate * 8)
    best_dev = best_rate = None
    for dev in find_cable_inputs():
        print(f"[t] probing device {dev}…")
        try:
            drate = int(sd.query_devices(dev)["default_samplerate"])
            clip = audio48 if drate == 48000 else \
                __import__("soxr").resample(audio48, 48000, drate)
            play_mono(dev, clip[:drate * 8], drate)
            await asyncio.to_thread(sd.wait)
        except Exception as e:
            print(f"[t]   device {dev} unavailable: {type(e).__name__}")
            continue
        before = len(results)
        await asyncio.sleep(10)
        if len(results) > before:
            print(f"[t]   device {dev} @ {drate}Hz produces subtitles — using it")
            best_dev, best_rate = dev, drate
            break
        print(f"[t]   device {dev}: no subtitles reached the pipeline")
    if best_dev is None:
        raise RuntimeError("no cable playback device feeds the pipeline — "
                           "check the web client's output device setting")

    print(f"[t] injecting {SECONDS}s of clip into the virtual cable @ "
          f"{best_rate}Hz (device {best_dev})")
    clip = audio48 if best_rate == 48000 else \
        __import__("soxr").resample(audio48, 48000, best_rate)
    play_mono(best_dev, clip, best_rate)
    # sd.wait() would block this event loop for the whole playback — the
    # collector would miss the server's heartbeat pings and get dropped
    await asyncio.to_thread(sd.wait)
    print("[t] playback finished — draining final utterances…")
    await asyncio.sleep(IDLE_AFTER)
    stop.set()
    await collect_task

    finals = [e for e in results if not e.get("partial")]
    partials = [e for e in results if e.get("partial")]
    rows = []
    for e in finals:
        st = e.get("stage_ts") or {}
        if not st.get("vad"):
            continue
        recv = e["_recv_mono"]
        rows.append({
            "asr": st["asr"] - st["vad"],
            "mt": st["trans"] - st["asr"],
            "send": (st.get("sent") or st["trans"]) - st["trans"],
            "tunnel": recv - (st.get("sent") or st["trans"]),
            "total": recv - st["vad"],
        })

    def stat(key):
        vals = sorted(r[key] for r in rows if r.get(key) is not None)
        if not vals:
            return "—"
        return f"p50 {vals[(len(vals)-1)//2]:.2f}s  p95 {vals[min(len(vals)-1, int(len(vals)*0.95))]:.2f}s  max {vals[-1]:.2f}s"

    print(f"\n=== {len(finals)} finals, {len(partials)} partials (skipped on Groq) ===")
    print(f"ASR  (Groq whisper-large-v3-turbo): {stat('asr')}")
    print(f"MT   (Azure Translator):            {stat('mt')}")
    print(f"server send:                        {stat('send')}")
    print(f"Cloudflare tunnel -> viewer:        {stat('tunnel')}")
    print(f"TOTAL (utterance closed -> viewer): {stat('total')}")
    (BENCH / "bench_out" / "tunnel_measure.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in results),
        encoding="utf-8")
    print(f"raw events: bench/bench_out/tunnel_measure.jsonl")


if __name__ == "__main__":
    asyncio.run(main())
