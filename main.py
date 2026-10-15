"""Discord voice translator — entry point.

Live mode (default):   python main.py
Find the cable device: python main.py --list-devices
Offline pipeline test: python main.py --test-file sample.wav
Headless (console):    python main.py --no-overlay
"""

import argparse
import importlib.util
import os
import queue
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np  # noqa: E402  (used by the debug audio dumper)

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

from config import apply_profile, load_config  # noqa: E402

TARGET_RATE = 16000


def add_cuda_dll_dirs():
    """Expose cuBLAS/cuDNN DLLs installed via pip (nvidia-*-cu12) to CTranslate2.

    os.add_dll_directory() alone is not enough: CTranslate2 loads cuBLAS with a
    plain LoadLibrary, which only consults PATH — so do both.
    """
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
            # Pascal cards report int8_float32 (no efficient fp16); RTX gets float16.
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


class PipelineWorker(threading.Thread):
    """seg_q (utterances) -> whisper -> guards -> translate -> result_q."""

    def __init__(self, cfg, seg_q, result_q, paused_event):
        super().__init__(daemon=True, name="PipelineWorker")
        self.cfg = cfg
        self.seg_q = seg_q
        self.result_q = result_q
        self.paused = paused_event
        self.stop_event = threading.Event()
        self.transcriber = None
        self.translator = None
        self.engine = cfg["engine"]
        # Rolling language vote: short utterances misdetect often, so once one
        # language dominates recent speech we pin it instead of re-guessing.
        # Every 4th utterance runs a detection "audit" so a genuine language
        # switch (e.g. a Japanese-speaking friend joins) is picked up.
        self._lang_hist: deque = deque(maxlen=12)
        self._pinned_lang: str | None = None
        self._detect_counter = 0
        self._n_filtered = 0

    def _vote_language(self) -> str | None:
        """Dominant recent language (weighted by duration x confidence), or None."""
        total = {}
        for lang, prob, dur in self._lang_hist:
            total[lang] = total.get(lang, 0.0) + dur * prob
        if not total:
            return None
        lang, weight = max(total.items(), key=lambda kv: kv[1])
        if weight >= 3.0 and weight >= 0.55 * sum(total.values()):
            return lang
        return None

    def _next_language(self) -> str | None:
        self._detect_counter += 1
        if self.cfg.get("language"):
            return self.cfg["language"]
        if self._pinned_lang is None or self._detect_counter % 4 == 0:
            return None  # auto-detect
        return self._pinned_lang

    def _record_detection(self, lang: str, prob: float):
        if self.cfg.get("language") or self.cfg.get("translate_only"):
            return  # pinned or filter mode: no voting
        if self._pinned_lang is None:
            self._lang_hist.append((lang, prob, 1.0))
            locked = self._vote_language()
            if locked and locked != self._pinned_lang:
                self._pinned_lang = locked
                print(f"[worker] Language locked: {locked}")
        elif lang != self._pinned_lang and prob >= 0.75:
            print(f"[worker] Language switched: {self._pinned_lang} -> {lang}")
            self._pinned_lang = lang
            self._lang_hist.clear()
            self._lang_hist.append((lang, prob, 1.0))

    def load_models(self, device, compute_type):
        from transcriber import Transcriber
        print(f"[init] Loading Whisper '{self.cfg['whisper_model']}' "
              f"({device}, {compute_type}) — first run downloads the model...")
        t0 = time.time()
        self.transcriber = Transcriber(
            self.cfg["whisper_model"], device, compute_type,
            cpu_threads=int(self.cfg["cpu_threads"]),
            beam_size=int(self.cfg["beam_size"]))
        print(f"[init] Whisper ready in {time.time() - t0:.1f}s")

        if self.engine == "nllb":
            from translator import NllbTranslator
            self.translator = NllbTranslator(
                self.cfg["nllb_ct2_repo"], self.cfg["nllb_tokenizer_repo"],
                device, compute_type, self.cfg["target_lang"])
        elif self.cfg["target_lang"] != "eng_Latn":
            print("[init] Warning: engine=whisper can only translate to English; "
                  "target_lang is ignored (switch engine to nllb for other targets).")
        print(f"[init] Engine: {self.engine}. Ready — speak in Discord!")

    def run(self):
        from transcriber import is_acceptable
        from translator import apply_glossary, apply_names, whisper_to_nllb

        pending: list[dict] = []
        while not self.stop_event.is_set():
            item = self._take_next(pending)
            if item is None:
                continue  # queue timeout
            if item.get("eof"):
                try:
                    self.result_q.put_nowait(None)
                except queue.Full:
                    pass
                continue
            if self.paused.is_set():
                continue
            seg, uid, partial = item["audio"], item["uid"], item["partial"]

            t0 = time.time()
            dur = len(seg) / TARGET_RATE
            # with translate_only, language ID must stay per-utterance: the voter
            # can lock onto the majority language and then Korean gets decoded
            # as (say) English and dropped by the filter — the reported bug
            only = self.cfg.get("translate_only")
            lang_arg = None if only else self._next_language()
            prompt = (self.cfg.get("initial_prompts") or {}).get(lang_arg) \
                if lang_arg else None
            prof = self.cfg.get("profiles", {}).get(self.cfg.get("profile", "chat"), {})
            add = (prof.get("prompt_add") or {}).get(lang_arg, "")
            if add:
                prompt = f"{prompt} {add}" if prompt else add
            try:
                text, seg_texts, info = self.transcriber.transcribe(
                    seg, translate=(self.engine == "whisper"),
                    language=lang_arg, initial_prompt=prompt)
            except Exception as e:
                print(f"[worker] Transcription error: {e}")
                continue
            detected = info.language or "?"
            self._record_detection(detected,
                                   getattr(info, "language_probability", 0.0))
            if self.cfg.get("debug_save_audio"):
                self._dump_audio(seg, uid, partial, detected)
            only = self.cfg.get("translate_only")
            if only and detected != only:
                prob = getattr(info, "language_probability", 0.0)
                # drop only confidently-foreign utterances; borderline detections
                # pass through — losing real Korean is worse than an extra line
                if prob >= float(self.cfg.get("translate_only_min_prob", 0.55)):
                    self._n_filtered += 1
                    if self._n_filtered == 1 or self._n_filtered % 20 == 0:
                        print(f"[worker] translate_only={only}: dropped "
                              f"{self._n_filtered} non-{only} utterance(s) "
                              f"(last: {detected} @ {prob:.2f})")
                    continue
            if not is_acceptable(text, info, dur, int(self.cfg["min_text_len"])):
                continue

            lang = detected
            if self.engine == "whisper":
                result = {"original": None, "translated": text, "lang": lang}
            else:
                src = whisper_to_nllb(lang)
                if lang == "en":
                    result = {"original": text, "translated": text, "lang": lang}
                elif src is None:
                    print(f"[worker] No NLLB mapping for '{lang}', showing raw text")
                    result = {"original": text, "translated": text, "lang": lang}
                else:
                    rules = (self.cfg.get("callout_glossary") or {}).get(lang)
                    glossed = ([apply_glossary(s, rules) for s in seg_texts]
                               if rules else None)
                    if glossed and all(g is not None for g in glossed):
                        translated = " ".join(g for g in glossed if g)  # pure callout: no NLLB hop
                    else:
                        names = (rules or {}).get("names")
                        mt_texts = [apply_names(s, names) for s in seg_texts] if names else seg_texts
                        try:
                            translated = self.translator.translate(mt_texts, src)
                        except Exception as e:
                            print(f"[worker] Translation error: {e}")
                            continue
                    result = {"original": text, "translated": translated,
                              "lang": lang}
            result.update({"ts": time.time(), "id": uid, "partial": partial,
                           "latency": time.time() - t0})
            try:
                self.result_q.put_nowait(result)
            except queue.Full:
                pass

    def _take_next(self, pending: list) -> dict | None:
        """Pop the next segment to process, collapsing stale versions: for one
        utterance only the newest partial is worth decoding, and a final makes
        every queued partial of that utterance obsolete."""
        if not pending:
            try:
                pending.append(self.seg_q.get(timeout=0.5))
            except queue.Empty:
                return None
        while True:
            try:
                pending.append(self.seg_q.get_nowait())
            except queue.Empty:
                break
        item = pending.pop(0)
        if item.get("partial"):
            same = [p for p in pending if p.get("uid") == item["uid"]]
            if same:
                finals = [p for p in same if not p.get("partial")]
                chosen = finals[0] if finals else same[-1]  # final wins, else newest partial
                for i, p in enumerate(pending):
                    if p is chosen:  # by identity: dicts hold ndarrays, == would explode
                        del pending[i]
                        break
                item = chosen
            # drop every remaining version of this uid: superseded
            pending[:] = [p for p in pending if p.get("uid") != item["uid"]]
        return item

    def _dump_audio(self, seg, uid, partial, detected: str):
        """Save what the app actually hears — for diagnosing capture problems."""
        import wave
        d = APP_DIR / "debug_audio"
        d.mkdir(exist_ok=True)
        name = f"{time.strftime('%H%M%S')}_u{uid}{'p' if partial else ''}_{detected}.wav"
        try:
            with wave.open(str(d / name), "wb") as w:
                w.setnchannels(1)
                w.setsampwidth(2)
                w.setframerate(TARGET_RATE)
                w.writeframes((np.clip(seg, -1, 1) * 32767).astype(np.int16).tobytes())
        except Exception as e:
            print(f"[worker] audio dump failed: {e}")

    def stop(self):
        self.stop_event.set()


def fmt_result(r: dict) -> str:
    ts = time.strftime("%H:%M:%S", time.localtime(r["ts"]))
    tag = f"#{r['id']}{'~' if r.get('partial') else ''} " if r.get("id") is not None else ""
    orig = f"\n    {r['lang']}: {r['original']}" if r.get("original") else ""
    return f"[{ts}] {tag}({r['lang']}) {r['translated']}   ({r['latency']:.1f}s){orig}"


# ------------------------------------------------------------------ modes
def run_live(cfg, no_overlay: bool):
    from segmentation import VadSegmenter

    audio_q: "queue.Queue" = queue.Queue(maxsize=400)
    seg_q: "queue.Queue" = queue.Queue(maxsize=30)
    result_q: "queue.Queue" = queue.Queue(maxsize=100)
    paused = threading.Event()

    add_cuda_dll_dirs()
    device, compute = pick_device_and_compute(cfg)
    print(f"[init] Device: {device} ({compute})")

    worker = PipelineWorker(cfg, seg_q, result_q, paused)
    worker.load_models(device, compute)
    worker.start()

    segmenter = VadSegmenter(audio_q, seg_q, cfg["vad"], paused)
    segmenter.start()

    service = None
    if not no_overlay:
        from overlay import OverlayService
        app_kwargs = {}
        from PyQt6.QtWidgets import QApplication
        qapp = QApplication.instance() or QApplication(sys.argv)

        def toggle_profile():
            cfg["profile"] = "game" if cfg.get("profile") != "game" else "chat"
            apply_profile(cfg)  # mutates cfg in place; segmenter reads it live
            worker.transcriber.beam_size = int(cfg["beam_size"])
            print(f"[mode] {cfg['profile'].upper()} mode "
                  f"(silence {cfg['vad']['min_silence_ms']}ms, beam {cfg['beam_size']})")
            service.win.flash(f"{cfg['profile'].upper()} MODE")

        startup_lang = cfg.get("language") or "ko"

        def toggle_language():
            if cfg.get("language"):
                cfg["language"] = None
                worker._pinned_lang = None       # resume auto-detection cleanly
                worker._lang_hist.clear()
                label = "AUTO"
            else:
                cfg["language"] = startup_lang
                if cfg.get("translate_only") == startup_lang:
                    cfg["language"] = None  # pinning would blind the filter
                    label = f"AUTO ({startup_lang.upper()}-ONLY)"
                else:
                    label = startup_lang.upper()
            from config import persist_key
            persist_key("language", cfg["language"])
            print(f"[lang] {label}")
            service.win.flash(f"LANGUAGE: {label}")

        service = OverlayService(result_q, cfg, paused,
                                 on_mode=toggle_profile, on_language=toggle_language)
        print(f"[init] Overlay running ({cfg.get('profile', 'chat')} mode, "
              f"language {cfg.get('language') or 'auto'}, Ctrl+Alt+G/L to switch). Hotkeys: "
              + " | ".join(f"{k}={v}" for k, v in
                           cfg["overlay"]["hotkeys"].items()))
    else:
        poller = threading.Thread(target=_console_poll, args=(result_q,), daemon=True)
        poller.start()

    from audio_capture import AudioCapture
    capture = AudioCapture(cfg["audio_device"], audio_q,
                           capture_rate=int(cfg["capture_sample_rate"] or 0))
    capture.start()

    try:
        if service is not None:
            qapp.exec()
        else:
            while True:
                time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        print("\n[exit] Shutting down...")
        worker.stop()
        segmenter.stop()
        capture.stop()


def _console_poll(result_q):
    while True:
        r = result_q.get()
        if r is None:
            continue
        print(fmt_result(r))


def run_test_file(cfg, path: str):
    """Feed an audio file through VAD -> Whisper -> translation, print results."""
    import av
    import numpy as np
    from segmentation import VadSegmenter

    add_cuda_dll_dirs()
    device, compute = pick_device_and_compute(cfg)
    print(f"[test] Device: {device} ({compute})")
    print(f"[test] Decoding {path} ...")

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
        print("[test] No audio decoded from file")
        return
    audio = np.concatenate(chunks)
    print(f"[test] {len(audio) / TARGET_RATE:.1f}s of audio @16 kHz mono")

    audio_q, seg_q, result_q = queue.Queue(), queue.Queue(), queue.Queue()
    paused = threading.Event()
    worker = PipelineWorker(cfg, seg_q, result_q, paused)
    worker.load_models(device, compute)
    worker.start()
    segmenter = VadSegmenter(audio_q, seg_q, cfg["vad"], paused)
    segmenter.start()

    # feed in ~1 s blocks; mark end-of-source so the segmenter flushes
    block = TARGET_RATE
    for i in range(0, len(audio), block):
        audio_q.put(audio[i:i + block])
    segmenter.source_done.set()

    print("[test] Results:")
    count = 0
    try:
        while True:
            try:
                r = result_q.get(timeout=120)
            except queue.Empty:
                print("[test] Timed out waiting for results")
                break
            if r is None:
                break
            print("  " + fmt_result(r))
            count += 1
    except KeyboardInterrupt:
        pass
    worker.stop()
    segmenter.stop()
    print(f"[test] Done — {count} segment(s) translated")


def run_list_devices():
    from audio_capture import list_input_devices
    devs = list_input_devices()
    if not devs:
        print("No input (recording) devices found.")
        return
    print("Input devices:")
    for d in devs:
        star = " *DEFAULT*" if d["is_default"] else ""
        print(f"  [{d['index']}] {d['name']}  "
              f"({d['channels']}ch @ {d['default_rate']} Hz, {d['hostapi']}){star}")
    print("\nSet audio_device in config.json to part of the name you want "
          "(e.g. \"CABLE Output\").")


def main():
    ap = argparse.ArgumentParser(description="Real-time Discord voice translator overlay")
    ap.add_argument("--list-devices", action="store_true", help="list recording devices and exit")
    ap.add_argument("--test-file", metavar="PATH", help="translate an audio file offline and exit")
    ap.add_argument("--no-overlay", action="store_true", help="print translations to console only")
    ap.add_argument("--engine", choices=["nllb", "whisper"], help="translation engine")
    ap.add_argument("--whisper-model", help="faster-whisper model name/size")
    ap.add_argument("--device", choices=["auto", "cuda", "cpu"])
    ap.add_argument("--compute-type",
                    choices=["auto", "float16", "int8_float16", "int8_float32", "int8"])
    ap.add_argument("--audio-device", help="recording device name substring")
    ap.add_argument("--language", help='pin Whisper language, e.g. "ko" (default: auto-detect)')
    ap.add_argument("--target-lang", help="NLLB target code, e.g. eng_Latn")
    args = ap.parse_args()

    overrides = {
        "engine": args.engine, "whisper_model": args.whisper_model,
        "device": args.device, "compute_type": args.compute_type,
        "audio_device": args.audio_device, "target_lang": args.target_lang,
        "language": args.language,
    }
    cfg = load_config({k: v for k, v in overrides.items() if v is not None})
    apply_profile(cfg)

    # A language pin forces Whisper to decode EVERYTHING as that language, so
    # translate_only could never see anything else — drop the pin at runtime.
    if cfg.get("translate_only") and cfg.get("language") == cfg["translate_only"]:
        print(f"[init] translate_only={cfg['translate_only']} needs auto-detection; "
              f"ignoring the language pin for this session")
        cfg["language"] = None

    if args.list_devices:
        run_list_devices()
    elif args.test_file:
        run_test_file(cfg, args.test_file)
    else:
        run_live(cfg, no_overlay=args.no_overlay)


if __name__ == "__main__":
    main()
