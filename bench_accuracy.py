"""Accuracy benchmark: known KO/JA gaming sentences -> gTTS -> pipeline -> chrF
against reference English. Clean and noise-degraded (12 dB SNR) variants.
Usage: venv/Scripts/python bench_accuracy.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from main import add_cuda_dll_dirs  # noqa: E402
add_cuda_dll_dirs()

import numpy as np  # noqa: E402
import sacrebleu  # noqa: E402

SENTS = [
    ("ko", "지금 오브젝트 싸움 하지 말고 빼자.", "Don't fight over the objective now, let's back off."),
    ("ko", "힐러가 계속 물리니까 각 잡지 말고 뒤로 빠져.", "The healer keeps getting dived, so fall back and don't take angles."),
    ("ko", "궁극기 다 모였으면 한타 열자.", "If our ultimates are all charged, let's start the team fight."),
    ("ko", "적 라인하르트 뒤에서 오니까 조심해.", "Careful, the enemy Reinhardt is coming from behind."),
    ("ko", "탄약 떨어졌어, 잠깐만 리로드할게.", "I'm out of ammo, give me a second to reload."),
    ("ko", "오늘 컨디션 진짜 안 좋다, 게임 몇 판만 더 하고 잘게.", "I'm really not in form today, I'll play a few more games and then sleep."),
    ("ko", "포지션 너무 앞으로 나왔어, 조금만 뒤로 가자.", "You're positioned too far forward, let's fall back a little."),
    ("ko", "상대 조합이 우리한테 불리하니까 우리는 사리면서 하자.", "Their composition is unfavorable for us, so let's play it safe."),
    ("ja", "敵が左側から来るから気をつけて。", "The enemy is coming from the left, so be careful."),
    ("ja", "弾薬がない、リロードするから少し待って。", "I'm out of ammo, wait a bit while I reload."),
    ("ja", "今のヒールありがと、助かった。", "Thanks for the heal, that saved me."),
    ("ja", "前に出すぎだから下がろう。", "You're too far forward, let's back up."),
    ("ja", "このマップは苦手だから気をつけてプレイしよう。", "I'm bad at this map, so let's play carefully."),
    ("ja", "相手のスナイパーが高台にいるから気をつけて。", "Watch out, the enemy sniper is on the high ground."),
    ("ja", "今日は調子いいね、この調子で行こう。", "You're in good form today, let's keep it up."),
    ("ja", "味方が全員やられたから一回下がって体勢を整えよう。", "All our teammates got killed, so let's fall back once and regroup."),
]

PROMPTS = {
    "ko": "오버워치 게임 음성 채팅. 아나, 키리코, 라인하르트, 바스티온, 레퍼, 윈스턴, 궁극기, 탱커, 힐러, 딜러, 탄약, 리로드, 한타, 오브젝트, 포지션.",
    "ja": "オーバーウォッチのゲームボイスチャット。アルティメット、タンク、ヒーラー、スナイパー、弾薬、リロード、味方、敵、高台、体勢を整える。",
}

CACHE = Path("bench_cache")
CACHE.mkdir(exist_ok=True)


def synth(lang: str, text: str, noisy: bool) -> np.ndarray:
    import av
    key = f"{lang}_{abs(hash((text, noisy)))}.wav"
    path = CACHE / key
    if not path.exists():
        from gtts import gTTS
        gTTS(text, lang=lang).save(str(path.with_suffix(".mp3")))
        r = av.AudioResampler(format="s16", layout="mono", rate=16000)
        chunks = []
        with av.open(str(path.with_suffix(".mp3"))) as c:
            for frame in c.decode(audio=0):
                for rf in r.resample(frame):
                    a = rf.to_ndarray()
                    if a.ndim > 1:
                        a = a.mean(axis=0)
                    chunks.append(a.astype(np.float32) / 32768.0)
        audio = np.concatenate(chunks)
        if noisy:
            rng = np.random.default_rng(42)
            noise = rng.normal(0, 1, len(audio)).astype(np.float32)
            noise *= audio.std() / (10 ** (12 / 20))  # 12 dB SNR
            audio = audio + noise
        import wave
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
    import wave
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0


def main():
    from transcriber import Transcriber
    from translator import NllbTranslator

    t = Transcriber("large-v3-turbo", "cuda", "int8_float32", beam_size=1)
    n = NllbTranslator("JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
                       "JustFrederik/nllb-200-distilled-1.3B-ct2-int8",
                       "cuda", "int8_float32", "eng_Latn", beam_size=1)

    audios = {}
    for lang, ko, _ in SENTS:
        audios[(lang, ko, False)] = synth(lang, ko, False)
        audios[(lang, ko, True)] = synth(lang, ko, True)

    configs = [
        ("beam1", 1, False),
        ("beam2", 2, False),
        ("beam2+prompt", 2, True),
    ]
    for label, beam, use_prompt in configs:
        t.beam_size = beam
        for variant in (False, True):
            scores, t0 = [], time.time()
            for lang, src_text, ref in SENTS:
                audio = audios[(lang, src_text, variant)]
                kw = {"language": lang}
                if use_prompt:
                    kw["initial_prompt"] = PROMPTS[lang]
                text, seg_texts, info = t.transcribe(audio, **kw)
                out = n.translate(seg_texts, {"ko": "kor_Hang", "ja": "jpn_Jpan"}[lang])
                chrf = sacrebleu.sentence_chrf(out, [ref]).score
                scores.append(chrf)
            tag = "noisy12dB" if variant else "clean"
            print(f"{label:14s} {tag:9s} chrF={np.mean(scores):5.1f}  (n={len(scores)}, {time.time()-t0:.0f}s)")

    # NLLB beam comparison on the best whisper setting
    print("--- nllb beam comparison (beam2 whisper + prompt, clean) ---")
    t.beam_size = 2
    for nbeam in (1, 2):
        outs, refs = [], []
        for lang, src_text, ref in SENTS:
            text, seg_texts, info = t.transcribe(
                audios[(lang, src_text, False)], language=lang, initial_prompt=PROMPTS[lang])
            outs.append(n.translate(seg_texts, {"ko": "kor_Hang", "ja": "jpn_Jpan"}[lang], beam_size=nbeam))
            refs.append(ref)
        print(f"nllb beam={nbeam}: chrF={sacrebleu.corpus_chrf(outs, [refs]).score:.1f}")


if __name__ == "__main__":
    main()
