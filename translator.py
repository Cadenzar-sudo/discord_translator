"""Translation engines: NLLB-200 (CTranslate2) and Whisper's translate task."""

import re
import shutil
from pathlib import Path

from config import MODEL_DIR

_CJK_RE = re.compile(r"[\uac00-\ud7af\u3040-\u30ff\u4e00-\u9fff]")


def apply_glossary(text: str, rules: dict) -> str | None:
    """Deterministic callout translation: hero-name mapping + slang patterns.
    Returns English if the whole segment is covered by the rules, else None
    (fall back to the neural translator)."""
    if not rules:
        return None
    out = text.strip()
    for src, dst in (rules.get("names") or {}).items():
        out = out.replace(src, dst)
    for pat, repl in (rules.get("patterns") or []):
        out = re.sub(pat, repl, out)
    if not out or _CJK_RE.search(out):
        return None  # source-language text remains: not a pure callout
    return re.sub(r"\s+", " ", out).strip()

# NLLB compresses multi-sentence run-on input to its leading sentence, so long
# segments are split into individual sentences before batching.
_SENT_SPLIT_RE = re.compile(r"(?<=[.。!?？！])\s+")

# Whisper ISO-639-1 -> NLLB 200 language codes (common Discord languages first).
WHISPER_TO_NLLB = {
    "en": "eng_Latn", "ja": "jpn_Jpan", "ko": "kor_Hang", "zh": "zho_Hans",
    "yue": "yue_Hant", "ru": "rus_Cyrl", "es": "spa_Latn", "pt": "por_Latn",
    "fr": "fra_Latn", "de": "deu_Latn", "it": "ita_Latn", "nl": "nld_Latn",
    "pl": "pol_Latn", "tr": "tur_Latn", "vi": "vie_Latn", "th": "tha_Thai",
    "id": "ind_Latn", "ms": "zsm_Latn", "uk": "ukr_Cyrl", "cs": "ces_Latn",
    "sv": "swe_Latn", "da": "dan_Latn", "fi": "fin_Latn", "no": "nob_Latn",
    "el": "ell_Grek", "he": "heb_Hebr", "hi": "hin_Deva", "bn": "ben_Beng",
    "ta": "tam_Taml", "tl": "tgl_Latn", "ar": "arb_Arab", "fa": "pes_Arab",
    "ro": "ron_Latn", "hu": "hun_Latn", "hu-HU": "hun_Latn",
}

LANGUAGE_NAMES = {
    "en": "English", "ja": "Japanese", "ko": "Korean", "zh": "Chinese",
    "ru": "Russian", "es": "Spanish", "pt": "Portuguese", "fr": "French",
    "de": "German", "it": "Italian", "th": "Thai", "vi": "Vietnamese",
    "id": "Indonesian", "ar": "Arabic", "hi": "Hindi", "tr": "Turkish",
}


def whisper_to_nllb(code: str | None) -> str | None:
    if not code:
        return None
    return WHISPER_TO_NLLB.get(code.lower())


def download_nllb_ct2(repo: str, local_dir: Path | None = None) -> Path:
    """Download a pre-converted CTranslate2 NLLB model from Hugging Face."""
    if local_dir is None:
        safe = repo.replace("/", "__")
        local_dir = MODEL_DIR / safe
    marker = local_dir / "model.bin"
    if marker.exists():
        return local_dir
    from huggingface_hub import snapshot_download
    print(f"[nllb] Downloading CTranslate2 model '{repo}' (first run only)...")
    try:
        snapshot_download(
            repo_id=repo, local_dir=str(local_dir),
            allow_patterns=["*.bin", "*.json", "*.txt", "*.model"])
    except Exception as e:
        shutil.rmtree(local_dir, ignore_errors=True)
        raise RuntimeError(
            f"Could not download the NLLB CTranslate2 model from '{repo}': {e}\n"
            "  Check your internet connection, or set nllb_ct2_repo in config.json\n"
            "  to another CTranslate2 NLLB conversion.") from e
    if not marker.exists():
        shutil.rmtree(local_dir, ignore_errors=True)
        raise RuntimeError(
            f"'{repo}' does not contain a CTranslate2 model.bin. Install the\n"
            "  ct2-transformers-converter CLI and convert\n"
            "  facebook/nllb-200-distilled-600M yourself, or set nllb_ct2_repo\n"
            "  in config.json to a valid conversion.")
    return local_dir


class NllbTranslator:
    """NLLB-200 on CTranslate2. Tokenizer comes from the original HF repo."""

    def __init__(self, ct2_repo: str, tokenizer_repo: str, device: str,
                 compute_type: str, target_lang: str, beam_size: int = 1):
        import ctranslate2
        from transformers import AutoTokenizer

        self.target_lang = target_lang
        self.beam_size = beam_size
        self.ct2_dir = download_nllb_ct2(ct2_repo)
        print(f"[nllb] Loading tokenizer from '{tokenizer_repo}'...")
        self.tok = AutoTokenizer.from_pretrained(tokenizer_repo)
        print(f"[nllb] Loading model from {self.ct2_dir} ({device}, {compute_type})...")
        self.translator = ctranslate2.Translator(
            str(self.ct2_dir), device=device, compute_type=compute_type)

    def translate(self, text, src_nllb: str, beam_size: int | None = None):
        """Translate one string or a list of phrase segments (batched through the
        model in a single call — see _SENT_SPLIT_RE for why long segments are
        split first)."""
        texts = [text] if isinstance(text, str) else list(text)
        chunks: list[str] = []
        for t in texts:
            parts = [p.strip() for p in _SENT_SPLIT_RE.split(t) if p and p.strip()]
            chunks.extend(parts or [t.strip()])
        batch = [
            self.tok.convert_ids_to_tokens(self.tok(t, src_lang=src_nllb)["input_ids"])
            for t in chunks
        ]
        results = self.translator.translate_batch(
            batch, target_prefix=[[self.target_lang]] * len(batch),
            beam_size=beam_size or self.beam_size,
            max_decoding_length=256, max_batch_size=8)
        outs = [
            self.tok.decode(self.tok.convert_tokens_to_ids(r.hypotheses[0]),
                            skip_special_tokens=True).strip()
            for r in results
        ]
        return " ".join(o for o in outs if o)
