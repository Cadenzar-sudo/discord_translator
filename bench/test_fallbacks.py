"""Fallback-chain tests: live cloud calls + mocked pipeline scenarios.

    python test_fallbacks.py            # from bench/, uses the server venv

Live part fires ONE real request per configured cloud engine using
bench_cache/test_ko.wav (16 kHz mono Korean, ~13 s): Groq, Azure Speech
free (F0), Azure Speech paid (S1), Azure Translator (free F0).
Mocked part drives the REAL PipelineWorker.run() loop with fake engines to
prove the ordering Groq -> Azure free -> Azure paid -> local whisper, and
Azure Translator -> Azure paid -> NLLB.
"""

import io
import json
import queue
import sys
import time
import wave
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
SERVER = BENCH.parent / "server"
sys.path.insert(0, str(SERVER))

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[str, str, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((PASS if ok else FAIL, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


def load_wav_float32(path: Path) :
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getframerate() == 16000, "expected 16k mono"
        import numpy as np
        raw = w.readframes(w.getnframes())
        return np.frombuffer(raw, np.int16).astype(np.float32) / 32767.0


# --------------------------------------------------------------- live cloud
def live_tests(cfg: dict):
    audio = load_wav_float32(BENCH / "bench_cache" / "test_ko.wav")

    # what load_models WOULD enable with this exact config (no model load)
    gq = cfg.get("groq") or {}
    azs = cfg.get("azure_speech") or {}
    azp = cfg.get("azure_speech_paid") or {}
    aztp = cfg.get("azure_translator_paid") or {}
    print(f"\n--- init plan from config.json ---")
    print(f"  Groq primary:           {'on' if gq.get('key') else 'off'}")
    print(f"  Azure Speech free:      {'on' if azs.get('key') and azs.get('region') else 'off'}")
    print(f"  Azure Speech paid:      {'on' if azp.get('key') and azp.get('region') else 'off'}")
    print(f"  Azure Translator paid:  {'on' if aztp.get('key') and aztp.get('region') else 'off'}"
          f"  (MT chain: Azure free -> NLLB)")

    if gq.get("key"):
        from groq_asr import GroqASR
        g = GroqASR(gq["key"], gq.get("model") or "whisper-large-v3-turbo")
        t0 = time.monotonic()
        text, segs, info = g.transcribe(audio, language="ko")
        dt = time.monotonic() - t0
        check("live: Groq transcribes ko", bool(text and text.strip()),
              f"{dt:.1f}s: {text[:60]!r}")

    for label, section in (("free F0", "azure_speech"), ("paid S1", "azure_speech_paid")):
        az = cfg.get(section) or {}
        if not (az.get("key") and az.get("region")):
            check(f"live: Azure Speech {label} configured", False, "no key in config.json")
            continue
        from azure_stt import AzureSTT
        stt = AzureSTT(az["key"], az["region"])
        t0 = time.monotonic()
        text, segs, info = stt.transcribe(audio, language="ko")
        dt = time.monotonic() - t0
        check(f"live: Azure Speech {label} transcribes ko",
              bool(text and text.strip()), f"{dt:.1f}s: {text[:60]!r}")

    az = cfg.get("azure") or {}
    if az.get("key"):
        from azure_translator import AzureTranslator
        tr = AzureTranslator(az["key"], az.get("region") or "",
                             az.get("endpoint") or "api.cognitive.microsofttranslator.com")
        t0 = time.monotonic()
        out = tr.translate_multi(["안녕하세요 친구들, 오늘 게임 재미있었어요"],
                                 "kor_Latn", ["eng_Latn"])
        dt = time.monotonic() - t0
        check("live: Azure Translator ko->en",
              bool(out.get("eng_Latn", "").strip()),
              f"{dt:.1f}s: {out.get('eng_Latn', '')[:60]!r}")

    # empty paid-translator key must be skipped by init (falsy), not crash
    check("config: azure_translator_paid empty -> skipped by init",
          not (aztp.get("key") and aztp.get("region")),
          "MT falls straight to NLLB when the free tier 429s")


# ------------------------------------------------------- mocked chain logic
class Recorder:
    """transcribe() returns a fixed healthy result, or raises 429 when ok=False."""

    def __init__(self, name: str, ok: bool):
        self.name, self.ok = name, ok
        self.calls = 0

    def transcribe(self, audio, **kw):
        self.calls += 1
        if not self.ok:
            raise RuntimeError("Error code: 429 - Rate limit reached")
        text = "안녕하세요 친구들, 오늘 게임 재미있었어요"
        return (text, [text], SimpleNamespace(
            language="ko", language_probability=0.9, avg_logprob=-0.2,
            no_speech_prob=0.0, compression_ratio=1.1))


def run_scenario(name, groq_ok: bool, free_ok: bool, paid_ok: bool,
                 expect_calls: dict):
    from pipeline import PipelineWorker

    cfg = {
        "language": "ko", "profile": "chat", "min_text_len": 2,
        "default_target_lang": "eng_Latn", "callout_glossary": {},
        "initial_prompts": {}, "debug_save_audio": False,
        "groq": {"skip_partials": True}, "backlog_keep": 3,
        "backlog_max": 12, "beam_size": 1, "cpu_threads": 4,
        "whisper_model": "large-v3-turbo",
    }
    seg_q: queue.Queue = queue.Queue()
    registry = SimpleNamespace(distinct=lambda: set(), viewer_count=lambda: 0)
    emitted: list[dict] = []
    w = PipelineWorker(cfg, seg_q, registry, emitted.append)
    w.groq = Recorder("groq", groq_ok)
    w.azure_stt = Recorder("azure free", free_ok)
    w.azure_stt_paid = Recorder("azure paid", paid_ok)
    w.azure = SimpleNamespace(translate_multi=lambda texts, src, targets: {
        t: f"[mt] {s}" for t, s in zip(targets, texts)})
    w.azure_paid = None
    w._groq_cooldown_until = 0.0
    local_calls = []
    local_ns = SimpleNamespace(transcribe=lambda audio, **kw: (
        "로컬 위스퍼 대체 텍스트입니다", ["로컬 위스퍼 대체 텍스트입니다"],
        SimpleNamespace(language="ko", language_probability=0.9,
                        avg_logprob=-0.2, no_speech_prob=0.0,
                        compression_ratio=1.1)))

    def fake_local_loader():
        local_calls.append(1)
        return local_ns

    w._load_local_transcriber = fake_local_loader

    n = 3  # three utterances: enough to see cooldown skip behaviour
    for i in range(n):
        seg_q.put({"speaker_id": 1, "speaker": "Tester", "uid": i,
                   "audio": __import__("numpy").zeros(16000, dtype="float32"),
                   "partial": False, "t_vad": time.monotonic()})
    w.start()
    deadline = time.monotonic() + 30
    while len(emitted) < n and time.monotonic() < deadline:
        time.sleep(0.05)
    w.stop_event.set()
    w.join(timeout=5)

    ok = len(emitted) == n
    detail = f"emitted {len(emitted)}/{n}; calls: groq={w.groq.calls} " \
             f"free={w.azure_stt.calls} paid={w.azure_stt_paid.calls} local={len(local_calls)}"
    for engine, want in expect_calls.items():
        got = (w.groq.calls if engine == "groq" else
               w.azure_stt.calls if engine == "free" else
               w.azure_stt_paid.calls if engine == "paid" else len(local_calls))
        if got != want:
            ok = False
            detail += f" | WANT {engine}={want}"
    if not all(e.get("translated") for e in emitted):
        ok = False
        detail += " | empty translations"
    check(name, ok, detail)


def main():
    cfg = json.loads((SERVER / "config.json").read_text(encoding="utf-8"))
    live_tests(cfg)

    print("\n--- mocked fallback chain (real PipelineWorker.run) ---")
    # 1. healthy Groq: everything else untouched
    run_scenario(
        "chain: Groq healthy -> Groq serves, no fallbacks touched",
        True, False, False,
        expect_calls={"groq": 3, "free": 0, "paid": 0, "local": 0})
    # 2. Groq 429: free Speech catches (1st item falls through same iteration,
    #    items 2-3 skip Groq via cooldown) — paid & local untouched
    run_scenario(
        "chain: Groq 429 -> Azure Speech free serves",
        False, True, False,
        expect_calls={"groq": 1, "free": 3, "paid": 0, "local": 0})
    # 3. Groq 429 + free exhausted: paid serves, local untouched
    run_scenario(
        "chain: Groq 429 + free 429 -> Azure Speech paid serves",
        False, False, True,
        expect_calls={"groq": 1, "free": 3, "paid": 3, "local": 0})
    # 4. everything cloud fails: local whisper catches
    run_scenario(
        "chain: all cloud fail -> local whisper serves",
        False, False, False,
        expect_calls={"groq": 1, "free": 3, "paid": 3, "local": 3})

    print("\n=== summary ===")
    bad = [r for r in results if r[0] == FAIL]
    for status, name, detail in results:
        print(f"  {status}  {name}")
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
