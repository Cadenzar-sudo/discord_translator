"""Subtitle overlay: always-on-top, click-through, follows the active window.

Run standalone to test visuals:  python overlay.py --demo [--screenshot out.png]
"""

import sys
import time

from PyQt6.QtCore import QObject, QRect, QRectF, Qt, QTimer
from PyQt6.QtCore import pyqtSignal as Signal
from PyQt6.QtGui import QAction, QColor, QFont, QFontMetrics, QIcon, QPainter, QPixmap
from PyQt6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

import queue as _queue

# ---------------------------------------------------------------- win32 helpers
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
GWL_EXSTYLE = -20
HWND_TOPMOST = -1
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040
MONITOR_DEFAULTTONEAREST = 2
DWMWA_EXTENDED_FRAME_BOUNDS = 9

import ctypes
from ctypes import wintypes

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)

# Full argtypes matter: HWND_TOPMOST is (HANDLE)-1, which without c_void_p
# gets truncated to a 32-bit int and SetWindowPos fails silently.
_user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_long, ctypes.c_long,
                                 ctypes.c_long, ctypes.c_long, ctypes.c_uint]
_user32.SetWindowPos.restype = ctypes.c_bool
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long
_USER32_FLAGS = SWP_NOACTIVATE | SWP_SHOWWINDOW


def _topmost():
    return ctypes.c_void_p(-1)


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", _RECT),
                ("rcWork", _RECT), ("dwFlags", wintypes.DWORD)]


def foreground_hwnd() -> int:
    return _user32.GetForegroundWindow()


def is_iconic(hwnd: int) -> bool:
    return bool(_user32.IsIconic(hwnd))


def window_visible_rect(hwnd: int):
    """Visible window bounds (excludes invisible resize borders) or None."""
    rect = _RECT()
    res = _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect))
    if res != 0:
        r = wintypes.RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return None
        rect = _RECT(r.left, r.top, r.right, r.bottom)
    if rect.right <= rect.left or rect.bottom <= rect.top:
        return None
    return rect.left, rect.top, rect.right, rect.bottom


def monitor_work_area(hwnd: int):
    hmon = _user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    info = _MONITORINFO()
    info.cbSize = ctypes.sizeof(info)
    if not _user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
        return None
    rc = info.rcWork
    return rc.left, rc.top, rc.right, rc.bottom


def set_click_through(hwnd: int, enabled: bool):
    ex = _user32.GetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    if enabled:
        ex |= WS_EX_TRANSPARENT | WS_EX_LAYERED | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    else:
        ex &= ~WS_EX_TRANSPARENT
    _user32.SetWindowLongW(ctypes.c_void_p(hwnd), GWL_EXSTYLE, ex)


def move_window(hwnd: int, x: int, y: int, w: int, h: int):
    ok = _user32.SetWindowPos(ctypes.c_void_p(hwnd), _topmost(), x, y, w, h,
                              _USER32_FLAGS)
    if not ok:
        print(f"[overlay] SetWindowPos failed (error {ctypes.get_last_error()})")


# ---------------------------------------------------------------- overlay window
class OverlayWindow(QWidget):
    def __init__(self, cfg: dict):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.overlay_cfg = cfg
        self.lines: list[dict] = []          # all unexpired entries
        self._view: list[dict] = []          # entries fitted to the size budget
        self._h_cap = 1 << 12                # px; set from the monitor by OverlayService
        self._user_hidden = False
        self.paused = False
        self._click_through = True
        self._flash_text: str | None = None
        self._flash_until = 0.0

        size = int(cfg.get("font_size", 15))
        self.font_main = QFont("Segoe UI", size)
        self.font_main.setBold(True)
        self.font_orig = QFont("Segoe UI", max(9, size - 2))
        self.font_orig.setItalic(True)

    # ---- content
    def set_lines(self, lines: list[dict]):
        self.lines = lines
        self.refresh()

    def set_paused(self, paused: bool):
        self.paused = paused
        self.refresh()

    def refresh(self):
        """Fit lines into the height budget, resize, show/hide the bar."""
        width = max(320, self.width())
        self._view = self._fitted_lines(width, self._h_cap)
        if not self._view and not self.paused:
            self.setFixedHeight(0)
            if not self._user_hidden and self.isVisible():
                self.hide()
            return
        if not self._user_hidden and not self.isVisible():
            self.show()
        self.setFixedHeight(min(self._measure(self._view, width), self._h_cap))

    def _fitted_lines(self, width: int, h_cap: int) -> list[dict]:
        """Newest lines that fit: cap line count, then drop oldest, then
        ellipsize a single over-long line. The bar can never exceed h_cap."""
        lines = self.lines[-int(self.overlay_cfg.get("max_lines", 3)):]
        while len(lines) > 1 and self._measure(lines, width) > h_cap:
            lines = lines[1:]
        if lines and self._measure(lines, width) > h_cap:
            lines = [self._shrink_line(lines[-1], width, h_cap)]
        return lines

    def _shrink_line(self, ln: dict, width: int, h_cap: int) -> dict:
        ln = dict(ln)
        ln.pop("original", None)  # drop the source line first — translation matters more
        for _ in range(32):
            if self._measure([ln], width) <= h_cap:
                break
            ln["translated"] = self._ellipsize(ln.get("translated", ""))
        return ln

    @staticmethod
    def _ellipsize(text: str, keep: float = 0.85) -> str:
        if len(text) <= 12:
            return text
        return text[: int(len(text) * keep)].rstrip() + "…"

    def _measure(self, lines: list[dict], width_logical: int) -> int:
        fm = QFontMetrics(self.font_main)
        fo = QFontMetrics(self.font_orig)
        avail = max(80, width_logical - 2 * 18)
        wrap = int(Qt.TextFlag.TextWordWrap)
        h = 12
        show_orig = self.overlay_cfg.get("show_original", True)
        for ln in lines:
            if show_orig and ln.get("original"):
                h += fo.boundingRect(QRect(0, 0, avail, 10000), wrap,
                                     self._orig_text(ln)).height() + 2
            h += fm.boundingRect(QRect(0, 0, avail, 10000), wrap,
                                 ln.get("translated", "")).height() + 10
        if self.paused:
            h += 26
        return int(h) + 12

    @staticmethod
    def _orig_text(ln: dict) -> str:
        lang = ln.get("lang") or "?"
        return f"[{lang}] {ln.get('original', '')}"

    def _apply_size(self):
        dpr = self.devicePixelRatioF() or 1.0
        w_logical = self.width()
        self.setFixedHeight(self.content_height(w_logical))

    # ---- painting
    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        w, h = self.width(), self.height()
        bg_alpha = int(self.overlay_cfg.get("bg_alpha", 175))

        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(12, 12, 18, bg_alpha))
        p.drawRoundedRect(QRectF(0.5, 0.5, w - 1, h - 1), 10, 10)

        if self.paused:
            p.setFont(self.font_orig)
            p.setPen(QColor(255, 210, 120, 220))
            p.drawText(QRectF(0, 8, w, 24), Qt.AlignmentFlag.AlignCenter,
                       "Paused  (Ctrl+Alt+P to resume)")

        if self._flash_text and time.time() < self._flash_until:
            p.setFont(self.font_orig)
            badge = self._flash_text
            bw = QFontMetrics(self.font_orig).horizontalAdvance(badge) + 24
            p.setBrush(QColor(90, 40, 8, 210))
            p.drawRoundedRect(QRectF(w - bw - 10, 8, bw, 24), 8, 8)
            p.setPen(QColor(255, 220, 160, 235))
            p.drawText(QRectF(w - bw - 10, 8, bw, 24),
                       Qt.AlignmentFlag.AlignCenter, badge)

        fm = QFontMetrics(self.font_main)
        fo = QFontMetrics(self.font_orig)
        pad_x, y = 18.0, 12.0
        avail = w - 2 * pad_x
        wrap = int(Qt.TextFlag.TextWordWrap)
        show_orig = self.overlay_cfg.get("show_original", True)
        for ln in self._view:
            if show_orig and ln.get("original"):
                p.setFont(self.font_orig)
                r = fo.boundingRect(QRect(0, 0, int(avail), 10000), wrap,
                                    self._orig_text(ln))
                p.setPen(QColor(168, 174, 190, 200))
                p.drawText(QRectF(pad_x, y, avail, r.height()),
                           Qt.TextFlag.TextWordWrap, self._orig_text(ln))
                y += r.height() + 2
            p.setFont(self.font_main)
            text = ln.get("translated", "")
            if ln.get("partial"):
                text = text.rstrip() + " …"
                p.setPen(QColor(235, 235, 245, 170))  # dimmed: still being decoded
            else:
                p.setPen(QColor(255, 255, 255, 245))
            r = fm.boundingRect(QRect(0, 0, int(avail), 10000), wrap, text)
            p.drawText(QRectF(pad_x, y, avail, r.height()),
                       Qt.TextFlag.TextWordWrap, text)
            y += r.height() + 10

    # ---- native behavior
    def showEvent(self, event):
        super().showEvent(event)
        hwnd = int(self.winId())
        if self._click_through:
            set_click_through(hwnd, True)

    def set_click_through_mode(self, enabled: bool):
        self._click_through = enabled
        set_click_through(int(self.winId()), enabled)

    def flash(self, text: str, seconds: float = 2.5):
        """Show a brief badge (mode switches etc.) without stealing focus."""
        self._flash_text = text
        self._flash_until = time.time() + seconds
        self.update()
        QTimer.singleShot(int(seconds * 1000) + 50, self.update)


# ---------------------------------------------------------------- controller
class _Bridge(QObject):
    fired = Signal(str)


class OverlayService(QObject):
    """Owns the overlay window, tray icon, hotkeys, and the results queue."""

    def __init__(self, result_q, cfg: dict, paused_event, parent=None,
                 on_result=None, on_mode=None, on_language=None):
        super().__init__(parent)
        self.cfg = cfg
        self.result_q = result_q
        self.paused_event = paused_event
        self.on_result = on_result
        self.on_mode = on_mode
        self.on_language = on_language
        oc = cfg["overlay"]

        self.win = OverlayWindow(oc)
        self.win.resize(900, 80)
        self.win.show()

        self._bridge = _Bridge()
        self._bridge.fired.connect(self._on_hotkey)
        import keyboard
        hk = oc.get("hotkeys", {})
        self._hotkeys = []
        for name, key in hk.items():
            try:
                keyboard.add_hotkey(key, lambda n=name: self._bridge.fired.emit(n))
                self._hotkeys.append((name, key))
            except Exception as e:
                print(f"[overlay] Hotkey {key} unavailable: {e}")

        self.tray = self._make_tray()

        self._poll = QTimer(self)
        self._poll.timeout.connect(self._poll_results)
        self._poll.start(60)
        self._expire = QTimer(self)
        self._expire.timeout.connect(self._expire_lines)
        self._expire.start(300)
        self._follow = QTimer(self)
        self._follow.timeout.connect(self._follow_foreground)
        self._follow.start(300)

        self._last_hwnd = 0
        self._reposition(force=True)

    # ---- tray
    def _make_tray(self) -> QSystemTrayIcon:
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor(28, 30, 44))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.setPen(QColor(255, 255, 255))
        f = QFont("Segoe UI", 20)
        f.setBold(True)
        p.setFont(f)
        p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "あA")
        p.end()

        menu = QMenu()
        self._act_pause = QAction("Paused", menu, checkable=True)
        self._act_pause.toggled.connect(self.set_paused)
        self._act_interact = QAction("Interaction mode", menu, checkable=True)
        self._act_interact.toggled.connect(self.set_interaction)
        self._act_vis = QAction("Hide overlay", menu)
        self._act_vis.triggered.connect(self.toggle_visible)
        self._act_chat = QAction("Chat mode (accurate)", menu, checkable=True)
        self._act_game = QAction("Game mode (fast callouts)", menu, checkable=True)
        self._act_chat.triggered.connect(lambda: self._set_mode("chat"))
        self._act_game.triggered.connect(lambda: self._set_mode("game"))
        self._sync_mode_actions()
        act_quit = QAction("Quit", menu)
        act_quit.triggered.connect(self.quit)
        for a in (self._act_pause, self._act_interact, self._act_vis):
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(self._act_chat)
        menu.addAction(self._act_game)
        menu.addSeparator()
        menu.addAction(act_quit)

        tray = QSystemTrayIcon(QIcon(pm))
        tray.setContextMenu(menu)
        tray.setToolTip("Discord voice translator")
        tray.activated.connect(self._tray_activated)
        tray.show()
        return tray

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.toggle_visible()

    # ---- hotkey actions
    def _on_hotkey(self, name: str):
        if name == "pause":
            self._act_pause.setChecked(not self._act_pause.isChecked())
        elif name == "toggle":
            self.toggle_visible()
        elif name == "interact":
            self._act_interact.setChecked(not self._act_interact.isChecked())
        elif name == "mode" and self.on_mode:
            self.on_mode()
            self._sync_mode_actions()
        elif name == "language" and self.on_language:
            self.on_language()
        elif name == "quit":
            self.quit()

    def _sync_mode_actions(self):
        prof = self.cfg.get("profile", "chat")
        for act, name in ((self._act_chat, "chat"), (self._act_game, "game")):
            act.setChecked(name == prof)

    def _set_mode(self, want: str):
        if self.cfg.get("profile") != want and self.on_mode:
            self.on_mode()
            self._sync_mode_actions()

    def set_paused(self, on: bool):
        self._act_pause.setChecked(on)
        self.paused_event.set() if on else self.paused_event.clear()
        self.win.set_paused(on)
        print("[overlay] Paused" if on else "[overlay] Resumed")

    def set_interaction(self, on: bool):
        self._act_interact.setChecked(on)
        self.win.set_click_through_mode(not on)
        print("[overlay] Interaction mode ON (right-click overlay for menu)"
              if on else "[overlay] Interaction mode OFF (click-through)")

    def toggle_visible(self):
        self.win._user_hidden = not self.win._user_hidden
        self.win.setVisible(not self.win._user_hidden)

    def quit(self):
        try:
            import keyboard
            keyboard.unhook_all()
        except Exception:
            pass
        self.tray.hide()
        self.win.close()
        QApplication.instance().quit()

    # ---- results
    def _poll_results(self):
        fresh = False
        while True:
            try:
                item = self.result_q.get_nowait()
            except _queue.Empty:
                break
            if item is None:
                continue
            entries = item.get("lines") if isinstance(item, dict) and "lines" in item else [item]
            for e in entries:
                uid = e.get("id")
                if uid is not None:
                    for existing in self.win.lines:
                        if existing.get("id") == uid:
                            existing.update(e)  # provisional update in place
                            break
                    else:
                        self.win.lines.append(e)
                else:
                    self.win.lines.append(e)
                if self.on_result:
                    self.on_result(e)
            fresh = True
        if fresh:
            self._trim_lines()
            self.win.set_lines(list(self.win.lines))
            self._reposition()  # re-pin to the bottom the moment content changes

    def _trim_lines(self):
        max_lines = int(self.cfg["overlay"].get("max_lines", 3))
        if len(self.win.lines) > max_lines:
            self.win.lines = self.win.lines[-max_lines:]

    def _expire_lines(self):
        timeout = float(self.cfg["overlay"].get("timeout_s", 9))
        now = time.time()
        # provisionals get 2x: they must survive the gap between partial updates
        keep = [ln for ln in self.win.lines
                if now - ln.get("ts", now) < timeout * (2 if ln.get("partial") else 1)]
        if len(keep) != len(self.win.lines):
            self.win.lines = keep
            self.win.set_lines(list(keep))

    # ---- window following
    def _follow_foreground(self):
        hwnd = foreground_hwnd()
        if not hwnd or hwnd == int(self.win.winId()) or is_iconic(hwnd):
            return
        self._reposition()

    def _reposition(self, force: bool = False):
        hwnd = foreground_hwnd() or self._last_hwnd
        mine = int(self.win.winId())
        if not hwnd or hwnd == mine:
            return
        self._last_hwnd = hwnd
        rect = window_visible_rect(hwnd)
        work = monitor_work_area(hwnd)
        if not rect or not work:
            return
        wl, wt, wr, wb = rect
        kl, kt, kr, kb = work
        dpr = self.win.devicePixelRatioF() or 1.0
        frac = float(self.cfg["overlay"].get("width_frac", 0.62))
        margin = int(self.cfg["overlay"].get("margin", 28))
        # size budget: never cover more than max_height_frac of the screen
        self.win._h_cap = int((kb - kt) * float(self.cfg["overlay"].get("max_height_frac", 0.18)))

        w_phys = int((kr - kl) * frac)
        w_logical = max(320, int(w_phys / dpr))
        if self.win.width() != w_logical:
            self.win.resize(w_logical, self.win.height())
        self.win.refresh()
        h_phys = int(self.win.height() * dpr)

        # anchor: "screen" = bottom of the active monitor (subtitle style, default);
        #         "window" = bottom edge of the focused window (floats mid-screen when windowed)
        anchor = self.cfg["overlay"].get("anchor", "screen")
        if anchor == "window":
            y = wb - h_phys - margin
        else:
            y = kb - h_phys - margin
        y = max(kt, min(y, kb - h_phys))
        x = kl + (kr - kl - w_phys) // 2
        x = max(kl, min(x, kr - w_phys))
        move_window(mine, x, y, w_phys, h_phys)


# ---------------------------------------------------------------- demo entry
DEMO_LINES = [
    {"original": "안녕하세요, 오늘 게임 하실래요?", "translated": "Hi, do you want to play games today?",
     "lang": "ko", "ts": time.time()},
    {"original": "ちょっと待って、弾薬がない", "translated": "Wait a moment, I'm out of ammo",
     "lang": "ja", "ts": time.time()},
]


def demo(screenshot: str | None = None, quit_after: float = 0.0):
    import threading
    from config import DEFAULTS

    cfg = {"overlay": DEFAULTS["overlay"]}
    app = QApplication(sys.argv)
    service = OverlayService(_queue.Queue(), cfg, threading.Event())
    service.win.lines = [dict(l, ts=time.time()) for l in DEMO_LINES]
    service.win.set_lines(list(service.win.lines))

    if screenshot:
        def snap():
            pm = service.win.grab()
            pm.save(screenshot)
            print(f"[demo] Screenshot saved: {screenshot}")
            app.quit()
        QTimer.singleShot(900, snap)
    if quit_after > 0:
        QTimer.singleShot(int(quit_after * 1000), app.quit)
    if not screenshot and quit_after <= 0:
        print("[demo] Overlay running. Hotkeys: pause ctrl+alt+p | hide ctrl+alt+o | "
              "interact ctrl+alt+i | quit ctrl+alt+q. Tray icon is in the system tray.")
    sys.exit(app.exec())


if __name__ == "__main__":
    args = sys.argv[1:]
    demo(screenshot=args[args.index("--screenshot") + 1] if "--screenshot" in args else None,
         quit_after=float(args[args.index("--quit-after") + 1]) if "--quit-after" in args else 0.0)
