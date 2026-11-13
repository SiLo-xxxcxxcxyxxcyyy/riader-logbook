"""In-game marks: a see-through, click-through layer over the game window that shows what the
automatic reading has recognised (a tick and the value on every slot read, a red "?" on a slot it
couldn't name), so you can see while scrolling the stash that everything was picked up.

A plain Win32 layered window (WS_EX_LAYERED | WS_EX_TRANSPARENT): mouse clicks go through to the
game, it never takes focus, and it is excluded from screen capture so the scanner never sees it.
Drawing is a BGRA picture made with Pillow, handed over with UpdateLayeredWindow. Windows only.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import wintypes

import numpy as np

WS_POPUP = 0x80000000
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x08000000
SW_SHOWNOACTIVATE, SW_HIDE = 4, 0
WM_APP, WM_DESTROY = 0x8000, 0x0002
ULW_ALPHA, AC_SRC_ALPHA = 0x2, 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11

GREEN, RED, INK = (46, 204, 113), (231, 76, 60), (255, 255, 255)


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", ctypes.c_byte), ("BlendFlags", ctypes.c_byte),
                ("SourceConstantAlpha", ctypes.c_byte), ("AlphaFormat", ctypes.c_byte)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD), ("biClrImportant", wintypes.DWORD)]


WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]


def _font(size):
    from PIL import ImageFont
    for name in ("segoeuib.ttf", "bahnschrift.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def short_value(v):
    if v >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if v >= 10_000:
        return f"{v / 1000:.0f}k"
    if v >= 1000:
        return f"{v / 1000:.1f}k".replace(".0k", "k")
    return str(int(v))


class Marks:
    """Call show(rect, slots) / clear() from any thread; the window lives on its own thread."""

    def __init__(self):
        self.enabled = True
        self._hwnd = None
        self._pending = None            # (rect, BGRA image) or None to hide
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._shown = False
        self._fonts = {}
        if os.name == "nt":
            threading.Thread(target=self._loop, daemon=True).start()
            self._ready.wait(3.0)

    # ---- public ----
    def show(self, rect, slots, status=None):
        """rect: (left, top, width, height) of the game picture on screen. slots: dicts with
        "box" (x, y, w, h in that picture), "ok" (recognised?) and "label" (the value). status:
        (text, colour name, (x, y) in the picture) for a pill above the stash, or None."""
        if not self.enabled or not self._hwnd:
            return
        img = self._draw(rect[2], rect[3], slots, status)
        with self._lock:
            self._pending = (rect, img)
        ctypes.windll.user32.PostMessageW(self._hwnd, WM_APP, 0, 0)

    def clear(self):
        if not self._hwnd or not self._shown:
            return
        with self._lock:
            self._pending = None
        ctypes.windll.user32.PostMessageW(self._hwnd, WM_APP, 0, 0)

    # ---- drawing ----
    def _f(self, size):
        if size not in self._fonts:
            self._fonts[size] = _font(size)
        return self._fonts[size]

    def _draw(self, w, h, slots, status=None):
        from PIL import Image, ImageDraw
        im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(im)
        if status:
            text, colour, (sx, sy) = status
            f = self._f(max(13, h // 60))
            l, t, r, b = d.textbbox((sx, sy), text, font=f, anchor="lm")
            bg = {"ok": (39, 174, 96), "warn": (211, 84, 0), "busy": (40, 44, 52)}.get(colour, (40, 44, 52))
            d.rounded_rectangle((l - 10, t - 6, r + 10, b + 6), radius=(b - t) // 2 + 6, fill=bg + (235,))
            d.text((sx, sy), text, font=f, fill=(255, 255, 255, 255), anchor="lm")
        for s in slots:
            x, y, bw, bh = s["box"]
            r = max(7, bw // 7)
            cx, cy = x + bw - r - 3, y + r + 3                     # badge in the top-right corner
            col = GREEN if s.get("ok") else RED
            d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=col + (235,), outline=(0, 0, 0, 160), width=1)
            if s.get("ok"):
                lw = max(2, r // 3)
                d.line([(cx - r * 0.5, cy), (cx - r * 0.1, cy + r * 0.45), (cx + r * 0.55, cy - r * 0.45)],
                       fill=INK + (255,), width=lw, joint="curve")
            else:
                f = self._f(int(r * 1.6))
                d.text((cx, cy), "?", font=f, fill=INK + (255,), anchor="mm")
            label = s.get("label")
            if label:
                f = self._f(max(10, bw // 6))
                tx, ty = x + 6, y + 5                           # top-left inside the slot (nothing there in game)
                l, t, rr, b = d.textbbox((tx, ty), label, font=f, anchor="lt")
                d.rounded_rectangle((l - 3, t - 2, rr + 3, b + 2), radius=4, fill=(0, 0, 0, 200))
                d.text((tx, ty), label, font=f, fill=(255, 214, 102, 255), anchor="lt")
        a = np.asarray(im, dtype=np.uint16)
        alpha = a[:, :, 3:4]
        bgra = np.empty_like(a)
        bgra[:, :, 0], bgra[:, :, 1], bgra[:, :, 2] = a[:, :, 2], a[:, :, 1], a[:, :, 0]
        bgra[:, :, :3] = bgra[:, :, :3] * alpha // 255                  # premultiplied alpha
        bgra[:, :, 3] = a[:, :, 3]
        return np.ascontiguousarray(bgra.astype(np.uint8))

    # ---- window thread ----
    def _loop(self):
        try:
            self._create()
        except Exception:
            logging.exception("in-game marks: window failed")
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        user32 = ctypes.windll.user32
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _create(self):
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        user32.DefWindowProcW.restype = ctypes.c_ssize_t
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                           wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

        def proc(hwnd, msg, wp, lp):
            if msg == WM_APP:
                self._apply()
                return 0
            return user32.DefWindowProcW(hwnd, msg, wp, lp)
        self._proc = WNDPROC(proc)                                   # keep a reference
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._proc
        wc.hInstance = hinst
        wc.lpszClassName = "RaiderLogbookMarks"
        user32.RegisterClassExW(ctypes.byref(wc))
        ex = WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self._hwnd = user32.CreateWindowExW(ex, "RaiderLogbookMarks", "Raider Logbook marks", WS_POPUP,
                                            0, 0, 1, 1, None, None, hinst, None)
        if not self._hwnd:
            raise OSError("CreateWindowExW failed")
        ok = user32.SetWindowDisplayAffinity(wintypes.HWND(self._hwnd), WDA_EXCLUDEFROMCAPTURE)
        logging.info("in-game marks: window ready, excluded from capture: %s", bool(ok))

    def _apply(self):
        with self._lock:
            p, self._pending = self._pending, None
        user32 = ctypes.windll.user32
        if p is None:
            user32.ShowWindow(wintypes.HWND(self._hwnd), SW_HIDE)
            self._shown = False
            return
        (x, y, w, h), img = p
        gdi32 = ctypes.windll.gdi32
        user32.GetDC.restype = wintypes.HDC
        gdi32.CreateCompatibleDC.restype = wintypes.HDC
        gdi32.CreateDIBSection.restype = wintypes.HBITMAP
        gdi32.SelectObject.restype = wintypes.HGDIOBJ
        screen = user32.GetDC(None)
        mem = gdi32.CreateCompatibleDC(wintypes.HDC(screen))
        bi = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bmp = gdi32.CreateDIBSection(wintypes.HDC(mem), ctypes.byref(bi), 0, ctypes.byref(bits), None, 0)
        try:
            ctypes.memmove(bits, img.ctypes.data, img.nbytes)
            old = gdi32.SelectObject(wintypes.HDC(mem), wintypes.HGDIOBJ(bmp))
            pt, size, src = wintypes.POINT(x, y), wintypes.SIZE(w, h), wintypes.POINT(0, 0)
            blend = BLENDFUNCTION(0, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(wintypes.HWND(self._hwnd), wintypes.HDC(screen), ctypes.byref(pt),
                                       ctypes.byref(size), wintypes.HDC(mem), ctypes.byref(src), 0,
                                       ctypes.byref(blend), ULW_ALPHA)
            gdi32.SelectObject(wintypes.HDC(mem), wintypes.HGDIOBJ(old))
            if not self._shown:
                user32.ShowWindow(wintypes.HWND(self._hwnd), SW_SHOWNOACTIVATE)
                self._shown = True
        finally:
            gdi32.DeleteObject(wintypes.HGDIOBJ(bmp))
            gdi32.DeleteDC(wintypes.HDC(mem))
            user32.ReleaseDC(None, wintypes.HDC(screen))
