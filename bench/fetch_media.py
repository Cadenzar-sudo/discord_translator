"""Fetch Korean-speech benchmark media from YouTube into bench_cache/.

Default sources are yt-dlp search queries (news anchors = clean continuous
Korean speech; searches never go stale). Pass any URLs/queries of your own —
gaming VODs with Korean comms are the ideal stress test.

    python fetch_media.py                      # default searches
    python fetch_media.py URL_OR_QUERY ...     # your picks
    python fetch_media.py --list               # what's cached

Files: {id}.m4a (audio) + {id}.ko.vtt (auto captions, reference text) and
media_index.json used by bench_e2e.py / compare_apps.py.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

BENCH = Path(__file__).resolve().parent
CACHE = BENCH / "bench_cache"

# Search queries, not hard-coded video ids: news VODs are continuous, clearly
# spoken Korean and the search always finds a fresh one.
DEFAULT_SOURCES = [
    "ytsearch1:KBS 뉴스 9",
    "ytsearch1:SBS 8 뉴스",
]


def run_ytdlp(args: list[str]) -> subprocess.CompletedProcess:
    venv_python = BENCH.parent / "server" / "venv" / "Scripts" / "python.exe"
    cmd = [str(venv_python), "-m", "yt_dlp", *args] if venv_python.exists() \
        else ["yt-dlp", *args]
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace")


def fetch(source: str, index: list[dict]) -> bool:
    print(f"[fetch] {source}")
    r = run_ytdlp([
        source,
        "-f", "bestaudio[ext=m4a]/bestaudio/best",
        "-o", str(CACHE / "%(id)s.%(ext)s"),
        "--write-auto-subs", "--sub-langs", "ko.-orig,ko", "--sub-format", "vtt",
        "--no-playlist", "--quiet", "--no-warnings", "--print",
        "after_move:%(id)s\t%(title)s\t%(ext)s",
    ])
    line = (r.stdout or "").strip().splitlines()
    if r.returncode != 0 or not line:
        print(f"[fetch]   FAILED: {(r.stderr or r.stdout).strip()[:200]}")
        return False
    vid, title, ext = (line[-1].split("\t") + ["?", "?"])[:3]
    audio = CACHE / f"{vid}.{ext}"
    subs = CACHE / f"{vid}.ko.vtt"
    for e in index:
        if e["id"] == vid:
            e.update(audio=str(audio.name), subs=str(subs.name) if subs.exists() else None,
                     title=title)
            return True
    index.append({"id": vid, "title": title, "url": source,
                  "audio": audio.name, "subs": subs.name if subs.exists() else None})
    print(f"[fetch]   {vid}: {title}  (subs: {'yes' if subs.exists() else 'NO'})")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sources", nargs="*", help="URLs or ytsearch queries")
    ap.add_argument("--list", action="store_true", help="list cached media")
    args = ap.parse_args()

    CACHE.mkdir(exist_ok=True)
    index_path = CACHE / "media_index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) \
        if index_path.exists() else []

    if args.list:
        for e in index:
            print(f"  {e['id']}: {e['title']}  audio={e['audio']} subs={e['subs']}")
        return

    sources = args.sources or DEFAULT_SOURCES
    ok = 0
    for s in sources:
        ok += fetch(s, index)
    index_path.write_text(json.dumps(index, indent=2, ensure_ascii=False),
                          encoding="utf-8")
    print(f"[fetch] {ok}/{len(sources)} fetched; {len(index)} in index -> {index_path}")


_VTT_LINE = re.compile(r"^\d\d:\d\d:\d\d[.,]")


def vtt_to_text(path: Path) -> str:
    """WebVTT -> plain text: strip cue metadata/tags, drop rolling duplicates."""
    out: list[str] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if (not line or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                or _VTT_LINE.match(line) or "-->" in line):
            continue
        line = re.sub(r"<[^>]+>", "", line)  # inline tags
        if not out or out[-1] != line:  # auto-subs repeat lines while rolling
            out.append(line)
    text = " ".join(out)
    return re.sub(r"\s+", " ", text).strip()


if __name__ == "__main__":
    main()
