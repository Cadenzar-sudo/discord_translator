"""faster-whisper wrapper: greedy real-time transcription with hallucination guards."""

import os
import re
from pathlib import Path

# TRANSLATOR_MODELS_DIR lets several installs share one model cache
# (e.g. the desktop app's D:\Translation Discord\models).
MODEL_DIR = Path(os.environ.get("TRANSLATOR_MODELS_DIR")
                 or (Path(__file__).resolve().parent / "models"))

# Phrases Whisper emits on near-silence; only rejected for short segments.
HALLUCINATION_PHRASES = {
    "thank you.", "thanks for watching!", "thank you for watching.",
    "thanks for watching.", "see you next time.", "bye.", "bye-bye.",
    "字幕", "字幕by", "subtitle", "subtitles", "please subscribe.",
    "ok.", "okay.", "uh.", "um.", "hmm.", "you",
}
_NOISE_RE = re.compile(r"^[\W_]*$")
_AMARA_RE = re.compile(r"amara\.org|subtitles? by", re.IGNORECASE)


class Transcriber:
    def __init__(self, model_name: str, device: str, compute_type: str,
                 cpu_threads: int = 4, beam_size: int = 1):
        from faster_whisper import WhisperModel
        self.beam_size = beam_size
        self.model = WhisperModel(
            model_name, device=device, compute_type=compute_type,
            cpu_threads=cpu_threads, download_root=str(MODEL_DIR / "whisper"),
            local_files_only=False)

    def transcribe(self, audio, translate: bool = False, language: str | None = None,
                   initial_prompt: str | None = None):
        """audio: float32 mono 16 kHz. Returns (text, segment_texts, info) —
        info.language is the ISO-639-1 code detected by Whisper, segment_texts
        are the per-phrase pieces (aligned with sentences, used for NLLB)."""
        segments, info = self.model.transcribe(
            audio,
            language=language,
            task="translate" if translate else "transcribe",
            beam_size=self.beam_size,
            best_of=self.beam_size,
            temperature=(0.0, 0.2),
            compression_ratio_threshold=2.4,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.6,
            condition_on_previous_text=False,
            initial_prompt=initial_prompt,
            vad_filter=False,  # we segment ourselves with Silero
        )
        texts = []
        for s in segments:
            t = s.text.strip()
            if not t:
                continue
            # per-segment guards: aggregate info misses loops confined to one segment
            if s.avg_logprob < -1.0:
                continue
            if s.compression_ratio > 2.4:
                continue
            if s.no_speech_prob > 0.8 and s.avg_logprob < -0.35:
                continue
            texts.append(t)
        return " ".join(texts), texts, info


def is_acceptable(text: str, info, duration_s: float, min_len: int = 2) -> bool:
    """Filter hallucinations and noise before translation."""
    t = text.strip()
    if len(t) < min_len:
        return False
    if getattr(info, "avg_logprob", 0.0) < -1.0:
        return False
    if (getattr(info, "no_speech_prob", 0.0) > 0.8
            and getattr(info, "avg_logprob", 0.0) < -0.35):
        return False
    if getattr(info, "compression_ratio", 1.0) > 2.4:
        return False
    if _NOISE_RE.fullmatch(t):
        return False
    if _AMARA_RE.search(t):
        return False
    if duration_s < 3.0 and t.lower().rstrip(".!?") in (
            p.rstrip(".!?") for p in HALLUCINATION_PHRASES):
        return False
    return True
