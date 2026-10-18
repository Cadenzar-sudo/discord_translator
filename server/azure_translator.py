"""Azure Translator engine: cloud MT quality with a generous free tier
(2M chars/month on the F0 tier), used with local NLLB as the fallback.

Setup: portal.azure.com -> Create resource -> Translator -> Free F0 ->
Keys and Endpoint -> paste key + region into config.json under "azure".
"""

import requests

# NLLB code -> Azure code (both 2-letter codes coincide for our pairs;
# derive from the existing table so new languages stay in sync)
from translator import WHISPER_TO_NLLB

AZURE_CODE = {nllb: whisper for whisper, nllb in WHISPER_TO_NLLB.items()}


class AzureTranslator:
    """translate_multi(texts, src_nllb, targets) mirrors NllbTranslator's
    interface so the pipeline can swap/fall back between them freely."""

    def __init__(self, key: str, region: str = "",
                 endpoint: str = "api.cognitive.microsofttranslator.com"):
        if not key:
            raise ValueError("Azure Translator key is empty")
        self.headers = {
            "Ocp-Apim-Subscription-Key": key,
            "Content-Type": "application/json",
        }
        if region:
            self.headers["Ocp-Apim-Subscription-Region"] = region
        self.url = f"https://{endpoint}/translate?api-version=3.0"

    def translate_multi(self, texts, src_nllb: str, targets: list[str],
                        beam_size: int | None = None) -> dict[str, str]:
        """Translate one list of phrases into SEVERAL target languages in a
        single Azure batch call. Returns {nllb_target: translated_text}."""
        items = [t for t in texts if t and t.strip()]
        if not items or not targets:
            return {}
        to = [AZURE_CODE.get(t, t.split("_")[0]) for t in targets]
        query = "&".join(f"to={c}" for c in to)
        resp = requests.post(
            f"{self.url}&{query}",
            json=[{"Text": t} for t in items],
            headers=self.headers, timeout=8)
        if resp.status_code != 200:
            raise RuntimeError(f"Azure Translator HTTP {resp.status_code}: "
                               f"{resp.text[:200]}")
        data = resp.json()
        # response[i].translations[j] matches the requested `to` order
        per_target: dict[str, list[str]] = {t: [] for t in to}
        for entry in data:
            for tr in entry.get("translations", []):
                if tr.get("to") in per_target:
                    per_target[tr["to"]].append(tr.get("text", ""))
        return {tgt: " ".join(x for x in per_target[azure] if x)
                for tgt, azure in zip(targets, to)}
