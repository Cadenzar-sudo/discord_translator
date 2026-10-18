"""Offline regression tests for the Oct 18 audit fixes — no models, no server.

1. VAD hysteresis: frames in the off..on probability band keep an open
   utterance alive (off_threshold used to be dead — ANY frame below
   on_threshold counted as silence, closing finals during soft speech).
2. Language pin guard: cloud engines report constant confidence (0.8), so a
   single differing detection must not flip a pinned speaker; two
   consecutive ones (or one >= 0.90) must. Unmappable codes never pin.
3. Groq helpers: CJK-aware prompt cap, language-name mapping.
4. /audio frame parser: new uid+name format and the legacy fallback.

    <server venv>/python test_vad_hysteresis.py
"""

import queue
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SERVER = Path(__file__).resolve().parent.parent / "server"
sys.path.insert(0, str(SERVER))

import numpy as np  # noqa: E402

from segmentation import TARGET_FPS, VadSegmenter  # noqa: E402
from pipeline import PipelineWorker  # noqa: E402
from groq_asr import GroqASR, _cap_prompt  # noqa: E402
from sub_server import SubtitleServer  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results: list[tuple[bool, str, str]] = []


def check(name: str, ok: bool, detail: str = ""):
    results.append((ok, name, detail))
    print(f"[{PASS if ok else FAIL}] {name}" + (f" — {detail}" if detail else ""))


# same expressions run() derives from the default config (chat profile)
MIN_SPEECH = max(1, int(250 / 1000 * TARGET_FPS))          # 7 frames
MIN_SILENCE = max(1, int(480 / 1000 * TARGET_FPS))         # 15
MAX_FRAMES = int(9.0 * TARGET_FPS)                         # 281
PAD = int(120 / 1000 * TARGET_FPS)                         # 3
PARTIAL_MIN = max(1, int(1.2 * TARGET_FPS))                # 37
PARTIAL_EVERY = max(1, int(2.0 * TARGET_FPS))              # 62
WINDOW = 512


def make_segmenter() -> VadSegmenter:
    seg = VadSegmenter(queue.Queue(), queue.Queue(), {}, threading.Event())
    seg._vad = SimpleNamespace(reset=lambda: None)  # real one loads in run()
    seg._reset_segment_state()
    return seg


def step_n(seg: VadSegmenter, prob: float, n: int, win=None):
    win = win if win is not None else np.zeros(WINDOW, dtype=np.float32)
    for _ in range(n):
        seg._step(win, prob, 0.5, 0.35, MIN_SPEECH, MIN_SILENCE, MAX_FRAMES,
                  PAD, PARTIAL_MIN, PARTIAL_EVERY)


def drained(seg: VadSegmenter) -> list[dict]:
    out = []
    while True:
        try:
            out.append(seg.seg_q.get_nowait())
        except queue.Empty:
            return out


# ------------------------------------------------------------- 1: hysteresis
def test_hysteresis():
    seg = make_segmenter()
    step_n(seg, 0.9, 10)          # opens the utterance
    step_n(seg, 0.42, 16)         # mid band: old code closed here (15 frames)
    check("hysteresis: mid band keeps the utterance open",
          seg._speaking and seg._silence_frames == 0,
          f"speaking={seg._speaking} silence={seg._silence_frames}")
    step_n(seg, 0.1, 15)          # real silence: closes (15 >= min_silence)
    finals = [d for d in drained(seg) if not d.get("partial")]
    check("hysteresis: silence after a mid-band stretch emits one final",
          len(finals) == 1, f"finals={len(finals)}")

    seg = make_segmenter()
    step_n(seg, 0.42, 20)         # mid band alone never starts an utterance
    step_n(seg, 0.1, 10)
    d = drained(seg)
    check("hysteresis: mid band alone does not open an utterance",
          not seg._speaking and not d, f"speaking={seg._speaking} items={len(d)}")

    seg = make_segmenter()
    step_n(seg, 0.42, MAX_FRAMES + 20)  # endless soft noise: capped, discarded
    d = drained(seg)
    check("hysteresis: tentative soft stretch is capped, not emitted",
          not seg._speaking and seg._cur == [] and not d,
          f"cur={len(seg._cur)} items={len(d)}")

    seg = make_segmenter()
    step_n(seg, 0.9, 10)
    step_n(seg, 0.42, 3)          # soft tail counts as speech, not silence
    step_n(seg, 0.1, 15)
    finals = [d for d in drained(seg) if not d.get("partial")]
    want = (10 + 3 + 15 - (MIN_SILENCE - PAD)) * WINDOW
    got = len(finals[0]["audio"]) if finals else 0
    check("hysteresis: soft tail is kept, trailing silence trimmed to padding",
          got == want, f"got={got} want={want}")


# ------------------------------------------------------- 2: language pinning
def make_worker() -> PipelineWorker:
    cfg = {"min_text_len": 2, "default_target_lang": "eng_Latn",
           "callout_glossary": {}, "initial_prompts": {}, "groq": {},
           "beam_size": 1, "cpu_threads": 4, "asr_engine": "groq",
           "backlog_keep": 3, "backlog_max": 12,
           "whisper_model": "large-v3-turbo"}
    return PipelineWorker(cfg, queue.Queue(), SimpleNamespace(
        distinct=lambda: set(), viewer_count=lambda: 0), lambda ev: None)


def pin_ko(w: PipelineWorker, sid: str):
    for _ in range(4):  # 4 x 0.8 = 3.2 vote weight: locks the pin
        w._record_detection(sid, "ko", 0.8)


def test_pin_guard():
    sid = "s1"
    w = make_worker()
    pin_ko(w, sid)
    w._record_detection(sid, "en", 0.8)   # one Groq-style misread...
    check("pin guard: single 0.8 detection does not flip the pin",
          w._pinned.get(sid) == "ko", f"pin={w._pinned.get(sid)}")
    w._record_detection(sid, "en", 0.8)   # ...confirmed by a second
    check("pin guard: two consecutive 0.8 detections flip the pin",
          w._pinned.get(sid) == "en", f"pin={w._pinned.get(sid)}")

    w = make_worker()
    pin_ko(w, sid)
    w._record_detection(sid, "en", 0.95)  # near-certain: flips at once
    check("pin guard: a >= 0.90 detection flips immediately",
          w._pinned.get(sid) == "en", f"pin={w._pinned.get(sid)}")

    w = make_worker()
    pin_ko(w, sid)
    w._record_detection(sid, "en", 0.8)
    w._record_detection(sid, "ko", 0.8)   # agreement resets the streak
    w._record_detection(sid, "en", 0.8)
    check("pin guard: agreement in between resets the flip streak",
          w._pinned.get(sid) == "ko", f"pin={w._pinned.get(sid)}")

    w = make_worker()
    pin_ko(w, sid)
    w._record_detection(sid, "cantonese", 0.99)  # unmappable: ignored
    check("pin guard: unmappable language is never pinned or voted",
          w._pinned.get(sid) == "ko"
          and all(l == "ko" for l, _, _ in w._lang_hist.get(sid, [])),
          f"pin={w._pinned.get(sid)} hist={[l for l, _, _ in w._lang_hist.get(sid, [])]}")


# ------------------------------------------------------- 3: Groq ASR helpers
def test_groq_helpers():
    ko = "게임 음성 채팅. " * 20
    en = "game voice chat. " * 20
    check("groq: CJK prompt capped at 170 chars", len(_cap_prompt(ko)) == 170,
          f"len={len(_cap_prompt(ko))}")
    check("groq: latin prompt capped at 224 chars",
          len(_cap_prompt(en)) == 224, f"len={len(_cap_prompt(en))}")
    check("groq: short prompt untouched", _cap_prompt("hello") == "hello")
    g = GroqASR("test-key")
    check("groq: language names map to codes",
          g._lang_code("Korean") == "ko" and g._lang_code("English") == "en")
    check("groq: unmappable name -> None (not the raw name)",
          g._lang_code("Cantonese") is None, f"got={g._lang_code('Cantonese')!r}")
    check("groq: 2-letter passthrough", g._lang_code("ko") == "ko")


# -------------------------------------------------------- 4: /audio frames
def frame(uid: str, name: str, samples: int) -> bytes:
    ub, nb = uid.encode(), name.encode()
    return (len(ub).to_bytes(2, "little") + ub
            + len(nb).to_bytes(2, "little") + nb
            + np.zeros(samples, dtype="<f4").tobytes())


def test_frame_parser():
    p = SubtitleServer._parse_audio_frame
    r = p(frame("1492870711436640383", "Joell", 1600))
    check("frame: uid+name format parses",
          r is not None and r[0] == "1492870711436640383" and r[1] == "Joell"
          and len(r[2]) == 1600,
          f"got={None if r is None else (r[0], r[1], len(r[2]))}")
    legacy = (len(b"GapTest").to_bytes(2, "little") + b"GapTest"
              + np.ones(1600, dtype="<f4").tobytes())
    r = p(legacy)
    check("frame: legacy name-only frame falls back (uid == name)",
          r is not None and r[0] == "GapTest" and r[1] == "GapTest"
          and len(r[2]) == 1600 and float(r[2][0]) == 1.0,
          f"got={None if r is None else (r[0], r[1], len(r[2]))}")
    check("frame: garbage rejected", p(b"\x01\x02\x03") is None
          and p(b"") is None)
    bad_name = (len(b"x" * 200).to_bytes(2, "little") + b"x" * 200
                + np.zeros(1600, dtype="<f4").tobytes())
    check("frame: oversized name rejected", p(bad_name) is None)


def main():
    test_hysteresis()
    test_pin_guard()
    test_groq_helpers()
    test_frame_parser()
    bad = [r for r in results if not r[0]]
    print(f"\n=== {len(results) - len(bad)}/{len(results)} passed ===")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
