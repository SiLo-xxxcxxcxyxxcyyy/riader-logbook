"""Screen capture for the scanner: find the ARC Raiders window and grab just that.

Grabbing a whole monitor picks up whatever is on top of it (a browser, Claude, Discord), and on
multi-monitor setups the game is often not on the primary monitor. So on Windows we look for the
game's window (process PioneerGame*.exe or a window titled "ARC Raiders"), bring it to the front if
needed, and capture its rectangle. Anything else falls back to the chosen monitor.

Updated through update/manifest.json like the scanner, so capture fixes need no new .exe.
"""
from __future__ import annotations

import os
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
            region = mon
            info["size"] = f"{mon['width']}x{mon['height']}"
            info["note"] = "Game window not found, scanned the whole monitor."
        shot = np.array(sct.grab(region))[:, :, :3]
    return np.ascontiguousarray(shot), info
