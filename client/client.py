"""Overlay client: pulls subtitles from the bot server over WebSocket and
shows them in the same overlay the desktop app used.

First run asks for the one-line setup code (wss://host/ws#token) and your
language, then never asks again. Reconnects automatically with backoff; the
overlay shows a small "reconnecting…" banner while the server is unreachable.

    python client.py                normal start
    python client.py --setup        redo the first-run questions
    python client.py --bench out.jsonl   also log every event (latency bench)
"""

import argparse
import asyncio
import faulthandler  # startup-hang diagnosis: dumps all thread stacks
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

# In a PyInstaller --noconsole exe, sys.stdout/stderr can be None, a
# NullWriter without reconfigure(), or a working stream depending on how the
# app was launched — any of them breaks printing or reconfigure(). In the
# frozen exe, ALWAYS route both to a log file next to the exe unconditionally
# (doubles as the log you ask friends for when debugging).
if getattr(sys, "frozen", False):
    _log_dir = Path(sys.executable).resolve().parent
    sys.stdout = open(_log_dir / "overlay_log.txt", "a",
                      buffering=1, encoding="utf-8", errors="replace")
    sys.stderr = sys.stdout
else:
    for _name in ("stdout", "stderr"):
        _s = getattr(sys, _name)
        if _s is not None and hasattr(_s, "reconfigure"):
            _s.reconfigure(encoding="utf-8", errors="replace")

# if the client is stuck 30s after start, dump every thread's stack ONCE to
# the log — a healthy client is quiet after this
faulthandler.dump_traceback_later(30, repeat=False, file=sys.stderr)

# In a PyInstaller onefile exe, __file__ lives in the temp extraction dir —
# keep everything persistent next to the exe itself.
APP_DIR = (Path(sys.executable).resolve().parent
           if getattr(sys, "frozen", False)
           else Path(__file__).resolve().parent)
sys.path.insert(0, str(APP_DIR))

from config import TARGET_CHOICES, load_config, parse_setup_code, save_config  # noqa: E402


def first_run_setup(cfg: dict, ask, choose) -> bool:
    """Setup flow. `ask(prompt, default)` and `choose(prompt, options)` come
    from the console (dev/bat) or Qt dialogs (the windowed exe, where
    sys.stdin doesn't exist). Returns True if config now has a usable server."""
    print("=" * 56)
    print("  Translator overlay — first-time setup")
    print("=" * 56)
    url = (cfg.get("server") or {}).get("url", "")
    if url:
        change = (ask(f"Server already set ({url}). Change it? [y/N]", "") or "").strip().lower()
        if change != "y":
            return True
    while True:
        code = ask("Paste the setup code from the server owner\n"
                   "(looks like wss://subs.example.com/ws#t=SeCrEt&cid=...&csec=...)", "")
        if code is None:
            return False  # cancelled (exe)
        try:
            parsed = parse_setup_code(code.strip())
            if not (parsed["token"]
                    or (parsed["cf_client_id"] and parsed["cf_client_secret"])):
                tok = ask("That code had no token in it. Paste the access token\n"
                          "(or press ENTER if the server uses none)", "")
                if tok is None:
                    return False
                parsed["token"] = tok.strip()
            break
        except ValueError as e:
            print(f"  {e}")
            continue
    labels = [label for _, label in TARGET_CHOICES]
    pick = choose("Which language should subtitles be in?", labels)
    if pick is None:
        return False
    target = TARGET_CHOICES[labels.index(pick)][0]

    cfg["server"] = {"url": parsed["url"], "token": parsed["token"],
                     "cf_client_id": parsed["cf_client_id"],
                     "cf_client_secret": parsed["cf_client_secret"]}
    cfg["target_lang"] = target
    save_config(cfg)
    print(f"\nSaved to {APP_DIR / 'config.json'} — you're done. Starting…\n")
    return True


class SubClient:
    """Network half: WS receive loop -> result_q + optional JSONL bench log."""

    def __init__(self, cfg: dict, result_q: "queue.Queue", overlay, bench_path=None):
        self.cfg = cfg
        self.result_q = result_q
        self.overlay = overlay  # OverlayService (set_conn / flash) or None
        self.bench_path = bench_path
        self.stop_event = threading.Event()
        self._bench_f = open(bench_path, "a", encoding="utf-8") if bench_path else None
        self.connected = threading.Event()
        self._ws = None           # live aiohttp WS (set on the loop thread)
        self._loop = None
        self.state = {}           # latest server status fields (mode, language)

    def _on_event(self, ev: dict):
        recv = time.monotonic()
        if self._bench_f:
            self._bench_f.write(json.dumps(
                {"recv_mono": recv, "recv_wall": time.time(), **ev}) + "\n")
            self._bench_f.flush()
        if ev.get("type") == "status":
            self.state.update({"mode": ev.get("mode"),
                               "language": ev.get("language")})
            return
        if ev.get("type") == "error":
            if self.overlay:
                self.overlay.flash_badge(f"SERVER: {ev.get('error', '')[:40]}", 3.0)
            return
        if ev.get("type") != "subtitle":
            return
        item = {k: ev.get(k) for k in
                ("original", "translated", "lang", "ts", "id", "partial",
                 "speaker", "target")}
        item.setdefault("ts", time.time())
        try:
            self.result_q.put_nowait(item)
        except queue.Full:
            pass  # UI stalled; subtitles are ephemeral

    def send_json(self, msg: dict) -> bool:
        """Thread-safe send to the live websocket (hotkeys run on Qt threads).
        Returns False if not currently connected."""
        ws, loop = self._ws, self._loop
        if ws is None or loop is None or ws.closed:
            return False
        asyncio.run_coroutine_threadsafe(ws.send_json(msg), loop)
        return True

    async def _run(self):
        import aiohttp
        self._loop = asyncio.get_running_loop()
        srv = self.cfg["server"]
        url = srv["url"]
        hello = {"type": "hello", "target_lang": self.cfg["target_lang"]}
        if srv.get("admin_token"):
            hello["admin_token"] = srv["admin_token"]
        # App token rides as a Bearer header (not the URL, so it can't leak
        # into logs); the Cloudflare Access service token, when present, is
        # checked at Cloudflare's edge before the request reaches the server.
        headers = {}
        if srv.get("token"):
            headers["Authorization"] = f"Bearer {srv['token']}"
        if srv.get("cf_client_id") and srv.get("cf_client_secret"):
            headers["CF-Access-Client-Id"] = srv["cf_client_id"]
            headers["CF-Access-Client-Secret"] = srv["cf_client_secret"]
        backoff = 1.0
        fails = 0
        async with aiohttp.ClientSession() as session:
            while not self.stop_event.is_set():
                try:
                    # no heartbeat + no total timeout: control frames were
                    # getting eaten through the Cloudflare path, killing the
                    # socket every ~5min. The self-healing reconnect loop is
                    # the fallback if the socket truly dies.
                    async with session.ws_connect(
                            url, heartbeat=None,
                            timeout=aiohttp.ClientTimeout(total=None,
                                                          sock_connect=15),
                            headers=headers) as ws:
                        self._ws = ws
                        await ws.send_json(hello)
                        backoff = 1.0
                        fails = 0
                        self.connected.set()
                        if self.overlay:
                            self.overlay.set_conn(None)
                            self.overlay.set_connection_state(True)
                            self.overlay.flash_badge("CONNECTED", 1.5)
                        print(f"[client] Connected to {url} "
                              f"(target={self.cfg['target_lang']})")
                        async for msg in ws:
                            if self.stop_event.is_set():
                                return
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                try:
                                    self._on_event(msg.json())
                                except Exception:
                                    pass
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    fails += 1
                    # never-connecting must be visible too: log the first
                    # failure and every 10th, not just drops after a connect
                    if self.connected.is_set() or fails == 1 or fails % 10 == 0:
                        print(f"[client] connect failed "
                              f"(attempt {fails}): {e}")
                    if self.connected.is_set():
                        print(f"[client] Disconnected; reconnecting…")
                    self.connected.clear()
                    if self.overlay:
                        self.overlay.set_conn("reconnecting…")
                        self.overlay.set_connection_state(False)
                    # plain sleep: threading.Event.wait() would block the
                    # event loop and freeze the reconnect forever
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30.0)

    def start_background(self):
        def runner():
            try:
                asyncio.run(self._run())
            except Exception as e:
                print(f"[client] Network thread died: {e}")
                if self.overlay:
                    self.overlay.set_conn(f"error: {e}")

        t = threading.Thread(target=runner, daemon=True, name="SubClient")
        t.start()
        return t

    def stop(self):
        self.stop_event.set()
        if self._bench_f:
            self._bench_f.close()


def _console_ask(prompt: str, default: str = "") -> str | None:
    try:
        return input(f"{prompt}\n> ").strip() if prompt.endswith("\n") or "\n" in prompt \
            else input(f"{prompt} [{default}]: ").strip()
    except (EOFError, OSError, RuntimeError):
        return None


def _console_choose(prompt: str, options: list[str]) -> str | None:
    print(prompt)
    for i, o in enumerate(options, 1):
        print(f"  {i}. {o}")
    while True:
        pick = input(f"Choose 1-{len(options)} [1]: ").strip() or "1"
        if pick.isdigit() and 1 <= int(pick) <= len(options):
            return options[int(pick) - 1]
        print(f"  Enter a number 1-{len(options)}.")


def _qt_ask(prompt: str, default: str = "") -> str | None:
    from PyQt6.QtWidgets import QInputDialog
    text, ok = QInputDialog.getText(None, "Translator setup", prompt,
                                    text=default)
    return text.strip() if ok else None


def _qt_choose(prompt: str, options: list[str]) -> str | None:
    from PyQt6.QtWidgets import QInputDialog
    pick, ok = QInputDialog.getItem(None, "Translator setup", prompt, options,
                                    0, editable=False)
    return pick if ok else None


def main():
    ap = argparse.ArgumentParser(description="Subtitle overlay client")
    ap.add_argument("--setup", action="store_true", help="redo first-run setup")
    ap.add_argument("--bench", metavar="OUT.jsonl",
                    help="append every received event to this file (latency runs)")
    args = ap.parse_args()

    from PyQt6.QtWidgets import QApplication
    qapp = QApplication.instance() or QApplication(sys.argv)

    cfg = load_config()
    if args.setup or not (cfg.get("server") or {}).get("url"):
        # a windowed exe has no stdin: prompt via Qt dialogs instead
        if sys.stdin is not None:
            ask, choose = _console_ask, _console_choose
        else:
            ask, choose = _qt_ask, _qt_choose
        if not first_run_setup(cfg, ask, choose):
            return

    result_q: "queue.Queue" = queue.Queue(maxsize=200)
    paused = threading.Event()

    from overlay import OverlayService

    def toggle_mode():
        nxt = "game" if client.state.get("mode") != "game" else "chat"
        if client.send_json({"type": "set_mode", "profile": nxt}):
            service.win.flash(f"{nxt.upper()} MODE")
        else:
            service.win.flash("SERVER OFFLINE")

    def cycle_language():
        cycle = cfg.get("language_cycle", ["auto", "ko", "ja", "vi"])
        cur = client.state.get("language") or "auto"
        nxt = cycle[(cycle.index(cur) + 1) % len(cycle)] if cur in cycle \
            else cycle[0]
        if client.send_json({"type": "set_language", "language": nxt}):
            label = "AUTO" if nxt == "auto" else nxt.upper()
            service.set_language_label(label)
            service.win.flash(f"LANGUAGE: {label}", 3.0)
        else:
            service.win.flash("SERVER OFFLINE")

    service = OverlayService(result_q, cfg, paused,
                             on_mode=toggle_mode, on_language=cycle_language)
    print(f"[client] Overlay running (target={cfg['target_lang']}, "
          f"pause/hide via tray or hotkeys). Waiting for server…")

    client = SubClient(cfg, result_q, service, bench_path=args.bench)
    client.start_background()

    try:
        qapp.exec()
    finally:
        client.stop()
        print("[client] Bye")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # any startup crash must land in the overlay log for remote support
        import traceback
        traceback.print_exc()
        sys.stderr.flush()
        raise
