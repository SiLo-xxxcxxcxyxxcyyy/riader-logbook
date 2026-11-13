"""Screen capture for the scanner: find the ARC Raiders window and grab just that.

Grabbing a whole monitor picks up whatever is on top of it (a browser, Claude, Discord), and on
multi-monitor setups the game is often not on the primary monitor. So on Windows we look for the
game's window (process PioneerGame*.exe or a window titled "ARC Raiders"), bring it to the front if
needed, and capture its rectangle. Anything else falls back to the chosen monitor.

Updated through update/manifest.json like the scanner, so capture fixes need no new .exe.
"""
from __future__ import annotations

import os
import threading
import time

import mss
import numpy as np

BROWSERS = ("chrome", "msedge", "firefox", "opera", "brave", "vivaldi", "claude", "discord", "explorer",
            "raiderlogbook", "python")


def _windows_game_window():
    """Return (hwnd, (left, top, right, bottom), title, exe) of the game window, or None."""
    import ctypes
    from ctypes import wintypes
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    found = []

    def exe_of(hwnd):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        h = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024); n = wintypes.DWORD(1024)
            return os.path.basename(buf.value).lower() if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else ""
        finally:
            kernel32.CloseHandle(h)

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1); user32.GetWindowTextW(hwnd, buf, n + 1)
        title, exe = buf.value.strip(), exe_of(hwnd)
        if any(b in exe for b in BROWSERS):
            return True
        score = 0
        if exe.startswith("pioneergame"):
            score = 3
        elif title.lower() in ("arc raiders", "arcraiders"):
            score = 2
        elif title.lower().startswith("arc raiders") and "logbook" not in title.lower():
            score = 1
        if score:
            r = wintypes.RECT(); user32.GetWindowRect(hwnd, ctypes.byref(r))
            area = max(0, r.right - r.left) * max(0, r.bottom - r.top)
            if area > 200 * 200:
                found.append((score, area, hwnd, (r.left, r.top, r.right, r.bottom), title, exe))
        return True

    user32.EnumWindows(cb, 0)
    if not found:
        return None
    found.sort(reverse=True)
    _, _, hwnd, rect, title, exe = found[0]
    return hwnd, rect, title, exe


def _client_rect(hwnd):
    """The window's drawable area in screen pixels (skips the title bar of windowed mode)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    r = wintypes.RECT(); user32.GetClientRect(hwnd, ctypes.byref(r))
    pt = wintypes.POINT(0, 0); user32.ClientToScreen(hwnd, ctypes.byref(pt))
    return pt.x, pt.y, pt.x + r.right, pt.y + r.bottom


def enable_dpi_awareness():
    """Make window coordinates real pixels so they match the screenshot (call once at start)."""
    if os.name != "nt":
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


_LAST = {"hash": None}


_CAMS = {}


def _dxgi_output_for(region):
    """(device_idx, output_idx, monitor rect) of the DXGI output that holds the region's centre.
    DXGI numbers outputs differently from mss/EnumDisplayMonitors (on a laptop with an external
    primary monitor mss says [laptop, primary] while DXGI says [primary, laptop]), so match by the
    output's own desktop coordinates instead of by index."""
    import dxcam
    factory = getattr(dxcam, "__factory")
    cx, cy = region["left"] + region["width"] // 2, region["top"] + region["height"] // 2
    first = None
    for d, outs in enumerate(factory.outputs):
        for i, o in enumerate(outs):
            o.update_desc()
            c = o.desc.DesktopCoordinates
            m = {"left": c.left, "top": c.top, "width": c.right - c.left, "height": c.bottom - c.top}
            first = first or (d, i, m)
            if c.left <= cx < c.right and c.top <= cy < c.bottom:
                return d, i, m
    if first is None:
        raise RuntimeError("no DXGI output found")
    return first


def _dxgi_grab(region):
    """Capture through DXGI Desktop Duplication (what recording tools use). Unlike GDI it sees the
    current frame of fullscreen/borderless games instead of an old, frozen one."""
    import dxcam
    dev, idx, m = _dxgi_output_for(region)
    key = (dev, idx)
    cam = _CAMS.get(key)
    if cam is None or getattr(cam, "is_released", False):
        cam = _CAMS[key] = dxcam.create(device_idx=dev, output_idx=idx, output_color="BGR")
    l, t = region["left"] - m["left"], region["top"] - m["top"]
    box = (max(0, l), max(0, t), min(m["width"], l + region["width"]), min(m["height"], t + region["height"]))
    out = {}
    cam.start(region=box, target_fps=30, video_mode=True)
    try:
        th = threading.Thread(target=lambda: out.setdefault("f", cam.get_latest_frame()), daemon=True)
        th.start()
        th.join(2.0)                                 # get_latest_frame blocks until a frame arrives
    finally:
        cam.stop()                                   # also releases a still-waiting get_latest_frame
    return out.get("f")


def grab(monitor=1, bring_to_front=False):
    """Screenshot of the game window if it can be found, else of the given monitor.
    Returns (BGR image, info dict)."""
    info = {"target": "monitor", "monitor": monitor}
    region = None
    if os.name == "nt":
        try:
            g = _windows_game_window()
        except Exception as e:
            g, info["error"] = None, str(e)
        if g:
            hwnd, _rect, title, exe = g
            import ctypes
            user32 = ctypes.windll.user32
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)                          # SW_RESTORE
                bring_to_front = True
            if bring_to_front or user32.GetForegroundWindow() != hwnd:
                user32.SetForegroundWindow(hwnd)
                time.sleep(0.35)
            l, t, r, b = _client_rect(hwnd)
            if r - l > 200 and b - t > 200:
                region = {"left": l, "top": t, "width": r - l, "height": b - t}
                info = {"target": "game", "title": title, "exe": exe, "size": f"{r - l}x{b - t}"}
            if user32.GetForegroundWindow() != hwnd:
                info["warning"] = "Another window may be covering the game. Press F9 while the game is in front."
    with mss.mss() as sct:
        if region is None:
            mon = sct.monitors[min(monitor, len(sct.monitors) - 1)]
            region = {k: mon[k] for k in ("left", "top", "width", "height")}
            info["size"] = f"{mon['width']}x{mon['height']}"
            info["note"] = "Game window not found, scanned the whole monitor."
    shot = None
    if os.name == "nt":
        try:
            shot = _dxgi_grab(region)
            info["method"] = "dxgi"
        except Exception as e:
            info["dxgi_error"] = str(e)[:200]
    if shot is None:
        with mss.mss() as sct:
            shot = np.array(sct.grab(region))[:, :, :3]
        info["method"] = "gdi"
    shot = np.ascontiguousarray(shot)
    h = hash(shot[::16, ::16].tobytes())
    if h == _LAST["hash"]:
        info["warning"] = (info.get("warning", "") + " The screenshot is identical to the previous scan: "
                           "if you scrolled or switched tabs, the game picture didn't refresh.").strip()
    _LAST["hash"] = h
    return shot, info


def exclude_from_capture(hwnd):
    """Keep one of our windows out of screenshots and screen capture (Windows 10 2004+), so the
    overlay never has to hide while the game is read. Returns True if Windows accepted it."""
    if os.name != "nt" or not hwnd:
        return False
    import ctypes
    WDA_EXCLUDEFROMCAPTURE = 0x11
    try:
        return bool(ctypes.windll.user32.SetWindowDisplayAffinity(ctypes.c_void_p(int(hwnd)), WDA_EXCLUDEFROMCAPTURE))
    except Exception:
        return False


class Stream:
    """A few frames a second of the game window, for the background watcher. Only while the game is
    the foreground window (in a menu you're using it; tabbed out, nothing is read)."""

    def __init__(self, fps=4):
        self.fps, self.cam, self.key = fps, None, None
        self._game, self._game_at = None, 0.0
        self.require_front = True
        self.origin = (0, 0)

    def _find(self):
        if time.time() - self._game_at > 2.0:
            self._game, self._game_at = _windows_game_window(), time.time()
        return self._game

    def frame(self):
        """(BGR frame or None, reason). Blocks for at most about one frame interval."""
        if os.name != "nt":
            return None, "not windows"
        import ctypes
        g = self._find()
        if not g:
            self.stop()
            return None, "game not running"
        hwnd = g[0]
        if self.require_front and not self._game_in_front(hwnd):
            self.stop()
            return None, "game in background"
        l, t, r, b = _client_rect(hwnd)
        if r - l < 200 or b - t < 200:
            return None, "game minimised"
        region = {"left": l, "top": t, "width": r - l, "height": b - t}
        dev, idx, m = _dxgi_output_for(region)
        x0, y0 = region["left"] - m["left"], region["top"] - m["top"]
        self.origin = (m["left"] + max(0, x0), m["top"] + max(0, y0))      # screen position of the frame
        box = (max(0, x0), max(0, y0), min(m["width"], x0 + region["width"]), min(m["height"], y0 + region["height"]))
        key = (dev, idx, box)
        if key != self.key:
            self.stop()
            import dxcam
            cam = _CAMS.get((dev, idx))
            if cam is None or getattr(cam, "is_released", False):
                cam = _CAMS[(dev, idx)] = dxcam.create(device_idx=dev, output_idx=idx, output_color="BGR")
            cam.start(region=box, target_fps=self.fps, video_mode=True)
            self.cam, self.key = cam, key
        out = {}
        th = threading.Thread(target=lambda: out.setdefault("f", self.cam.get_latest_frame()), daemon=True)
        th.start()
        th.join(2.0)
        f = out.get("f")
        return (np.ascontiguousarray(f), "ok") if f is not None else (None, "no frame")

    def _game_in_front(self, game):
        """The game is the foreground window, or one of our own windows is (the overlay, the in-game
        marks: they sit on top of the game) and the last other window in front was the game."""
        import ctypes
        from ctypes import wintypes
        fg = ctypes.windll.user32.GetForegroundWindow()
        if fg == game:
            self._last_other = game
            return True
        pid = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(fg, ctypes.byref(pid))
        if pid.value == os.getpid() or not fg:
            return getattr(self, "_last_other", None) == game
        self._last_other = fg
        return False

    def stop(self):
        if self.cam is not None:
            try:
                self.cam.stop()
            except Exception:
                pass
        self.cam, self.key = None, None
