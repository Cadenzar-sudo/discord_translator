"""Pipeline worker: per-speaker utterances -> Whisper -> guards -> per-target
translation fan-out -> subtitle events handed to the WebSocket broadcaster.

Reworked from the desktop app's PipelineWorker (main.py): uid collapsing and
language voting are keyed by speaker, the Korean-only filter is replaced by
per-viewer target fan-out (each connected client declares its own language),
and every event carries per-stage timestamps for the latency benchmarks.
"""

# lazy annotations: TargetRegistry.set() shadows the builtin `set` in this
# class body, which breaks `-> set[str]` on Python <= 3.13 (3.14 defers)
from __future__ import annotations

import queue
import threading
import time
from collections import deque
from pathlib import Path

# Groq free tier: ~20 requests/minute. After a 429, serve utterances with the
# fallback chain for the rest of the minute, then probe Groq again — the
# window resets every minute, so this keeps Groq primary without hammering
# a rate-limited key (and without the latency of doomed attempts).
GROQ_RATELIMIT_COOLDOWN_S = 60.0
GROQ_ERROR_COOLDOWN_S = 15.0

import numpy as np

APP_DIR = Path(__file__).resolve().parent
TARGET_RATE = 16000


class TargetRegistry:
    """Which target languages connected viewers want (thread-safe). The
    pipeline fans each utterance out to the distinct set; the sub server owns
    registration."""

    def __init__(self):
        self._lock = threading.Lock()
        self._targets: dict[int, str] = {}

    def set(self, viewer_id: int, target_lang: str):
        with self._lock:
            self._targets[viewer_id] = target_lang

    def remove(self, viewer_id: int):
        with self._lock:
            self._targets.pop(viewer_id, None)

    def distinct(self) -> set[str]:
        with self._lock:
            return set(self._targets.values())

    def viewer_count(self) -> int:
        with self._lock:
            return len(self._targets)


def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * p))]


class PipelineWorker(threading.Thread):
    """seg_q (per-speaker utterances) -> whisper -> fan-out -> emit(event)."""

    def __init__(self, cfg, seg_q, registry: TargetRegistry, emit):
        super().__init__(daemon=True, name="PipelineWorker")
        self.cfg = cfg
        self.seg_q = seg_q
        self.registry = registry
        self.emit = emit  # callable(dict) -> None (thread-safe, non-blocking)
        self.on_drained = None  # optional callable(speaker_id) when eof passes
        self.stop_event = threading.Event()
        self.transcriber = None
        self.translator = None
        self.device = "cpu"
        self.compute_type = "int8"
        # Per-speaker language vote: short utterances misdetect often, so once
        # one language dominates a speaker's recent speech we pin it instead of
        # re-guessing. Every 4th utterance runs a detection "audit" so a genuine
        # language switch is picked up.
        self._lang_hist: dict = {}
        self._pinned: dict = {}
        self._counters: dict = {}
        self._recent: deque = deque(maxlen=60)  # (partial, latency_s) of finals

    # ------------------------------------------------------------- languages
    def _vote_language(self, sid) -> str | None:
        total = {}
        for lang, prob, dur in self._lang_hist.get(sid, ()):
            total[lang] = total.get(lang, 0.0) + dur * prob
        if not total:
            return None
        lang, weight = max(total.items(), key=lambda kv: kv[1])
        if weight >= 3.0 and weight >= 0.55 * sum(total.values()):
            return lang
        return None

    def _next_language(self, sid) -> str | None:
        if self.cfg.get("language"):
            return self.cfg["language"]
        self._counters[sid] = self._counters.get(sid, 0) + 1
        if self._pinned.get(sid) is None or self._counters[sid] % 4 == 0:
            return None  # auto-detect
        return self._pinned[sid]

    def _record_detection(self, sid, lang: str, prob: float):
        if self.cfg.get("language"):
            return  # globally pinned: no voting
        hist = self._lang_hist.setdefault(sid, deque(maxlen=12))
        pinned = self._pinned.get(sid)
        if pinned is None:
            hist.append((lang, prob, 1.0))
            locked = self._vote_language(sid)
            if locked and locked != pinned:
                self._pinned[sid] = locked
        elif lang != pinned and prob >= 0.75:
            self._pinned[sid] = lang
            hist.clear()
            hist.append((lang, prob, 1.0))

    # --------------------------------------------------------------- models
    def _load_local_transcriber(self):
        """Local faster-whisper, loaded lazily: with asr_engine=groq it only
        loads on the first Groq failure (fallback), keeping VRAM free."""
        if self.transcriber is not None:
            return self.transcriber
        from transcriber import Transcriber
        print(f"[init] Loading local Whisper '{self.cfg['whisper_model']}' "
              f"({self.device}, {self.compute_type}) as fallback...")
        self.transcriber = Transcriber(
            self.cfg["whisper_model"], self.device, self.compute_type,
            cpu_threads=int(self.cfg["cpu_threads"]),
            beam_size=int(self.cfg["beam_size"]))
        return self.transcriber

    def load_models(self, device, compute_type):
        from translator import NllbTranslator
        self.device, self.compute_type = device, compute_type
        self.transcriber = None  # local whisper: loaded lazily when needed
        self.groq = None
        self._groq_cooldown_until = 0.0  # monotonic: skip Groq until then
        self.azure_stt = None
        if self.cfg.get("asr_engine") == "groq":
            gq = self.cfg.get("groq") or {}
            if gq.get("key"):
                try:
                    from groq_asr import GroqASR
                    self.groq = GroqASR(gq["key"],
                                        gq.get("model") or
                                        "whisper-large-v3-turbo")
                    print(f"[init] ASR engine: Groq ({self.groq.model}) — "
                          f"local whisper loads only as fallback")
                except Exception as e:
                    print(f"[init] Groq init failed ({e}); using local whisper")
            else:
                print("[init] asr_engine=groq but groq.key is empty — using "
                      "local whisper (run setup.bat or set groq.key)")
        else:
            print("[init] ASR engine: local faster-whisper")
        # Azure Speech as the middle fallback: Groq quota/errors -> Azure
        # (needs the utterance language, so only used when it is known)
        azs = self.cfg.get("azure_speech") or {}
        if azs.get("key") and azs.get("region"):
            try:
                from azure_stt import AzureSTT
                self.azure_stt = AzureSTT(azs["key"], azs["region"])
                print("[init] Azure Speech fallback: on")
            except Exception as e:
                print(f"[init] Azure Speech init failed ({e})")
        self.azure_stt_paid = None
        azp = self.cfg.get("azure_speech_paid") or {}
        if azp.get("key") and azp.get("region"):
            try:
                from azure_stt import AzureSTT
                self.azure_stt_paid = AzureSTT(azp["key"], azp["region"])
                print("[init] Azure Speech PAID fallback: on (student credits)")
            except Exception as e:
                print(f"[init] Azure Speech paid init failed ({e})")
        if self.groq is None:
            self._load_local_transcriber()
        t0 = time.time()
        # NLLB is always loaded: fallback for Azure quota/outage/unsupported
        self.translator = NllbTranslator(
            self.cfg["nllb_ct2_repo"], self.cfg["nllb_tokenizer_repo"],
            device, compute_type)
        print(f"[init] MT fallback ready in {time.time() - t0:.1f}s")
        self.azure = None
        if self.cfg.get("engine") == "azure":
            az = self.cfg.get("azure") or {}
            if az.get("key"):
                try:
                    from azure_translator import AzureTranslator
                    self.azure = AzureTranslator(az["key"],
                                                 az.get("region") or "",
                                                 az.get("endpoint") or
                                                 "api.cognitive.microsofttranslator.com")
                    print("[init] Engine: Azure Translator (NLLB as fallback)")
                except Exception as e:
                    print(f"[init] Azure init failed ({e}); using NLLB")
            else:
                print("[init] engine=azure but azure.key is empty — using NLLB "
                      "(run setup.bat or set azure.key in config.json)")
        self.azure_paid = None
        azp = self.cfg.get("azure_translator_paid") or {}
        if azp.get("key") and azp.get("region"):
            try:
                from azure_translator import AzureTranslator
                self.azure_paid = AzureTranslator(azp["key"], azp["region"])
                print("[init] Azure Translator PAID fallback: on (student credits)")
            except Exception as e:
                print(f"[init] Azure paid init failed ({e})")
        if self.azure is None:
            print("[init] Engine: local NLLB")
        print("[init] Pipeline ready — waiting for speakers.")

    def stats(self) -> dict:
        finals = [l for p, l in self._recent if not p]
        partials = [l for p, l in self._recent if p]
        return {
            "utterances_final": len(finals),
            "utterances_partial": len(partials),
            "final_p50_s": round(_pct(finals, 0.50), 3),
            "final_p95_s": round(_pct(finals, 0.95), 3),
            "partial_p50_s": round(_pct(partials, 0.50), 3),
        }

    # ----------------------------------------------------------------- run
    def run(self):
        from transcriber import is_acceptable
        from translator import apply_glossary, apply_names, whisper_to_nllb

        pending: list[dict] = []
        while not self.stop_event.is_set():
            item = self._take_next(pending)
            if item is None:
                continue
            if item.get("eof"):
                if self.on_drained:
                    try:
                        self.on_drained(item.get("speaker_id"))
                    except Exception:
                        pass
                continue

            sid = item.get("speaker_id")
            speaker = item.get("speaker", "?")
            seg, uid, partial = item["audio"], item["uid"], item["partial"]
            t_vad = item.get("t_vad") or time.monotonic()

            t0 = time.monotonic()
            dur = len(seg) / TARGET_RATE
            lang_arg = self._next_language(sid)
            # domain prompts only in game mode: on non-gaming audio a long
            # gaming vocab prompt gets hallucinated into the transcript
            prompt = None
            if self.cfg.get("profile") == "game" and lang_arg:
                prompt = (self.cfg.get("initial_prompts") or {}).get(lang_arg)
                prof = self.cfg.get("profiles", {}).get(self.cfg.get("profile", "chat"), {})
                add = (prof.get("prompt_add") or {}).get(lang_arg, "")
                if add:
                    prompt = f"{prompt} {add}" if prompt else add
            # Groq free tier gates at ~20 requests/minute: partial subtitles
            # are skipped so finals always fit under the gate
            if self.groq is not None and partial and \
                    (self.cfg.get("groq") or {}).get("skip_partials", True):
                continue
            text = None
            groq_ready = self.groq is not None and \
                time.monotonic() >= self._groq_cooldown_until
            if groq_ready:
                try:
                    text, seg_texts, info = self.groq.transcribe(
                        seg, language=lang_arg, initial_prompt=prompt)
                    if self._groq_cooldown_until:
                        self._groq_cooldown_until = 0.0
                        print("[worker] Groq window reset — Groq is primary again")
                except Exception as e:
                    msg = str(e)
                    if "429" in msg or "rate limit" in msg.lower():
                        self._groq_cooldown_until = time.monotonic() + \
                            GROQ_RATELIMIT_COOLDOWN_S
                        print(f"[worker] Groq rate-limited — Azure serves for "
                              f"{GROQ_RATELIMIT_COOLDOWN_S:.0f}s, then Groq "
                              f"retries")
                    else:
                        self._groq_cooldown_until = time.monotonic() + \
                            GROQ_ERROR_COOLDOWN_S
                        print(f"[worker] Groq error ({e}) — retrying in "
                              f"{GROQ_ERROR_COOLDOWN_S:.0f}s")
            if text is None and self.azure_stt is not None and lang_arg:
                # Azure Speech needs the language up front: only usable when
                # the language is pinned or vote-locked (skip on auto-unknown)
                try:
                    text, seg_texts, info = self.azure_stt.transcribe(
                        seg, language=lang_arg)
                except Exception as e:
                    print(f"[worker] Azure Speech failed ({e})")
            if text is None and self.azure_stt_paid is not None and lang_arg:
                # free Speech tier exhausted: spend student credits to keep
                # cloud quality before dropping to local whisper
                try:
                    text, seg_texts, info = self.azure_stt_paid.transcribe(
                        seg, language=lang_arg)
                except Exception as e:
                    print(f"[worker] Azure Speech paid failed ({e})")
            if text is None:
                try:
                    text, seg_texts, info = \
                        self._load_local_transcriber().transcribe(
                            seg, language=lang_arg, initial_prompt=prompt)
                except Exception as e:
                    print(f"[worker] Transcription error: {e}")
                    continue
            t_asr = time.monotonic()
            detected = info.language or "?"
            self._record_detection(sid, detected,
                                   getattr(info, "language_probability", 0.0))
            if self.cfg.get("debug_save_audio"):
                self._dump_audio(seg, speaker, uid, partial, detected)
            if not is_acceptable(text, info, dur, int(self.cfg["min_text_len"])):
                continue

            lang = detected
            src = whisper_to_nllb(lang)
            targets = sorted(
                self.registry.distinct() | {self.cfg["default_target_lang"]})
            results: dict[str, str] = {}
            need_nllb: list[str] = []
            rules = (self.cfg.get("callout_glossary") or {}).get(lang)
            for tgt in targets:
                if src is None or tgt == src:
                    results[tgt] = text  # unmapped language or same language: raw
                elif tgt == "eng_Latn" and rules:
                    glossed = [apply_glossary(s, rules) for s in seg_texts]
                    if all(g is not None for g in glossed):
                        results[tgt] = " ".join(g for g in glossed if g)  # pure callout
                    else:
                        need_nllb.append(tgt)
                else:
                    need_nllb.append(tgt)
            if need_nllb:
                names = (rules or {}).get("names")
                mt_texts = [apply_names(s, names) for s in seg_texts] if names else seg_texts
                translated = None
                mt_engines = [e for e in (self.azure, self.azure_paid) if e]
                for mt in mt_engines:
                    try:
                        translated = mt.translate_multi(mt_texts, src, need_nllb)
                        break
                    except Exception as e:
                        print(f"[worker] MT engine failed ({e}); trying next")
                if translated is None:
                    try:
                        translated = self.translator.translate_multi(
                            mt_texts, src, need_nllb)
                    except Exception as e:
                        print(f"[worker] Translation error: {e}")
                        continue
                results.update(translated)
            t_trans = time.monotonic()

            lat = t_trans - t_vad
            self._recent.append((partial, round(lat, 3)))
            line_id = f"{sid}:{uid}"
            for tgt in sorted(results):
                event = {
                    "type": "subtitle",
                    "speaker": speaker, "speaker_id": str(sid),
                    "original": text, "translated": results[tgt],
                    "lang": lang, "target": tgt, "id": line_id,
                    "partial": partial, "ts": time.time(),
                    "stage_ts": {"vad": t_vad, "asr": t_asr,
                                 "trans": t_trans, "sent": time.monotonic()},
                    "latency": round(lat, 3),
                }
                try:
                    self.emit(event)
                except Exception as e:
                    print(f"[worker] Emit failed: {e}")

    def _take_next(self, pending: list) -> dict | None:
        """Pop the next segment to process, collapsing stale versions: for one
        (speaker, utterance) only the newest partial is worth decoding, and a
        final makes every queued partial of that utterance obsolete."""
        # backlog guard: when the queue runs long (cloud fallback loading or
        # lagging), cut the old tail — stale subtitles are worthless
        keep = int(self.cfg.get("backlog_keep", 3))
        if self.seg_q.qsize() > int(self.cfg.get("backlog_max", 12)):
            drained = []
            while True:
                try:
                    drained.append(self.seg_q.get_nowait())
                except queue.Empty:
                    break
            merged = pending + drained
            dropped = max(0, len(merged) - keep)
            if dropped:
                pending[:] = merged[-keep:] if keep else []
                print(f"[worker] backlog {len(merged)} deep: dropped "
                      f"{dropped} stale utterances, kept {len(pending)}")
        if not pending:
            try:
                pending.append(self.seg_q.get(timeout=0.5))
            except Exception:
                return None
        while True:
            try:
                pending.append(self.seg_q.get_nowait())
            except Exception:
                break
        item = pending.pop(0)
        key = lambda d: (d.get("speaker_id"), d.get("uid"))  # noqa: E731
        if item.get("partial"):
            same = [p for p in pending if key(p) == key(item)]
            if same:
                finals = [p for p in same if not p.get("partial")]
                chosen = finals[0] if finals else same[-1]
                for i, p in enumerate(pending):
                    if p is chosen:  # by identity: dicts hold ndarrays
                        del pending[i]
                        break
                item = chosen
            pending[:] = [p for p in pending if key(p) != key(item)]
        return item

    def _dump_audio(self, seg, speaker, uid, partial, detected: str):
        """Save what the server actually heard — for diagnosing voice issues."""
        import wave
        d = APP_DIR / "debug_audio"
        d.mkdir(exist_ok=True)
        safe = "".join(c if c.isalnum() else "_" for c in str(speaker))[:24]
        name = (f"{time.strftime('%H%M%S')}_{safe}_u{uid}"
                f"{'p' if partial else ''}_{detected}.wav")
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
