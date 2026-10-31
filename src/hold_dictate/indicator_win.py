"""The pill's window on Windows: a layered tool window, through ctypes so it needs nothing installed.

WS_EX_NOACTIVATE keeps it from taking focus, WS_EX_TOOLWINDOW keeps it out of the taskbar and Alt+Tab, and
WS_EX_TRANSPARENT with WS_EX_LAYERED lets clicks through. Corners are hard-edged: GDI has no anti-aliasing and
per-pixel alpha would be a lot more code for a window this small.

Off by default (`desktop_indicator`) until someone has tried it on real Windows.
"""
import ctypes
import os
import threading
from ctypes import wintypes

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x8, 0x20, 0x80
WS_EX_LAYERED, WS_EX_NOACTIVATE = 0x80000, 0x8000000
WM_DESTROY, WM_PAINT, WM_ERASEBKGND, WM_TIMER = 0x2, 0xF, 0x14, 0x113
WM_NCHITTEST, WM_MOUSEACTIVATE, WM_APP = 0x84, 0x21, 0x8000
HTTRANSPARENT, MA_NOACTIVATE = -1, 3
LWA_COLORKEY, LWA_ALPHA = 0x1, 0x2
SWP_NOACTIVATE, SWP_SHOWWINDOW = 0x10, 0x40
SW_HIDE = 0
HWND_TOPMOST = -1
SRCCOPY = 0xCC0020
TRANSPARENT_BK = 1
FW_HEAVY = 900
MONITOR_DEFAULTTONEAREST = 2
SYSTEM_DPI = 96
DPI_PER_MONITOR_V2 = -4

FRAME_MS = 33
EDGE = 16  # from the edge of the screen's usable area
KEY = 0x00FF00FF  # this colour in the window shows as nothing


class WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HANDLE), ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HANDLE), ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL), ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT), ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]


# 64-bit handles get cut to 32 bits without these
user32.DefWindowProcW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = (wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID)
user32.CreateWindowExW.restype = wintypes.HWND
user32.RegisterClassW.argtypes = (ctypes.POINTER(WNDCLASSW),)
user32.RegisterClassW.restype = wintypes.ATOM
user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT)
user32.SetLayeredWindowAttributes.argtypes = (wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD)
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.SetTimer.argtypes = (wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID)
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = (wintypes.HWND, ctypes.c_size_t)
user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
user32.InvalidateRect.argtypes = (wintypes.HWND, wintypes.LPVOID, wintypes.BOOL)
user32.BeginPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.BeginPaint.restype = wintypes.HDC
user32.EndPaint.argtypes = (wintypes.HWND, ctypes.POINTER(PAINTSTRUCT))
user32.FillRect.argtypes = (wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HANDLE)
user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
user32.MonitorFromPoint.argtypes = (wintypes.POINT, wintypes.DWORD)
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = (wintypes.HANDLE, ctypes.POINTER(MONITORINFO))
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT)
user32.DispatchMessageW.argtypes = (ctypes.POINTER(wintypes.MSG),)
user32.DispatchMessageW.restype = LRESULT
kernel32.GetModuleHandleW.argtypes = (wintypes.LPCWSTR,)
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
gdi32.CreateCompatibleDC.argtypes = (wintypes.HDC,)
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleBitmap.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int)
gdi32.CreateCompatibleBitmap.restype = wintypes.HANDLE
gdi32.CreateSolidBrush.argtypes = (wintypes.COLORREF,)
gdi32.CreateSolidBrush.restype = wintypes.HANDLE
gdi32.CreatePen.argtypes = (ctypes.c_int, ctypes.c_int, wintypes.COLORREF)
gdi32.CreatePen.restype = wintypes.HANDLE
gdi32.SelectObject.argtypes = (wintypes.HDC, wintypes.HANDLE)
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = (wintypes.HANDLE,)
gdi32.DeleteDC.argtypes = (wintypes.HDC,)
gdi32.RoundRect.argtypes = (wintypes.HDC, *[ctypes.c_int] * 6)
gdi32.Ellipse.argtypes = (wintypes.HDC, *[ctypes.c_int] * 4)
gdi32.BitBlt.argtypes = (wintypes.HDC, *[ctypes.c_int] * 4, wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD)
gdi32.SetBkMode.argtypes = (wintypes.HDC, ctypes.c_int)
gdi32.SetTextColor.argtypes = (wintypes.HDC, wintypes.COLORREF)
gdi32.TextOutW.argtypes = (wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.LPCWSTR, ctypes.c_int)
gdi32.CreateFontW.argtypes = (*[ctypes.c_int] * 5, *[wintypes.DWORD] * 8, wintypes.LPCWSTR)
gdi32.CreateFontW.restype = wintypes.HANDLE


def colorref(rgb: tuple) -> int:
    r, g, b = (int(c * 255) for c in rgb)
    return r | g << 8 | b << 16


def mixed(a: tuple, b: tuple, amount: float) -> tuple:
    return tuple(x + (y - x) * amount for x, y in zip(a, b))


class PillWindow:
    def __init__(self, pill):
        self.pill = pill
        self.timer = False
        self.dpi_scale = 1.0
        self.width = self.height = 1
        self.proc = WNDPROC(self.on_message)  # kept: Windows calls it for as long as the window lives
        instance = kernel32.GetModuleHandleW(None)
        user32.RegisterClassW(ctypes.byref(WNDCLASSW(lpfnWndProc=self.proc, hInstance=instance, lpszClassName="hold-dictate-pill")))
        style = WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_LAYERED | WS_EX_TRANSPARENT
        self.hwnd = user32.CreateWindowExW(style, "hold-dictate-pill", "hold-dictate", WS_POPUP, 0, 0, 1, 1, None, None, instance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())

    def place(self):
        """On the monitor the mouse is on, checked each time: displays come and go."""
        pill = self.pill
        point, info = wintypes.POINT(), MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        user32.GetCursorPos(ctypes.byref(point))
        user32.GetMonitorInfoW(user32.MonitorFromPoint(point, MONITOR_DEFAULTTONEAREST), ctypes.byref(info))
        work = info.rcWork
        get_dpi = getattr(user32, "GetDpiForWindow", None)
        self.dpi_scale = (get_dpi(self.hwnd) or SYSTEM_DPI) / SYSTEM_DPI if get_dpi else 1.0
        s = pill.scale * self.dpi_scale
        self.width = int((pill.WIDTH + 2 * (pill.MARK + pill.MARK_GAP)) * s)
        self.height = int(pill.HEIGHT * s)
        edge = int(EDGE * self.dpi_scale)
        y = work.top + edge if pill.top else work.bottom - self.height - edge
        x = (work.left + work.right - self.width) // 2
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, x, y, self.width, self.height, SWP_NOACTIVATE | SWP_SHOWWINDOW)

    def fade(self):
        user32.SetLayeredWindowAttributes(self.hwnd, KEY, max(1, int(self.pill.shown * 255)), LWA_COLORKEY | LWA_ALPHA)

    def sync(self):
        self.pill.step(0)
        if self.pill.wants_frames() and not self.timer:
            self.fade()
            self.place()  # SWP_NOACTIVATE: shown without taking focus
            self.timer = bool(user32.SetTimer(self.hwnd, 1, FRAME_MS, None))

    def frame(self):
        self.pill.step(FRAME_MS / 1000)
        if self.pill.wants_frames():
            self.fade()
            user32.InvalidateRect(self.hwnd, None, False)
        else:  # no timer at all between recordings, so the helper sleeps
            user32.KillTimer(self.hwnd, 1)
            self.timer = False
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def paint(self, dc):
        pill = self.pill
        s = pill.scale * self.dpi_scale
        whole = wintypes.RECT(0, 0, self.width, self.height)
        key = gdi32.CreateSolidBrush(KEY)
        user32.FillRect(dc, ctypes.byref(whole), key)
        gdi32.DeleteObject(key)

        pen, brush = gdi32.CreatePen(0, 1, colorref(pill.BORDER)), gdi32.CreateSolidBrush(colorref(pill.bg))
        old_pen, old_brush = gdi32.SelectObject(dc, pen), gdi32.SelectObject(dc, brush)
        left, pill_w, pill_h = (self.width - int(pill.WIDTH * s)) // 2, int(pill.WIDTH * s), int(pill.HEIGHT * s)
        gdi32.RoundRect(dc, left, 0, left + pill_w, pill_h, pill_h, pill_h)
        gdi32.SelectObject(dc, old_pen)
        gdi32.SelectObject(dc, old_brush)
        gdi32.DeleteObject(pen)
        gdi32.DeleteObject(brush)

        fx, fy = left + (pill.WIDTH - pill.FIELD_W) / 2 * s, (pill.HEIGHT - pill.FIELD_H) / 2 * s
        color = pill.bar_color()
        brushes = {}  # dots share one colour; lines fade with their height
        rect = wintypes.RECT()
        for x, y, w, h, alpha in pill.shapes():
            shade = round(alpha, 1)
            if shade not in brushes:
                brushes[shade] = gdi32.CreateSolidBrush(colorref(mixed(pill.bg, color, shade)))
            rect.left, rect.top = int(fx + x * s), int(fy + y * s)
            rect.right, rect.bottom = rect.left + max(1, round(w * s)), rect.top + max(1, round(h * s))
            user32.FillRect(dc, ctypes.byref(rect), brushes[shade])
        for used in brushes.values():
            gdi32.DeleteObject(used)

        if pill.mark > 0.01:
            size = int(pill.MARK * s)
            x, y = left + int((pill.WIDTH + pill.MARK_GAP) * s), (self.height - size) // 2
            red = gdi32.CreateSolidBrush(colorref(pill.PROBLEM))
            pen = gdi32.CreatePen(0, 1, colorref(pill.PROBLEM))
            old_pen, old_brush = gdi32.SelectObject(dc, pen), gdi32.SelectObject(dc, red)
            gdi32.Ellipse(dc, x, y, x + size, y + size)
            gdi32.SelectObject(dc, old_pen)
            gdi32.SelectObject(dc, old_brush)
            gdi32.DeleteObject(pen)
            gdi32.DeleteObject(red)
            # GDI draws no crossed-out icons without image files: a "!" here, and the notification says which
            height = int(28 * s)
            font = gdi32.CreateFontW(-height, 0, 0, 0, FW_HEAVY, 0, 0, 0, 0, 0, 0, 0, 0, "Segoe UI")
            old_font = gdi32.SelectObject(dc, font)
            gdi32.SetBkMode(dc, TRANSPARENT_BK)
            gdi32.SetTextColor(dc, 0xFFFFFF)
            gdi32.TextOutW(dc, x + size // 2 - height // 7, y + (size - height) // 2 - height // 6, "!", 1)
            gdi32.SelectObject(dc, old_font)
            gdi32.DeleteObject(font)

    def on_message(self, hwnd, message, wparam, lparam):
        if message == WM_APP:
            self.sync()
        elif message == WM_TIMER:
            self.frame()
        elif message == WM_NCHITTEST:
            return HTTRANSPARENT
        elif message == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        elif message == WM_ERASEBKGND:
            return 1
        elif message == WM_PAINT:
            ps = PAINTSTRUCT()
            target = user32.BeginPaint(hwnd, ctypes.byref(ps))
            # drawn off screen first, or the window flickers at every frame
            dc = gdi32.CreateCompatibleDC(target)
            bitmap = gdi32.CreateCompatibleBitmap(target, self.width, self.height)
            old = gdi32.SelectObject(dc, bitmap)
            self.paint(dc)
            gdi32.BitBlt(target, 0, 0, self.width, self.height, dc, 0, 0, SRCCOPY)
            gdi32.SelectObject(dc, old)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(dc)
            user32.EndPaint(hwnd, ctypes.byref(ps))
            return 0
        elif message == WM_DESTROY:
            os._exit(0)
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)


def run(pill):
    try:
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(DPI_PER_MONITOR_V2))
    except (AttributeError, OSError):  # before Windows 10: scaled by the system, a little soft
        pass
    window = PillWindow(pill)
    wake = lambda: user32.PostMessageW(window.hwnd, WM_APP, 0, 0)
    threading.Thread(target=pill.read_stdin, args=(wake, lambda: os._exit(0)), daemon=True).start()
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.DispatchMessageW(ctypes.byref(msg))
