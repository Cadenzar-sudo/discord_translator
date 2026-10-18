"""Discord bot translator — server entry point.

Normal:      python main.py            (bot + subtitle WebSocket)
Offline:     python main.py --test-file clip.wav [--fast] [--speaker NAME]
             feeds a media file through the live pipeline as a fake speaker
             and broadcasts to connected overlay clients (no Discord needed).
"""

import argparse
import asyncio
import importlib.util
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from config import apply_profile, load_config  # noqa: E402

TARGET_RATE = 16000


def add_cuda_dll_dirs():
    """Expose cuBLAS/cuDNN DLLs installed via pip (nvidia-*-cu12) to CTranslate2.

    os.add_dll_directory() alone is not enough: CTranslate2 loads cuBLAS with a
    plain LoadLibrary, which only consults PATH — so do both. Windows-only:
    Linux containers get CUDA libs from pip wheels via LD_LIBRARY_PATH.
    """
    if sys.platform != "win32":
        return
    for pkg, sub in (("nvidia.cublas", "bin"), ("nvidia.cudnn", "bin")):
        try:
            spec = importlib.util.find_spec(pkg)
        except (ImportError, ModuleNotFoundError, ValueError):
            continue
        if spec and spec.submodule_search_locations:
            p = Path(list(spec.submodule_search_locations)[0]) / sub
            if p.is_dir():
                os.add_dll_directory(str(p))
                os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")


def pick_device_and_compute(cfg) -> tuple[str, str]:
    """Resolve device/compute_type: 'auto' probes CTranslate2's CUDA support."""
    want_dev, want_ct = cfg["device"], cfg["compute_type"]
    try:
        import ctranslate2 as ct2
        if want_dev == "auto":
            want_dev = "cuda" if ct2.get_cuda_device_count() > 0 else "cpu"
        if want_ct == "auto":
            supported = ct2.get_supported_compute_types(want_dev)
            # Pascal cards report int8_float32 (no efficient fp16); Ampere+ gets fp16.
            for pref in (("int8_float16", "float16", "int8_float32", "int8")
                         if want_dev == "cuda" else ("int8", "float32")):
                if pref in supported:
                    want_ct = pref
                    break
            else:
                want_ct = supported[0] if supported else "int8"
    except Exception as e:
        print(f"[init] CUDA probe failed ({e}); using CPU")
        want_dev, want_ct = "cpu", "int8"
    return want_dev, want_ct


def decode_media(path: str):
    """Any audio/video file -> float32 mono 16 kHz numpy array (via PyAV)."""
    import av
    import numpy as np
    resampler = av.AudioResampler(format="s16", layout="mono", rate=TARGET_RATE)
    chunks = []
    with av.open(path) as container:
        for frame in container.decode(audio=0):
            for rf in resampler.resample(frame):
                arr = rf.to_ndarray()
                if arr.ndim > 1:
                    arr = arr.mean(axis=0)
                chunks.append(arr.astype(np.float32) / 32768.0)
    if not chunks:
        raise RuntimeError(f"no audio decoded from {path}")
    return np.concatenate(chunks)


async def feed_test_file(cfg, manager, worker, path: str, speaker: str,
                         realtime: bool):
    """Push a media file through as a fake speaker; returns when the pipeline
    has drained it (worker popped the eof sentinel)."""
    audio = await asyncio.to_thread(decode_media, path)
    print(f"[test] {len(audio) / TARGET_RATE:.1f}s of audio as speaker "
          f"'{speaker}' ({'realtime' if realtime else 'fast'})")
    fake_id, audio_q = manager.fake_speaker(speaker)

    drained = asyncio.Event()

    def on_drained(sid):
        if sid == fake_id:
            drained.set()

    worker.on_drained = on_drained

    block = TARGET_RATE // 2  # 0.5 s blocks
    start = time.monotonic()
    for i in range(0, len(audio), block):
        audio_q.put(audio[i:i + block])
        if realtime:
            target_t = start + (i + block) / TARGET_RATE
            delay = target_t - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
    manager.remove(fake_id, flush=True)  # flushes the VAD tail -> eof sentinel

    timeout = len(audio) / TARGET_RATE + 300 if realtime else 600
    try:
        await asyncio.wait_for(drained.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        print("[test] Pipeline did not drain in time")
    # give in-flight events a moment to reach connected viewers
    await asyncio.sleep(1.5)


class ShipperSink:
    """Bridges /audio shipper frames to the SpeakerManager. The server tracks
    which speaker uids each shipper connection created and calls disconnect()
    with them when the connection drops, so a vanished shipper flushes exactly
    its own per-speaker VAD sessions."""

    def __init__(self, manager, default_speaker: str):
        self.manager = manager
        self.default_speaker = default_speaker

    def push(self, uid, name: str, block):
        self.manager.push(uid, name, block)

    def disconnect(self, uids):
        for uid in uids:
            self.manager.remove(uid, flush=True)


async def amain(cfg, args):
    from pipeline import PipelineWorker, TargetRegistry
    from speakers import SpeakerManager
    from sub_server import SubtitleServer

    loopback = cfg.get("source", "loopback") == "loopback" and not args.test_file
    ws_mode = cfg.get("source") == "ws" and not args.test_file
    if not loopback and not ws_mode and not args.test_file:
        print(f"[init] Unknown source {cfg.get('source')!r}. Use \"loopback\" "
              "(capture a normal Discord client via VB-Cable) or \"ws\" "
              "(receive audio from the voice-bot sidecar / audio_shipper.py), "
              "or --test-file for offline testing.")
        raise SystemExit(2)

    add_cuda_dll_dirs()
    device, compute = pick_device_and_compute(cfg)
    print(f"[init] Device: {device} ({compute})")

    seg_q: "queue.Queue" = queue.Queue(maxsize=60)
    paused = threading.Event()
    manager = SpeakerManager(cfg, seg_q, paused)
    registry = TargetRegistry()
    state = {"ready": False}
    worker = None  # bound after model load; set_mode guards on it

    def status_provider():
        return {"mode": cfg.get("profile", "chat"), "channel": "loopback",
                "language": cfg.get("language"),
                "speakers": manager.names(),
                "viewer_count": registry.viewer_count(),
                "audio": server.audio_client,
                "model": cfg.get("whisper_model"), "device": device,
                "compute_type": compute, "ready": state["ready"], "uptime_s": 0}

    def set_mode(profile: str):
        """Called on the loop thread when a viewer toggles chat/game."""
        cfg["profile"] = profile
        apply_profile(cfg)
        # worker is None until models finish loading; a viewer connecting
        # during that window must not crash the handler
        if worker is not None and worker.transcriber:
            worker.transcriber.beam_size = int(cfg["beam_size"])
        print(f"[mode] {profile.upper()} mode (silence "
              f"{cfg['vad']['min_silence_ms']}ms, beam {cfg['beam_size']})")

    def set_language(lang):
        """Called on the loop thread when a viewer cycles the pinned language."""
        cfg["language"] = lang
        print(f"[lang] pinned language: {lang or 'AUTO'}")

    server = SubtitleServer(cfg, registry, status_provider,
                            on_set_mode=set_mode, on_set_language=set_language,
                            audio_sink=ShipperSink(
                                manager, cfg.get("speaker_name", "Discord"))
                            if ws_mode else None)
    await server.start()

    worker = PipelineWorker(cfg, seg_q, registry, server.thread_emit)
    await asyncio.to_thread(worker.load_models, device, compute)
    worker.start()
    state["ready"] = True

    capture = None
    if ws_mode:
        print("[init] Waiting for an audio shipper on /audio (voice-bot "
              "sidecar with per-speaker DAVE audio, or audio_shipper.py "
              "streaming the VB-Cable fallback)")
    elif loopback:
        # VB-Cable loopback: the whole voice channel as one mixed stream,
        # labelled with a single synthetic speaker
        from audio_capture import AudioCapture
        spk = cfg.get("speaker_name", "Discord")
        audio_q: "queue.Queue" = queue.Queue(maxsize=400)

        def pump():
            sid = f"loopback-{spk}"
            while True:
                block = audio_q.get()
                if block is None:
                    return
                manager.push(sid, spk, block)

        threading.Thread(target=pump, daemon=True, name="loopback-pump").start()
        capture = AudioCapture(cfg["audio_device"], audio_q,
                               capture_rate=int(cfg.get("capture_sample_rate") or 0))
        capture.start()
        print(f"[init] Loopback capture from '{cfg['audio_device']}' as "
              f"speaker '{spk}' — single mixed stream (no per-speaker labels)")
        dw = cfg.get("discord_web") or {}
        if dw.get("auto_join"):
            joiner_log = open(APP_DIR / "discord_web_joiner.log", "ab")
            joiner = await asyncio.create_subprocess_exec(
                sys.executable, "-u", str(APP_DIR / "discord_web_joiner.py"),
                cwd=str(APP_DIR), stdout=joiner_log, stderr=subprocess.STDOUT)
            state["joiner"] = joiner
            print("[init] Discord Web auto-joiner launched "
                  "(logs: discord_web_joiner.log)")

    async def loop_monitor():
        """Warn when the event loop stalls — stalls break WS heartbeats and
        viewers get dropped for missing pongs."""
        last = time.monotonic()
        while True:
            await asyncio.sleep(1)
            now = time.monotonic()
            lag = now - last - 1
            if lag > 0.5:
                print(f"[loop] event loop stalled {lag:.1f}s")
            last = now

    loop_task = asyncio.create_task(loop_monitor())

    try:
        if args.test_file:
            await feed_test_file(cfg, manager, worker, args.test_file,
                                 args.speaker or "Test", realtime=not args.fast)
            print("[test] Done.")
        else:
            await asyncio.Event().wait()  # run until Ctrl+C
    except asyncio.CancelledError:
        pass
    finally:
        print("\n[exit] Shutting down...")
        worker.stop()
        manager.stop_all()
        if capture is not None:
            capture.stop()
        loop_task.cancel()
        joiner = state.get("joiner")
        if joiner is not None:
            try:
                joiner.terminate()
            except Exception:
                pass
        try:
            # aiohttp cleanup can stall on lingering keep-alive sockets;
            # don't let that trap the whole process on exit
            await asyncio.wait_for(server.stop(), timeout=10.0)
        except (asyncio.TimeoutError, Exception) as e:
            print(f"[exit] web shutdown skipped ({e})")


def main():
    ap = argparse.ArgumentParser(description="Discord translator server")
    ap.add_argument("--test-file", metavar="PATH",
                    help="feed a media file as a fake speaker (no Discord)")
    ap.add_argument("--speaker", metavar="NAME", default="Test",
                    help="speaker name for --test-file (default: Test)")
    ap.add_argument("--fast", action="store_true",
                    help="with --test-file: no realtime pacing (accuracy runs)")
    ap.add_argument("--port", type=int, help="override server.port")
    ap.add_argument("--whisper-model", help="faster-whisper model name/size")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute-type",
                    choices=["auto", "float16", "int8_float16", "int8_float32", "int8"])
    ap.add_argument("--language", help='pin Whisper language for all speakers, e.g. "ko"')
    ap.add_argument("--profile", choices=["chat", "game"])
    args = ap.parse_args()

    overrides = {
        "whisper_model": args.whisper_model, "device": args.device,
        "compute_type": args.compute_type, "language": args.language,
        "profile": args.profile,
    }
    cfg = load_config({k: v for k, v in overrides.items() if v is not None})
    if args.port:
        cfg["server"]["port"] = args.port
    apply_profile(cfg)

    # library errors must always reach our log — some swallowed errors
    # fail silently otherwise
    import logging
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    try:
        asyncio.run(amain(cfg, args))
    except KeyboardInterrupt:
        pass


def _install_sigterm():
    """Containers stop with SIGTERM: turn it into the KeyboardInterrupt path
    so amain's cleanup (workers, server) actually runs."""
    import signal

    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)


if __name__ == "__main__":
    _install_sigterm()
    main()
