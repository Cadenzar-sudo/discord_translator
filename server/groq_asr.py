"""Groq Whisper ASR engine: cloud transcription (whisper-large-v3-turbo) via
Groq's OpenAI-compatible API. Free tier is gated at ~20 requests/minute —
with the mixed-stream design, finals-only traffic usually fits; anything the
API rejects falls back to the local faster-whisper model (loaded lazily, so
the GPU VRAM stays free while Groq is handling everything).

Setup: console.groq.com -> API Keys -> Create API Key -> paste into
config.json under "groq".
"""

import io
import wave
from types import SimpleNamespace

import numpy as np
import requests

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"

# Groq verbose_json reports the language as a full name ("Korean") — map the
# names whisper knows to ISO-639-1 codes for the pipeline.
_LANG_NAME_TO_CODE = {
    "english": "en", "korean": "ko", "japanese": "ja", "chinese": "zh",
    "mandarin": "zh", "vietnamese": "vi", "thai": "th", "russian": "ru",
    "spanish": "es", "portuguese": "pt", "french": "fr", "german": "de",
    "italian": "it", "dutch": "nl", "polish": "pl", "turkish": "tr",
    "arabic": "ar", "hindi": "hi", "indonesian": "id", "ukrainian": "uk",
    "czech": "cs", "swedish": "sv", "finnish": "fi", "norwegian": "no",
    "danish": "da", "hebrew": "he",
}


def _to_wav(audio: np.ndarray) -> io.BytesIO:
    """float32 mono 16 kHz -> in-memory WAV (Groq accepts WAV uploads)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    buf.seek(0)
    return buf


class GroqASR:
    """transcribe() mirrors transcriber.Transcriber's interface so the
    pipeline can swap between local and Groq freely."""

    def __init__(self, key: str, model: str = "whisper-large-v3-turbo"):
        if not key:
            raise ValueError("Groq API key is empty")
        self.key = key
        self.model = model

    def _lang_code(self, lang_name: str | None) -> str | None:
        """Groq's verbose_json reports the language as a full name ('Korean'),
        but the pipeline needs the ISO-639-1 code ('ko')."""
        if not lang_name:
            return None
        code = _LANG_NAME_TO_CODE.get(lang_name.lower())
        return code if code else (lang_name.lower() if len(lang_name) == 2
                                  else lang_name)

    def transcribe(self, audio, translate: bool = False,
                   language: str | None = None,
                   initial_prompt: str | None = None):
        """audio: float32 mono 16 kHz. Returns (text, segment_texts, info)
        like the local Transcriber (info carries language fields)."""
        buf = _to_wav(audio)
        data = {"model": self.model, "response_format": "verbose_json",
                "temperature": "0"}
        if language:
            data["language"] = language
        if initial_prompt:
            data["prompt"] = initial_prompt[:224]  # API prompt cap
        resp = requests.post(
            GROQ_URL,
            files={"file": ("audio.wav", buf, "audio/wav")},
            data=data,
            headers={"Authorization": f"Bearer {self.key}"},
            timeout=30)
        if resp.status_code == 429:
            raise RuntimeError("Groq rate limit (429) — free tier is "
                               "20 requests/minute")
        if resp.status_code != 200:
            raise RuntimeError(f"Groq HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        r = resp.json()
        meta = r.get("x_groq") or {}
        segs = r.get("segments") or []
        seg_texts = [s.get("text", "").strip() for s in segs]
        seg_texts = [t for t in seg_texts if t]
        info = SimpleNamespace(
            language=self._lang_code(r.get("language")),
            language_probability=meta.get("language_probability", 0.8),
            # aggregate guards: absence lets is_acceptable's getattr defaults
            # pass; per-segment quality is what Groq already filtered
            avg_logprob=(sum(s.get("avg_logprob", 0.0) for s in segs)
                         / len(segs)) if segs else 0.0,
            no_speech_prob=max((s.get("no_speech_prob", 0.0) for s in segs),
                               default=0.0),
            compression_ratio=max((s.get("compression_ratio", 1.0)
                                   for s in segs), default=1.0),
        )
        return r.get("text", "").strip(), seg_texts, info
