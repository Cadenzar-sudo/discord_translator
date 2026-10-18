"""Subtitle server: token-authed WebSocket push + /health, riding the same
asyncio loop as the Discord bot.

The pipeline thread hands subtitle events to thread_emit(); they are routed to
each connection by that viewer's target language (everything else — status,
welcome, pong — goes to all). Slow consumers drop messages rather than stall
the pipeline: subtitles are ephemeral.
"""

import asyncio
import itertools
import time

from aiohttp import web, WSMsgType

from translator import WHISPER_TO_NLLB

VALID_TARGETS = set(WHISPER_TO_NLLB.values())


class SubtitleServer:
    def __init__(self, cfg: dict, registry, status_provider, on_set_mode=None,
                 on_set_language=None):
        self.cfg = cfg
        self.registry = registry
        self.status_provider = status_provider  # callable() -> dict
        self.on_set_mode = on_set_mode  # callable(profile: str) -> None
        self.on_set_language = on_set_language  # callable(lang: str|None) -> None
        self._conns: dict[int, dict] = {}
        self._ids = itertools.count(1)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._runner: web.AppRunner | None = None
        self._status_task: asyncio.Task | None = None

    # ------------------------------------------------------------ lifecycle
    async def start(self):
        self._loop = asyncio.get_running_loop()
        app = web.Application()
        app.router.add_get("/ws", self._ws_handler)
        app.router.add_get("/health", self._health)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self.cfg["server"]["host"],
                           int(self.cfg["server"]["port"]))
        await site.start()
        self._status_task = self._loop.create_task(self._status_loop())
        host = self.cfg["server"]["host"]
        print(f"[server] Subtitles on ws://{host}:{self.cfg['server']['port']}/ws "
              f"(auth {'ON' if self.cfg['server']['access_token'] else 'OFF — set a token!'})")

    async def stop(self):
        if self._status_task:
            self._status_task.cancel()
        for conn in list(self._conns.values()):
            conn["q"].put_nowait(None)  # wake writers so they exit
        if self._runner:
            await self._runner.cleanup()

    # ---------------------------------------------------- thread bridge
    def thread_emit(self, event: dict):
        """Safe to call from the pipeline thread."""
        if self._loop is not None:
            self._loop.call_soon_threadsafe(self.broadcast, event)

    def broadcast(self, event: dict):
        """Route one event; runs on the loop thread."""
        if event.get("type") == "subtitle":
            target = event.get("target")
            for conn in self._conns.values():
                if conn["target"] == target:
                    self._offer(conn, event)
        else:
            for conn in self._conns.values():
                self._offer(conn, event)

    @staticmethod
    def _offer(conn: dict, event: dict):
        try:
            conn["q"].put_nowait(event)
        except asyncio.QueueFull:
            pass  # this viewer is stalled; subtitles are ephemeral — drop

    async def _status_loop(self):
        while True:
            await asyncio.sleep(5.0)
            self.broadcast({"type": "status", "ts": time.time(),
                            **self.safe_status()})

    def safe_status(self) -> dict:
        try:
            return dict(self.status_provider())
        except Exception as e:
            return {"error": str(e)}

    # ------------------------------------------------------------ handlers
    async def _health(self, request):
        return web.json_response(self.safe_status())

    def _authorized(self, request) -> bool:
        """App-token check. The token normally arrives as an Authorization:
        Bearer header; the ?token= query stays supported for curl and LAN
        testing. (Cloudflare Access, if configured, authenticates at the
        edge before requests ever reach here — see README.)"""
        token = self.cfg["server"].get("access_token") or ""
        if not token:
            return True
        if request.headers.get("Authorization", "") == f"Bearer {token}":
            return True
        return request.query.get("token") == token

    async def _ws_handler(self, request):
        if not self._authorized(request):
            raise web.HTTPUnauthorized(text="bad or missing token")
        ws = web.WebSocketResponse(heartbeat=25.0)
        await ws.prepare(request)

        # hello: the viewer declares its target language
        try:
            msg = await asyncio.wait_for(ws.receive_json(), timeout=10.0)
        except Exception:
            await ws.close(code=4001, message=b"expected hello")
            return ws
        target = msg.get("target_lang") if msg.get("type") == "hello" else None
        if target not in VALID_TARGETS:
            await ws.send_json({"type": "error", "error":
                                f"unknown target_lang {target!r}"})
            await ws.close(code=4002, message=b"bad target_lang")
            return ws

        cid = next(self._ids)
        admin = (msg.get("admin_token")
                 and msg.get("admin_token") == self.cfg["server"].get("admin_token")
                 and bool(self.cfg["server"].get("admin_token")))
        conn = {"ws": ws, "q": asyncio.Queue(maxsize=200), "target": target,
                "admin": admin}
        self._conns[cid] = conn
        self.registry.set(cid, target)
        conn["q"].put_nowait({"type": "welcome", "ts": time.time(),
                              "target": target, "admin": admin,
                              **self.safe_status()})
        print(f"[server] Viewer #{cid} connected (target={target}, "
              f"admin={'yes' if admin else 'no'}); {len(self._conns)} total")

        writer = asyncio.create_task(self._writer(conn))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    m = msg.json()
                except Exception:
                    continue
                if m.get("type") == "ping":  # app-level RTT probe
                    self._offer(conn, {"type": "pong", "t": m.get("t"),
                                       "ts": time.time()})
                elif m.get("type") in ("set_mode", "set_language"):
                    if not self._may_control(conn, m.get("type")):
                        continue
                    if m["type"] == "set_mode" and self.on_set_mode:
                        profile = m.get("profile")
                        if profile in ("chat", "game"):
                            self.on_set_mode(profile)
                            self.broadcast({"type": "status",
                                            **self.safe_status()})
                    elif m["type"] == "set_language" and self.on_set_language:
                        lang = m.get("language")
                        if lang in (None, "auto") or (
                                isinstance(lang, str) and len(lang) == 2
                                and lang.isalpha()):
                            self.on_set_language(None if lang in (None, "auto")
                                                 else lang)
                            self.broadcast({"type": "status",
                                            **self.safe_status()})
        finally:
            writer.cancel()
            self._conns.pop(cid, None)
            self.registry.remove(cid)
            print(f"[server] Viewer #{cid} disconnected "
                  f"(code={ws.close_code}, msg={ws.close_message!r}); "
                  f"{len(self._conns)} left")
        return ws

    def _may_control(self, conn: dict, kind: str) -> bool:
        """set_mode/set_language are admin-only when an admin token is set.
        Friends without it get an explanatory status line instead."""
        required = self.cfg["server"].get("admin_token") or ""
        if not required or conn.get("admin"):
            return True
        self._offer(conn, {"type": "error", "error":
                           f"{kind} is reserved for the server owner"})
        return False

    async def _writer(self, conn: dict):
        ws, q = conn["ws"], conn["q"]
        while True:
            event = await q.get()
            if event is None:
                return
            try:
                await ws.send_json(event)
            except Exception:
                return  # socket died; the reader loop will clean up
