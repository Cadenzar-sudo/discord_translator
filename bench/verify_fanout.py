"""One-off verification: two viewers (eng + kor) get their own translations."""
import asyncio, subprocess, sys, time
from pathlib import Path

BENCH = Path(__file__).resolve().parent
SERVER = BENCH.parent / "server"
SPY = SERVER / "venv" / "Scripts" / "python.exe"
PORT = 18932

def ts():
    return f"{time.monotonic()%1000:7.2f}"

async def viewer(port, target, sink):
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.ws_connect(f"ws://127.0.0.1:{port}/ws") as ws:
            await ws.send_json({"type": "hello", "target_lang": target})
            print(f"{ts()} [{target}] connected+hello sent")
            while True:
                msg = await ws.receive(timeout=60)
                if msg.type != aiohttp.WSMsgType.TEXT:
                    print(f"{ts()} [{target}] closed: {msg.type}")
                    return
                ev = msg.json()
                if ev.get("type") == "welcome":
                    print(f"{ts()} [{target}] welcome viewers={ev.get('viewer_count')}")
                elif ev.get("type") == "status":
                    print(f"{ts()} [{target}] status viewers={ev.get('viewer_count')} mode={ev.get('mode')}")
                elif ev.get("type") == "subtitle":
                    sink.append(ev)

async def main():
    proc = subprocess.Popen(
        [str(SPY), str(SERVER / "main.py"), "--test-file",
         str(BENCH / "bench_cache" / "test_ko_long.wav"),
         "--speaker", "Bench", "--fast", "--port", str(PORT)],
        cwd=str(SERVER),
        stdout=open(BENCH / "bench_out" / "fanout_server.log", "w",
                    encoding="utf-8"),
        stderr=subprocess.STDOUT)
    eng, kor = [], []
    import aiohttp
    try:
        async with aiohttp.ClientSession() as s:
            for _ in range(600):
                try:
                    async with s.get(f"http://127.0.0.1:{PORT}/health",
                                     timeout=aiohttp.ClientTimeout(total=2)) as r:
                        if (await r.json()).get("ready"):
                            print(f"{ts()} [main] server ready")
                            break
                except Exception:
                    pass
                await asyncio.sleep(1)
        a = asyncio.create_task(viewer(PORT, "eng_Latn", eng))
        b = asyncio.create_task(viewer(PORT, "kor_Hang", kor))
        await asyncio.wait_for(asyncio.gather(a, b), timeout=300)
    finally:
        try:
            proc.wait(timeout=60)   # clean exit flushes its stdout log
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"\n== ENGLISH viewer ({len(eng)} events) ==")
    for e in eng:
        kind = "P" if e.get("partial") else "F"
        print(f"  [{kind}->{e['target']}] {e['original'][:36]} => {e['translated'][:60]}")
    print(f"\n== KOREAN viewer ({len(kor)} events) ==")
    for e in kor:
        kind = "P" if e.get("partial") else "F"
        print(f"  [{kind}->{e['target']}] {e['original'][:36]} => {e['translated'][:60]}")

asyncio.run(main())
