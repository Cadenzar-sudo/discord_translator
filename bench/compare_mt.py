"""Compare Azure vs NLLB translation quality + latency on known sentences."""
import sys, time
sys.path.insert(0, "../server")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SENTS = [
    ("지금 오브젝트 싸움 하지 말고 빼자.", "Don't fight over the objective now, let's back off."),
    ("힐러가 계속 물리니까 각 잡지 말고 뒤로 빠져.", "The healer keeps getting dived, so fall back and don't take angles."),
    ("궁극기 다 모였으면 한타 열자.", "If our ultimates are all charged, let's start the team fight."),
    ("적 라인하르트 뒤에서 오니까 조심해.", "Careful, the enemy Reinhardt is coming from behind."),
    ("탄약 떨어졌어, 잠깐만 리로드할게.", "I'm out of ammo, give me a second to reload."),
    ("오늘 컨디션 진짜 안 좋다, 게임 몇 판만 더 하고 잘게.", "I'm really not in form today, I'll play a few more games and then sleep."),
    ("포지션 너무 앞으로 나왔어, 조금만 뒤로 가자.", "You're positioned too far forward, let's fall back a little."),
    ("상대 조합이 우리한테 불리하니까 우리는 사리면서 하자.", "Their composition is unfavorable for us, so let's play it safe."),
]
import io
out = io.open("../bench/bench_out/mt_compare.txt", "w", encoding="utf-8")

from main import add_cuda_dll_dirs
add_cuda_dll_dirs()
import json
cfg = json.load(open("../server/config.json", encoding="utf-8"))

import sacrebleu

def score(outs):
    chrf = sum(sacrebleu.sentence_chrf(o, [r]).score for _, r, o in outs) / len(outs)
    return chrf

from translator import NllbTranslator
n = NllbTranslator(cfg["nllb_ct2_repo"], cfg["nllb_tokenizer_repo"], "cuda", "int8_float32")

ko_texts = [k for k, _ in SENTS]
t0 = time.time()
nllb_out = n.translate_multi(ko_texts, "kor_Hang", ["eng_Latn"])
nllb_s = time.time() - t0
nllb_pairs = [(k, r, nllb_out["eng_Latn"].split(". ")[i] if False else None) for i, (k, r) in enumerate(SENTS)]

# translate_multi joins sentences with spaces; split per-sentence by position is
# unreliable — translate individually for scoring instead
t0 = time.time()
nllb_pairs = [(k, r, n.translate_multi([k], "kor_Hang", ["eng_Latn"])["eng_Latn"])
              for k, r in SENTS]
nllb_s = time.time() - t0

from azure_translator import AzureTranslator
az = AzureTranslator(cfg["azure"]["key"], cfg["azure"].get("region") or "")
t0 = time.time()
az_pairs = [(k, r, az.translate_multi([k], "kor_Hang", ["eng_Latn"])["eng_Latn"])
            for k, r in SENTS]
az_s = time.time() - t0

batch_az = az.translate_multi(ko_texts, "kor_Hang", ["eng_Latn"])
batch_n = n.translate_multi(ko_texts, "kor_Hang", ["eng_Latn"])

print(f"NLLB 1.3B: chrF {score(nllb_pairs):.1f}   ({nllb_s:.2f}s for {len(SENTS)} sentences, individual calls)")
print(f"AZURE    : chrF {score(az_pairs):.1f}   ({az_s:.2f}s individual; batch of 8 in one call)")
print()
out.write("=== sentence-by-sentence ===\n")
for (k, r, no), (_, _, ao) in zip(nllb_pairs, az_pairs):
    line = f"KO: {k}\nREF: {r}\nNLLB : {no}\nAZURE: {ao}\n"
    print(line)
    out.write(line + "\n")
out.write(f"NLLB chrF {score(nllb_pairs):.1f} | AZURE chrF {score(az_pairs):.1f}\n")
out.close()
print("written to ../bench/bench_out/mt_compare.txt")
