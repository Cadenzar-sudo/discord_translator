"""End-to-end benchmark: media file -> server (fake speaker) -> WebSocket ->
this collector. Measures stage + end-to-end latency for partials and finals,
and ASR accuracy against caption references.

    python bench_e2e.py --id <cached-id>            # from fetch_media.py
    python bench_e2e.py --media clip.m4a [--refs clip.ko.vtt]
    add --fast for a non-realtime accuracy run (latency then = processing only)
    add --whisper-model small to compare models

Runs the server as a subprocess on a scratch port; needs the server venv.
"""

import argparse
import asyncio
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
SERVER_DIR = BENCH.parent / "server"
SERVER_PY = SERVER_DIR / "venv" / "Scripts" / "python.exe"
CACHE = BENCH / "bench_cache"


def server_token() -> str:
    cfg = SERVER_DIR / "config.json"
    if cfg.exists():
        try:
            return json.loads(cfg.read_text(encoding="utf-8")) \
                .get("server", {}).get("access_token") or ""
        except Exception:
            pass
    return ""


async def collect(port: int, token: str, target: str, idle_s: float,
                  log: Path, proc=None, tunnel: bool = False) -> list[dict]:
    import aiohttp
    events: list[dict] = []
    base = f"http://127.0.0.1:{port}"
    async with aiohttp.ClientSession() as s:
        if tunnel:
            # connect through the Cloudflare tunnel like a real viewer
            full = json.loads((BENCH.parent / "server" / "config.json")
                              .read_text(encoding="utf-8"))
            srv = full["server"]
            cf = full.get("cloudflare") or {}
            url = f"wss://{srv.get('public_host')}/ws"
            headers = {"Authorization": f"Bearer {srv['access_token']}"}
            if cf.get("client_id"):
                headers["CF-Access-Client-Id"] = cf["client_id"]
                headers["CF-Access-Client-Secret"] = cf["client_secret"]
            print(f"[bench] collecting THROUGH THE TUNNEL: {url}", flush=True)
        else:
            url = f"ws://127.0.0.1:{port}/ws" + (f"?token={token}" if token else "")
            headers = {}

        deadline = time.monotonic() + 900  # first run loads/downloads models
        while time.monotonic() < deadline:
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"server exited (code {proc.returncode}) before becoming "
                    f"ready — check {log}")
            try:
                async with s.get(f"{base}/health",
                                 timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200 and (await r.json()).get("ready"):
                        break
            except Exception:
                pass
            await asyncio.sleep(1.0)
        else:
            raise RuntimeError("server never became ready (check server log)")

        last = time.monotonic()
        async with s.ws_connect(url, headers=headers, heartbeat=None,
                                timeout=aiohttp.ClientTimeout(total=None,
                                                              sock_connect=15)) as ws:
            await ws.send_json({"type": "hello", "target_lang": target})
            print(f"[bench] Collecting (idle cutoff {idle_s}s)…")
            while True:
                try:
                    msg = await ws.receive(timeout=idle_s)
                except asyncio.TimeoutError:
                    break
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        ev = msg.json()
                    except Exception:
                        continue
                    ev["_recv_mono"] = time.monotonic()
                    events.append(ev)
                    if ev.get("type") == "subtitle":
                        last = time.monotonic()
                        kind = "partial" if ev.get("partial") else "final"
                        print(f"    [{kind}] ({ev.get('lang')}) {ev.get('original','')[:60]}")
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE,
                                  aiohttp.WSMsgType.ERROR):
                    break
                if time.monotonic() - last > idle_s:
                    break
    log.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events),
                   encoding="utf-8")
    return events


def latency_rows(events: list[dict]) -> list[dict]:
    rows = []
    for ev in events:
        if ev.get("type") != "subtitle":
            continue
        st = ev.get("stage_ts") or {}
        vad, asr, trans = st.get("vad"), st.get("asr"), st.get("trans")
        sent, recv = st.get("sent"), ev.get("_recv_mono")
        if None in (vad, asr, trans):
            continue
        rows.append({
            "id": ev.get("id"), "partial": bool(ev.get("partial")),
            "asr_s": round(asr - vad, 3),
            "trans_s": round(trans - asr, 3),
            "server_s": round(sent - vad, 3) if sent is not None else None,
            "e2e_s": round(recv - vad, 3) if recv is not None else None,
            "net_client_s": round(recv - sent, 3) if recv is not None and sent is not None else None,
        })
    return rows


def _stats(vals: list) -> dict:
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return {"n": 0}
    return {"n": len(vals), "mean": round(statistics.mean(vals), 3),
            "p50": round(vals[(len(vals) - 1) // 2], 3),
            "p95": round(vals[min(len(vals) - 1, int(len(vals) * 0.95))], 3)}


def summarise(rows: list[dict]) -> dict:
    out = {}
    for kind, label in ((False, "final"), (True, "partial")):
        sub = [r for r in rows if r["partial"] == kind]
        out[label] = {k: _stats([r[k] for r in sub])
                      for k in ("asr_s", "trans_s", "server_s", "e2e_s", "net_client_s")}
    return out


def char_similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    norm = lambda s: re.sub(r"[^가-힣ㄱ-ㅎ0-9a-zA-Z]", "", s)  # noqa: E731
    return round(SequenceMatcher(None, norm(a), norm(b), autojunk=False).ratio(), 4)


def accuracy(events: list[dict], ref_text: str | None) -> dict:
    import sacrebleu
    finals = [e for e in events if e.get("type") == "subtitle" and not e.get("partial")]
    hyp_orig = " ".join(e.get("original", "") for e in finals)
    hyp_trans = " ".join(e.get("translated", "") for e in finals)
    out = {"n_finals": len(finals),
           "n_partials": sum(1 for e in events
                             if e.get("type") == "subtitle" and e.get("partial"))}
    if ref_text:
        out["ref_chars"] = len(ref_text)
        out["asr_chrf_vs_captions"] = round(
            sacrebleu.sentence_chrf(hyp_orig, [ref_text]).score, 2)
        out["asr_char_sim"] = char_similarity(hyp_orig, ref_text)
        out["trans_chrf_vs_captions"] = round(
            sacrebleu.sentence_chrf(hyp_trans, [ref_text]).score, 2)
    return out


def md_table(label: str, stats: dict) -> str:
    keys = ("asr_s", "trans_s", "server_s", "e2e_s")
    head = "| stage | n | mean | p50 | p95 |"
    sep = "|---|---|---|---|---|"
    name = {"asr_s": "VAD->ASR done (s)", "trans_s": "ASR->translation done (s)",
            "server_s": "server total (s)", "e2e_s": "end-to-end at client (s)",
            "net_client_s": "network+client (s)"}
    rows = [head, sep]
    for k in keys + ("net_client_s",):
        s = stats.get(k) or {}
        rows.append(f"| {name[k]} | {s.get('n', 0)} | {s.get('mean', '—')} | "
                    f"{s.get('p50', '—')} | {s.get('p95', '—')} |")
    return f"**{label}**\n" + "\n".join(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", help="cached media id from fetch_media.py")
    ap.add_argument("--media", help="path to an audio/video file")
    ap.add_argument("--refs", help="reference transcript (plain text or .vtt)")
    ap.add_argument("--target", default="eng_Latn")
    ap.add_argument("--fast", action="store_true",
                    help="no realtime pacing (latency = processing only)")
    ap.add_argument("--whisper-model", help="e.g. small / large-v3-turbo")
    ap.add_argument("--port", type=int, default=18923)
    ap.add_argument("--tunnel", action="store_true",
                    help="collect through the Cloudflare tunnel like a real viewer")
    ap.add_argument("--idle", type=float, default=20.0,
                    help="stop collecting after this many idle seconds")
    ap.add_argument("--out", default=None, help="output prefix (default: bench_out/<media>_<mode>)")
    args = ap.parse_args()

    if args.id:
        idx = json.loads((CACHE / "media_index.json").read_text(encoding="utf-8"))
        entry = next((e for e in idx if e["id"] == args.id), None)
        if not entry:
            sys.exit(f"id {args.id} not in {CACHE / 'media_index.json'}")
        media = CACHE / entry["audio"]
        refs = CACHE / entry["subs"] if entry.get("subs") else None
    else:
        media = Path(args.media) if args.media else None
        refs = Path(args.refs) if args.refs else None
    if not media or not media.exists():
        sys.exit("give --id or --media pointing at an existing file")
    media = media.resolve()  # the server subprocess runs with cwd=server/
    if refs and refs.exists():
        refs = refs.resolve()

    ref_text = None
    if refs and refs.exists():
        from fetch_media import vtt_to_text
        ref_text = vtt_to_text(refs) if refs.suffix == ".vtt" \
            else refs.read_text(encoding="utf-8")

    if not SERVER_PY.exists():
        sys.exit(f"server venv missing: {SERVER_PY} — run server/install.bat")

    mode = "fast" if args.fast else "realtime"
    tag = media.stem
    out_dir = BENCH / "bench_out"
    out_dir.mkdir(exist_ok=True)
    prefix = Path(args.out) if args.out else out_dir / f"{tag}_{mode}"

    cmd = [str(SERVER_PY), str(SERVER_DIR / "main.py"),
           "--test-file", str(media), "--speaker", "Bench",
           "--port", str(args.port)]
    if args.fast:
        cmd.append("--fast")
    if args.whisper_model:
        cmd += ["--whisper-model", args.whisper_model]
        prefix = Path(f"{prefix}_{args.whisper_model}")

    print(f"[bench] server: {' '.join(cmd[1:])}")
    log_path = Path(f"{prefix}_server.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(SERVER_DIR), stdout=log,
                            stderr=subprocess.STDOUT)

    try:
        events = asyncio.run(collect(args.port, server_token(), args.target,
                                     args.idle, Path(f"{prefix}_events.jsonl"),
                                     proc=proc, tunnel=args.tunnel))
    finally:
        log.close()
    try:
        proc.wait(timeout=180)
    except subprocess.TimeoutExpired:
        proc.kill()

    rows = latency_rows(events)
    summary = summarise(rows)
    acc = accuracy(events, ref_text)

    lines = [f"# bench: {media.name} ({mode}, target={args.target})",
             "", md_table("Finals", summary["final"]), "",
             md_table("Partials", summary["partial"]), "",
             f"**Accuracy** (refs: {refs.name if refs else 'none'})",
             "```json", json.dumps(acc, ensure_ascii=False, indent=2), "```", "",
             f"server log: {log_path.name}"]
    Path(f"{prefix}.md").write_text("\n".join(lines), encoding="utf-8")

    import csv
    with open(f"{prefix}_latency.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["id", "partial", "asr_s", "trans_s",
                                          "server_s", "e2e_s", "net_client_s"])
        w.writeheader()
        w.writerows(rows)

    print("\n".join(lines))
    print(f"\n[bench] report: {prefix}.md")


if __name__ == "__main__":
    main()
