from __future__ import annotations
import ctypes
import ctypes.wintypes as wintypes
import threading
from typing import Dict, Optional

# ===========================================================================
# toast_helper - lightweight OSD toast (ctypes / Win32, no dependency)
# ---------------------------------------------------------------------------
# 画面下部中央に小さな通知を表示し、自動でフェードアウトして消えます。
# フォーカスを奪わず、クリックも透過します（作業の邪魔をしない）。
#
# 使い方:
#   from toast_helper import show_toast
#   show_toast("3件コピーしました")            # 既定 1.5 秒表示
#   show_toast("完了", duration_ms=3000)      # 表示時間指定
#
# 実装メモ:
# - 表示はバックグラウンドスレッドで行い、呼び出しは即座に返ります。
# - 同時表示は常に1件。新しいトーストが来たら古いものを閉じます。
# ===========================================================================

_user32 = ctypes.WinDLL("user32", use_last_error=True)
_gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(
    LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)

# --- constants -------------------------------------------------------------
WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TRANSPARENT = 0x00000020  # クリック透過

SW_SHOWNOACTIVATE = 4
LWA_ALPHA = 0x02
SPI_GETWORKAREA = 48

WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONUP = 0x0202

DT_CENTER = 0x0001
DT_VCENTER = 0x0004
DT_SINGLELINE = 0x0020
DT_NOPREFIX = 0x0800
DT_CALCRECT = 0x0400

TIMER_LIFE = 1
TIMER_FADE = 2

ERROR_CLASS_ALREADY_EXISTS = 1410

_CLASS_NAME = "FilediniScriptToast"
_BG_COLOR = 0x00202020    # COLORREF (0x00BBGGRR): 濃いグレー
_TEXT_COLOR = 0x00F0F0F0  # ほぼ白
_HILITE_COLOR = 0x00D77800  # 選択行の背景（Windows アクセント風の青, RGB 0,120,215）
_HOVER_COLOR = 0x00404040   # ホバー行の背景（明るめグレー）
_ROW_PADDING_Y = 4
_ALPHA_MAX = 235
_FADE_STEP = 25
_FADE_INTERVAL_MS = 30
_PADDING_X = 22
_PADDING_Y = 12
_BOTTOM_MARGIN = 80
_FONT_FACE = "Yu Gothic UI"
_FONT_HEIGHT = -14

# --- struct definitions ----------------------------------------------------


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


# --- 64-bit safe prototypes -------------------------------------------------
_user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]
_user32.RegisterClassW.restype = wintypes.ATOM
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.DefWindowProcW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.DefWindowProcW.restype = LRESULT
_user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
_user32.DestroyWindow.argtypes = [wintypes.HWND]
_user32.PostMessageW.argtypes = [
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
]
_user32.SetTimer.argtypes = [
    wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID,
]
_user32.SetTimer.restype = ctypes.c_size_t
_user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
_user32.SetLayeredWindowAttributes.argtypes = [
    wintypes.HWND, wintypes.COLORREF, ctypes.c_ubyte, wintypes.DWORD,
]
_user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
]
_user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.DispatchMessageW.restype = LRESULT
_user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
_user32.BeginPaint.restype = wintypes.HDC
_user32.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(PAINTSTRUCT)]
_user32.FillRect.argtypes = [
    wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH,
]
_user32.DrawTextW.argtypes = [
    wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int,
    ctypes.POINTER(wintypes.RECT), wintypes.UINT,
]
_user32.GetDC.argtypes = [wintypes.HWND]
_user32.GetDC.restype = wintypes.HDC
_user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
_user32.SystemParametersInfoW.argtypes = [
    wintypes.UINT, wintypes.UINT, wintypes.LPVOID, wintypes.UINT,
]
_user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
_user32.InvalidateRect.argtypes = [
    wintypes.HWND, ctypes.POINTER(wintypes.RECT), wintypes.BOOL,
]
_user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_user32.PostQuitMessage.argtypes = [ctypes.c_int]
_gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
_gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
_gdi32.CreateFontW.restype = wintypes.HFONT
_gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
_gdi32.SelectObject.restype = wintypes.HANDLE
_gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
_gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
_gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE

# --- module state ------------------------------------------------------------

_lock = threading.Lock()
_class_registered = False
_active_hwnd: Optional[int] = None


class _ToastState:
    def __init__(self, message: str, font, brush,
                 lines=None, selected=-1, hilite_brush=None, row_height=0,
                 on_click=None, hover_brush=None, duration_ms=1500):
        self.message = message
        self.font = font
        self.brush = brush
        self.alpha = _ALPHA_MAX
        # リスト表示モード用（lines が None なら単一メッセージ表示）
        self.lines = lines
        self.selected = selected
        self.hilite_brush = hilite_brush
        self.row_height = row_height
        # クリック対応リスト用
        self.on_click = on_click
        self.hover_brush = hover_brush
        self.hover = -1
        self.duration_ms = duration_ms


_instances: Dict[int, _ToastState] = {}


def _create_font():
    return _gdi32.CreateFontW(
        _FONT_HEIGHT, 0, 0, 0, 400, 0, 0, 0,
        1,  # DEFAULT_CHARSET
        0, 0, 5,  # CLEARTYPE_QUALITY
        0, _FONT_FACE,
    )


@WNDPROC
def _wnd_proc(hwnd, msg, wparam, lparam):
    state = _instances.get(int(hwnd) if hwnd else 0)

    if msg == WM_PAINT and state is not None:
        ps = PAINTSTRUCT()
        hdc = _user32.BeginPaint(hwnd, ctypes.byref(ps))
        try:
            rect = wintypes.RECT()
            _user32.GetClientRect(hwnd, ctypes.byref(rect))
            _user32.FillRect(hdc, ctypes.byref(rect), state.brush)
            old_font = _gdi32.SelectObject(hdc, state.font)
            _gdi32.SetBkMode(hdc, 1)  # TRANSPARENT
            _gdi32.SetTextColor(hdc, _TEXT_COLOR)
            if state.lines is None:
                # 単一メッセージ（従来のトースト）
                text_rect = wintypes.RECT(
                    rect.left + _PADDING_X, rect.top + _PADDING_Y,
                    rect.right - _PADDING_X, rect.bottom - _PADDING_Y,
                )
                _user32.DrawTextW(
                    hdc, state.message, -1, ctypes.byref(text_rect),
                    DT_CENTER | DT_NOPREFIX,
                )
            else:
                # リスト表示: 各行を描画し、選択行はハイライト
                y = rect.top + _PADDING_Y
                for i, line in enumerate(state.lines):
                    row_rect = wintypes.RECT(
                        rect.left, y, rect.right, y + state.row_height
                    )
                    if i == state.selected and state.hilite_brush:
                        _user32.FillRect(
                            hdc, ctypes.byref(row_rect), state.hilite_brush
                        )
                    elif i == state.hover and state.hover_brush:
                        _user32.FillRect(
                            hdc, ctypes.byref(row_rect), state.hover_brush
                        )
                    text_rect = wintypes.RECT(
                        rect.left + _PADDING_X, y,
                        rect.right - _PADDING_X, y + state.row_height,
                    )
                    _user32.DrawTextW(
                        hdc, line, -1, ctypes.byref(text_rect),
                        DT_SINGLELINE | DT_VCENTER | DT_NOPREFIX,
                    )
                    y += state.row_height
            _gdi32.SelectObject(hdc, old_font)
        finally:
            _user32.EndPaint(hwnd, ctypes.byref(ps))
        return 0

    if (msg in (WM_MOUSEMOVE, WM_LBUTTONUP) and state is not None
            and state.lines is not None and state.on_click is not None
            and state.row_height > 0):
        y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
        row = (y - _PADDING_Y) // state.row_height
        in_range = 0 <= row < len(state.lines)

        if msg == WM_MOUSEMOVE:
            # ホバー行の更新と、操作中はフェード/消滅を保留する
            if in_range and row != state.hover:
                state.hover = row
                _user32.InvalidateRect(hwnd, None, True)
            _user32.KillTimer(hwnd, TIMER_FADE)
            if state.alpha != _ALPHA_MAX:
                state.alpha = _ALPHA_MAX
                _user32.SetLayeredWindowAttributes(
                    hwnd, 0, _ALPHA_MAX, LWA_ALPHA
                )
            _user32.SetTimer(hwnd, TIMER_LIFE, state.duration_ms, None)
            return 0

        # WM_LBUTTONUP: クリックされた行をコールバックへ
        if in_range:
            callback = state.on_click
            _user32.DestroyWindow(hwnd)
            try:
                callback(row)
            except Exception:  # noqa: BLE001
                pass  # コールバック側の失敗でウィンドウスレッドを壊さない
        return 0

    if msg == WM_TIMER and state is not None:
        if wparam == TIMER_LIFE:
            _user32.KillTimer(hwnd, TIMER_LIFE)
            _user32.SetTimer(hwnd, TIMER_FADE, _FADE_INTERVAL_MS, None)
        elif wparam == TIMER_FADE:
            state.alpha -= _FADE_STEP
            if state.alpha <= 0:
                _user32.DestroyWindow(hwnd)
            else:
                _user32.SetLayeredWindowAttributes(
                    hwnd, 0, state.alpha, LWA_ALPHA
                )
        return 0

    if msg == WM_DESTROY:
        _user32.KillTimer(hwnd, TIMER_LIFE)
        _user32.KillTimer(hwnd, TIMER_FADE)
        _user32.PostQuitMessage(0)
        return 0

    return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)


def _ensure_class() -> None:
    global _class_registered
    if _class_registered:
        return
    wc = WNDCLASSW()
    wc.style = 0
    wc.lpfnWndProc = _wnd_proc
    wc.hInstance = _kernel32.GetModuleHandleW(None)
    wc.lpszClassName = _CLASS_NAME
    if not _user32.RegisterClassW(ctypes.byref(wc)):
        if ctypes.get_last_error() != ERROR_CLASS_ALREADY_EXISTS:
            raise ctypes.WinError(ctypes.get_last_error())
    _class_registered = True


def _measure_text(message: str, font) -> tuple[int, int]:
    hdc = _user32.GetDC(None)
    try:
        old_font = _gdi32.SelectObject(hdc, font)
        rect = wintypes.RECT(0, 0, 0, 0)
        _user32.DrawTextW(
            hdc, message, -1, ctypes.byref(rect),
            DT_CALCRECT | DT_CENTER | DT_NOPREFIX,
        )
        _gdi32.SelectObject(hdc, old_font)
        return rect.right - rect.left, rect.bottom - rect.top
    finally:
        _user32.ReleaseDC(None, hdc)


def _apply_rounded_corners(hwnd) -> None:
    """Windows 11 の角丸を適用する（失敗しても無視）。"""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        DWMWA_WINDOW_CORNER_PREFERENCE = 33
        DWMWCP_ROUND = 2
        pref = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref),
        )
    except Exception:
        pass


def _toast_thread(message: str, duration_ms: int,
                  lines=None, selected=-1, on_click=None) -> None:
    global _active_hwnd

    _ensure_class()
    font = _create_font()
    brush = _gdi32.CreateSolidBrush(_BG_COLOR)
    hilite_brush = None
    hover_brush = None
    row_height = 0

    if lines is None:
        text_w, text_h = _measure_text(message, font)
        width = text_w + _PADDING_X * 2
        height = text_h + _PADDING_Y * 2
    else:
        hilite_brush = _gdi32.CreateSolidBrush(_HILITE_COLOR)
        if on_click is not None:
            hover_brush = _gdi32.CreateSolidBrush(_HOVER_COLOR)
        max_w = 0
        max_h = 0
        for line in lines:
            w, h = _measure_text(line or " ", font)
            max_w = max(max_w, w)
            max_h = max(max_h, h)
        row_height = max_h + _ROW_PADDING_Y * 2
        width = max_w + _PADDING_X * 2
        height = row_height * len(lines) + _PADDING_Y * 2

    work = wintypes.RECT()
    _user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
    x = work.left + (work.right - work.left - width) // 2
    y = work.bottom - height - _BOTTOM_MARGIN

    ex_style = (WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_LAYERED
                | WS_EX_NOACTIVATE)
    if on_click is None:
        # クリック不要のトーストはクリック透過（作業の邪魔をしない）。
        # クリック対応リストではマウスメッセージを受けるため透過しない。
        ex_style |= WS_EX_TRANSPARENT

    hwnd = _user32.CreateWindowExW(
        ex_style,
        _CLASS_NAME, "", WS_POPUP,
        x, y, width, height,
        None, None, _kernel32.GetModuleHandleW(None), None,
    )
    if not hwnd:
        _gdi32.DeleteObject(font)
        _gdi32.DeleteObject(brush)
        if hilite_brush:
            _gdi32.DeleteObject(hilite_brush)
        if hover_brush:
            _gdi32.DeleteObject(hover_brush)
        return

    hwnd_key = int(hwnd)
    state = _ToastState(
        message, font, brush,
        lines=lines, selected=selected,
        hilite_brush=hilite_brush, row_height=row_height,
        on_click=on_click, hover_brush=hover_brush, duration_ms=duration_ms,
    )
    _instances[hwnd_key] = state

    with _lock:
        # 既存のトーストがあれば閉じる（常に1件表示）。
        if _active_hwnd is not None and _active_hwnd != hwnd_key:
            _user32.PostMessageW(_active_hwnd, WM_CLOSE, 0, 0)
        _active_hwnd = hwnd_key

    _user32.SetLayeredWindowAttributes(hwnd, 0, _ALPHA_MAX, LWA_ALPHA)
    _apply_rounded_corners(hwnd)
    _user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
    _user32.SetTimer(hwnd, TIMER_LIFE, duration_ms, None)

    msg = wintypes.MSG()
    while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        _user32.TranslateMessage(ctypes.byref(msg))
        _user32.DispatchMessageW(ctypes.byref(msg))

    with _lock:
        if _active_hwnd == hwnd_key:
            _active_hwnd = None
    _instances.pop(hwnd_key, None)
    _gdi32.DeleteObject(font)
    _gdi32.DeleteObject(brush)
    if hilite_brush:
        _gdi32.DeleteObject(hilite_brush)
    if hover_brush:
        _gdi32.DeleteObject(hover_brush)


def show_toast(message: str, duration_ms: int = 1500) -> threading.Thread:
    """
    OSDトーストを表示する。呼び出しは即座に返る（表示は別スレッド）。
    表示に失敗しても例外は投げない（フィードバックは主処理を壊さない）。
    """
    thread = threading.Thread(
        target=_toast_thread, args=(message, duration_ms), daemon=True
    )
    thread.start()
    return thread


def show_list_toast(
    lines: list[str], selected: int = -1, duration_ms: int = 4000,
    on_click=None,
) -> threading.Thread:
    """
    複数行のリストをOSD表示する。selected 行はハイライトされる。
    連続呼び出しで内容が置き換わるため、インクリメンタル検索の
    候補リスト表示に使える。

    on_click に callable を渡すとリストがクリック可能になる:
    - 行のホバーでグレーハイライト、クリックで on_click(row_index) を呼ぶ
    - マウスがリスト上にある間は自動消滅しない
    - コールバックはトーストのUIスレッドから呼ばれる点に注意
    """
    thread = threading.Thread(
        target=_toast_thread,
        args=("", duration_ms, list(lines), selected, on_click),
        daemon=True,
    )
    thread.start()
    return thread


def dismiss_toast() -> None:
    """表示中のトースト/リストがあれば即座に閉じる。"""
    with _lock:
        if _active_hwnd is not None:
            _user32.PostMessageW(_active_hwnd, WM_CLOSE, 0, 0)
