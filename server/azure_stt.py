"""Azure Speech-to-Text fallback: used when the primary ASR (Groq free tier)
is exhausted or errors. Requires a Speech service resource in the same Azure
subscription (portal.azure.com -> Create resource -> Speech).

REST v1 single-utterance endpoint: WAV in, JSON out. No phrase hints, no
per-segment data — the pipeline's defaults apply.
"""

import io

import numpy as np
import requests
from types import SimpleNamespace

from groq_asr import _to_wav

# Whisper's 2-letter codes -> Azure Speech locales (BCP-47): the v1 endpoint
# rejects bare "ko"-style codes with HTTP 400 "Invalid 'language' parameter".
AZURE_LOCALE = {
    "en": "en-US", "ja": "ja-JP", "ko": "ko-KR", "zh": "zh-CN", "yue": "zh-HK",
    "ru": "ru-RU", "es": "es-ES", "pt": "pt-BR", "fr": "fr-FR", "de": "de-DE",
    "it": "it-IT", "nl": "nl-NL", "pl": "pl-PL", "tr": "tr-TR", "vi": "vi-VN",
    "th": "th-TH", "id": "id-ID", "ms": "ms-MY", "uk": "uk-UA", "cs": "cs-CZ",
    "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI", "no": "nb-NO", "el": "el-GR",
    "he": "he-IL", "hi": "hi-IN", "bn": "bn-IN", "ta": "ta-IN", "tl": "fil-PH",
    "ar": "ar-SA", "fa": "fa-IR", "ro": "ro-RO", "hu": "hu-HU",
}


def _locale(lang: str | None) -> str | None:
    if not lang:
        return None
    lang = lang.strip()
    if "-" in lang:  # already a locale (ko-KR, en-US): pass through
        return lang
    return AZURE_LOCALE.get(lang.lower(), f"{lang}-{lang.upper()}")


class AzureSTT:
    """transcribe() mirrors Transcriber/GroqASR's interface. Azure v1 needs
    the language up front, so this engine is used only when the pipeline's
    language is known (pinned or vote-locked)."""

    def __init__(self, key: str, region: str):
        if not key or not region:
            raise ValueError("Azure Speech key and region are required")
        self.url = (f"https://{region}.stt.speech.microsoft.com"
                    f"/speech/recognition/conversation/cognitiveservices/v1")
        self.headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "audio/wav; codecs=audio/pcm; samplerate=16000",
            "Accept": "application/json",
        }

    def transcribe(self, audio: np.ndarray, translate: bool = False,
                   language: str | None = None,
                   initial_prompt: str | None = None):
        buf = _to_wav(audio)
        resp = requests.post(
            self.url + (f"?language={_locale(language)}" if language else ""),
            data=buf.getvalue(),
            headers=self.headers,
            timeout=15)
        if resp.status_code == 429:
            raise RuntimeError("Azure Speech quota exceeded (429)")
        if resp.status_code in (401, 403):
            raise RuntimeError(f"Azure Speech rejected the key ({resp.status_code})")
        if resp.status_code != 200:
            raise RuntimeError(f"Azure Speech HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        r = resp.json()
        status = r.get("RecognitionStatus", "")
        text = (r.get("DisplayText") or "").strip()
        if status != "Success":
            # NoMatch = silence/unintelligible: empty result, let the
            # pipeline's normal filtering drop it
            return "", [], SimpleNamespace(
                language=language, language_probability=0.0,
                avg_logprob=0.0, no_speech_prob=1.0, compression_ratio=1.0)
        info = SimpleNamespace(
            language=language,
            language_probability=0.8,
            avg_logprob=0.0, no_speech_prob=0.0,
            compression_ratio=1.0)
        return text, [text] if text else [], info
