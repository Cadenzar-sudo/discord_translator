"""Compare the new bot pipeline against the old desktop app on the same media.

    python compare_apps.py --id <cached-id>
    python compare_apps.py --media clip.m4a [--refs clip.ko.vtt]
    python compare_apps.py --id X --repeat-model small   # also bench `small`

Both apps process the clip at full speed (no realtime pacing) so their
latency numbers mean the same thing: pure processing time per utterance.
The old app runs from its own venv at --old-app (default: the sibling
"Translation Discord" project) with --test-file.

Report: markdown + console table (chrF ASR/translation, char similarity,
latency mean/p95).
"""

import argparse
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
CACHE = BENCH / "bench_cache"
NEW_SERVER_PY = BENCH.parent / "server" / "venv" / "Scripts" / "python.exe"
DEFAULT_OLD = Path(r"D:\Translation Discord")

_FINAL_RE = re.compile(
    r"^\[(\d+:\d+:\d+)\] #(\d+)(~?) \((\w+)\) (.*?)\s+\(([\d.]+)s\)\s*$")


def bench_new(media: Path, refs: Path | None, model: str | None,
              out: Path) -> dict:
    cmd = [sys.executable, str(BENCH / "bench_e2e.py"),
           "--media", str(media), "--fast", "--idle", "15",
           "--out", str(out)]
    if refs:
        cmd += ["--refs", str(refs)]
    if model:
        cmd += ["--whisper-model", model]
    print(f"[cmp] NEW: {' '.join(cmd[1:])}")
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print(r.stdout[-3000:], r.stderr[-2000:])
        sys.exit("new-app bench failed")
    prefix = Path(f"{out}_{model}") if model else out
    rows = list(_csv_rows(Path(f"{prefix}_latency.csv")))
    return {"latency_s": [float(x["server_s"]) for x in rows if x["server_s"]],
            "finals": _finals_from_jsonl(Path(f"{prefix}_events.jsonl")),
            "log": r.stdout}


def bench_old(app_dir: Path, media: Path) -> dict:
    old_py = app_dir / "venv" / "Scripts" / "python.exe"
    if not old_py.exists():
        print(f"[cmp] OLD app venv missing ({old_py}) — skipping old-app column")
        return {}
    print(f"[cmp] OLD: {old_py} main.py --test-file {media}")
    r = subprocess.run([str(old_py), "main.py", "--test-file", str(media)],
                       cwd=str(app_dir), stdout=subprocess.PIPE,
                       stderr=subprocess.STDOUT, text=True,
                       encoding="utf-8", errors="replace")
    finals, lats = [], []
    pending = None
    for line in (r.stdout or "").splitlines():
        m = _FINAL_RE.match(line.strip())
        if m:
            ts, uid, partial, lang, translated, lat = m.groups()
            if partial == "~":
                pending = None
                continue
            pending = {"lang": lang, "translated": translated.strip(),
                       "original": "", "latency_s": float(lat)}
            finals.append(pending)
            lats.append(float(lat))
        elif pending is not None and line.startswith("    ") and ": " in line:
            pending["original"] = line.strip().split(": ", 1)[1]
    if not finals:
        print(r.stdout[-2000:])
        print("[cmp] WARNING: no finals parsed from old app output")
    return {"latency_s": lats, "finals": finals, "log": r.stdout}


def _csv_rows(path: Path):
    import csv
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        yield from csv.DictReader(f)


def _finals_from_jsonl(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "subtitle" and not ev.get("partial"):
            out.append(ev)
    return out


def _mean_p95(vals):
    if not vals:
        return "—", "—"
    vals = sorted(vals)
    return (f"{statistics.mean(vals):.2f}",
            f"{vals[min(len(vals) - 1, int(len(vals) * 0.95))]:.2f}")


def score(finals: list[dict], ref_text: str | None):
    import sacrebleu
    from bench_e2e import char_similarity
    orig = " ".join(f.get("original", "") for f in finals)
    trans = " ".join(f.get("translated", "") for f in finals)
    if not ref_text or not finals:
        return {}
    return {
        "n": len(finals),
        "asr_chrf": round(sacrebleu.sentence_chrf(orig, [ref_text]).score, 1),
        "asr_char_sim": char_similarity(orig, ref_text),
        "trans_chrf": round(sacrebleu.sentence_chrf(trans, [ref_text]).score, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id")
    ap.add_argument("--media")
    ap.add_argument("--refs")
    ap.add_argument("--old-app", default=str(DEFAULT_OLD),
                    help="path to the old desktop app")
    ap.add_argument("--repeat-model", metavar="MODEL",
                    help="also bench the new app with this whisper model")
    args = ap.parse_args()

    if args.id:
        idx = json.loads((CACHE / "media_index.json").read_text(encoding="utf-8"))
        entry = next((e for e in idx if e["id"] == args.id), None)
        if not entry:
            sys.exit(f"id {args.id} not in index")
        media = CACHE / entry["audio"]
        refs = CACHE / entry["subs"] if entry.get("subs") else None
    else:
        media = Path(args.media) if args.media else None
        refs = Path(args.refs) if args.refs else None
    if not media or not media.exists():
        sys.exit("give --id or --media")
    media = media.resolve()  # the old app runs with its own cwd
    if refs and refs.exists():
        refs = refs.resolve()

    from fetch_media import vtt_to_text
    ref_text = None
    if refs and refs.exists():
        ref_text = vtt_to_text(refs) if refs.suffix == ".vtt" \
            else refs.read_text(encoding="utf-8")

    new = bench_new(media, refs, None, BENCH / "bench_out" / f"{media.stem}_cmp_new")
    old = bench_old(Path(args.old_app), media)

    rows = [("NEW (bot server)", score(new["finals"], ref_text), new["latency_s"])]
    if old:
        rows.append(("OLD (desktop app)", score(old["finals"], ref_text),
                     old["latency_s"]))
    if args.repeat_model:
        m = bench_new(media, refs, args.repeat_model,
                      BENCH / "bench_out" / f"{media.stem}_cmp_new")
        rows.append((f"NEW (model={args.repeat_model})",
                     score(m["finals"], ref_text), m["latency_s"]))

    print("\n" + "=" * 72)
    print(f" comparison on {media.name}  "
          f"(refs: {refs.name if ref_text else 'none'})")
    print("=" * 72)
    md = [f"# old vs new — {media.name}",
          f"reference: {refs.name if ref_text else 'none'}", "",
          "| pipeline | finals | ASR chrF | ASR char-sim | MT chrF | lat mean (s) | lat p95 (s) |",
          "|---|---|---|---|---|---|---|"]
    for label, sc, lats in rows:
        if not sc:
            md.append(f"| {label} | — | — | — | — | — | — |")
            continue
        mean, p95 = _mean_p95(lats)
        md.append(f"| {label} | {sc['n']} | {sc['asr_chrf']} | "
                  f"{sc['asr_char_sim']} | {sc['trans_chrf']} | {mean} | {p95} |")
    table = "\n".join(md)
    print(table)
    out = BENCH / "bench_out" / f"{media.stem}_compare.md"
    out.write_text(table, encoding="utf-8")
    print(f"\n[cmp] report: {out}")


if __name__ == "__main__":
    main()
