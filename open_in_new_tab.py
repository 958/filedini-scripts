from __future__ import annotations
from typing import TYPE_CHECKING, List, Optional, Tuple
import ctypes
import ctypes.wintypes as wintypes
import os
import sys
import time

# ---------------------------------------------------------------------------
# Type checking block (editor IntelliSense only; not loaded at runtime).
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    try:
        from host_stubs import HostAPI
        host: HostAPI = ...
    except ImportError:
        pass

try:
    from toast_helper import show_toast
except Exception:  # noqa: BLE001
    try:
        sys.path.append(os.path.dirname(os.path.abspath(__file__)))
        from toast_helper import show_toast
    except Exception:  # noqa: BLE001
        show_toast = None

# ===========================================================================
# open_in_new_tab - 選択アイテムを新しいタブで開く
# ---------------------------------------------------------------------------
# 選択中（未選択時はカーソル下）のアイテムごとに:
#   - フォルダ          → そのフォルダを新しいタブで開く
#   - ショートカット(.lnk) → リンク先を解決して、
#         リンク先がフォルダ → そのフォルダを新しいタブで開く
#         リンク先がファイル → 親フォルダを新しいタブで開き、カーソルを合わせる
#   - それ以外のファイル  → スキップ
#
# .lnk の解決は ctypes による COM (IShellLinkW / IPersistFile) 直接呼び出しで、
# 外部パッケージに依存しません。
# ===========================================================================

MAX_OPEN_TABS = 10

# ---------------------------------------------------------------------------
# .lnk resolver (ctypes COM, no external dependency)
# ---------------------------------------------------------------------------

_CLSID_SHELL_LINK = "{00021401-0000-0000-C000-000000000046}"
_IID_ISHELL_LINK_W = "{000214F9-0000-0000-C000-000000000046}"
_IID_IPERSIST_FILE = "{0000010B-0000-0000-C000-000000000046}"

_CLSCTX_INPROC_SERVER = 0x1
_SLGP_UNCPRIORITY = 0x2
_STGM_READ = 0x0
_RPC_E_CHANGED_MODE = -2147417850  # 0x80010106


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


_ole32 = ctypes.WinDLL("ole32")
_ole32.CoInitialize.argtypes = [ctypes.c_void_p]
_ole32.CoInitialize.restype = ctypes.c_long  # 生の HRESULT を判定したいので auto-raise させない
_ole32.CoUninitialize.restype = None
_ole32.CLSIDFromString.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_GUID)]
_ole32.CLSIDFromString.restype = ctypes.HRESULT
_ole32.CoCreateInstance.argtypes = [
    ctypes.POINTER(_GUID), ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p),
]
_ole32.CoCreateInstance.restype = ctypes.HRESULT


def _guid(s: str) -> _GUID:
    guid = _GUID()
    _ole32.CLSIDFromString(s, ctypes.byref(guid))
    return guid


def _com_call(obj: ctypes.c_void_p, index: int, restype, argtypes, *args):
    """COMオブジェクトの vtable から index 番目のメソッドを呼ぶ。"""
    vtable = ctypes.cast(
        obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    func = proto(vtable[index])
    return func(obj, *args)


def _com_release(obj: ctypes.c_void_p) -> None:
    try:
        _com_call(obj, 2, wintypes.ULONG, [])  # IUnknown::Release
    except Exception:  # noqa: BLE001
        pass


def resolve_lnk(lnk_path: str) -> Optional[str]:
    """
    .lnk ファイルのリンク先パスを返す。解決できない場合は None。
    （URLリンクやターゲットを持たない特殊リンクは None になる）
    """
    hr = _ole32.CoInitialize(None)
    need_uninit = hr in (0, 1)  # S_OK / S_FALSE
    if hr < 0 and hr != _RPC_E_CHANGED_MODE:
        return None
    try:
        link = ctypes.c_void_p()
        _ole32.CoCreateInstance(
            ctypes.byref(_guid(_CLSID_SHELL_LINK)), None,
            _CLSCTX_INPROC_SERVER,
            ctypes.byref(_guid(_IID_ISHELL_LINK_W)), ctypes.byref(link),
        )
        try:
            persist = ctypes.c_void_p()
            _com_call(  # IUnknown::QueryInterface
                link, 0, ctypes.HRESULT,
                [ctypes.POINTER(_GUID), ctypes.POINTER(ctypes.c_void_p)],
                ctypes.byref(_guid(_IID_IPERSIST_FILE)), ctypes.byref(persist),
            )
            try:
                _com_call(  # IPersistFile::Load
                    persist, 5, ctypes.HRESULT,
                    [wintypes.LPCWSTR, wintypes.DWORD],
                    lnk_path, _STGM_READ,
                )
                buf = ctypes.create_unicode_buffer(1024)
                hr = _com_call(  # IShellLinkW::GetPath
                    link, 3, ctypes.c_long,
                    [wintypes.LPWSTR, ctypes.c_int,
                     ctypes.c_void_p, wintypes.DWORD],
                    buf, len(buf), None, _SLGP_UNCPRIORITY,
                )
                if hr == 0 and buf.value:
                    return buf.value
                return None
            finally:
                _com_release(persist)
        finally:
            _com_release(link)
    except Exception:  # noqa: BLE001
        return None
    finally:
        if need_uninit:
            _ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# Main command
# ---------------------------------------------------------------------------

def _get_target_paths(host: "HostAPI") -> List[str]:
    """選択アイテムのパス一覧。未選択時はカーソル下アイテムを返す。"""
    paths = list(host.state.get_selected_paths() or [])
    if not paths:
        cursor = host.state.get_cursor_path()
        if cursor:
            paths = [cursor]
    return paths


def _resolve_destination(
    host: "HostAPI", path: str
) -> Optional[Tuple[str, Optional[str]]]:
    """
    開き先を (フォルダパス, カーソルを合わせるファイルパス or None) で返す。
    対象外・解決不能は None。
    """
    if os.path.isdir(path):
        return (path, None)
    if path.lower().endswith(".lnk"):
        target = resolve_lnk(path)
        if not target:
            host.log(f"open_in_new_tab: cannot resolve {path}",
                     host.LogLevel.WARNING)
            return None
        if os.path.isdir(target):
            return (target, None)
        if os.path.isfile(target):
            return (os.path.dirname(target), target)
        host.log(f"open_in_new_tab: target missing {target}",
                 host.LogLevel.WARNING)
        return None
    return None


def _notify(host: "HostAPI", message: str) -> None:
    if show_toast is not None:
        try:
            show_toast(message)
            return
        except Exception:  # noqa: BLE001
            pass
    host.log(f"open_in_new_tab: {message}")


# add_tab(path) は同期的にナビゲートしない（2026-09 実測）。新タブは「その時点の
# アクティブタブのフォルダ」で生成され、path への移動は非同期に後追いで走る。しかも
# その移動は実行時点でアクティブなタブに着地するため、待たずに次の add_tab を呼ぶと
# 移動が同じタブに積み上がり、履歴が [1件目, 2件目] になったタブと、移動されないまま
# 元のフォルダに残ったタブができる。そこで1件ずつ着地を確認して直列化する。
# 同じロジックを clipboard_tools.py / explorer_bridge.py にも複写している。
TAB_SETTLE_TIMEOUT_SEC = 3.0
TAB_SETTLE_POLL_SEC = 0.02


def _same_path(left, right) -> bool:
    """Windows のパスとして同一か（大文字小文字・区切り・末尾の区切りの揺れを無視）。"""
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _tab_folder(fw, tab_id: str):
    """タブが現在表示しているフォルダ。取得できなければ None。"""
    try:
        pane = fw.get_pane(tab_id, 0)
    except Exception:  # noqa: BLE001
        return None
    return getattr(pane, "folder_path", None) if pane else None


def _wait_for_tab(fw, tab_id: str, path: str,
                  timeout_sec: float = TAB_SETTLE_TIMEOUT_SEC) -> bool:
    """
    タブが path に到達するまで待つ。到達したら True、時間切れなら False。
    所要時間はネットワークパスかどうかで大きく変わるため、固定の sleep ではなく
    実際の表示フォルダを見て待つ。
    """
    deadline = time.monotonic() + timeout_sec
    while True:
        if _same_path(_tab_folder(fw, tab_id), path):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(TAB_SETTLE_POLL_SEC)


def open_in_new_tab(host: "HostAPI") -> None:
    """
    選択中のフォルダ / ショートカット(.lnk) を新しいタブで開く。
    """
    fw = host.folder_window

    destinations: List[Tuple[str, Optional[str]]] = []
    for path in _get_target_paths(host):
        dest = _resolve_destination(host, path)
        if dest is not None:
            destinations.append(dest)

    if not destinations:
        _notify(host, "対象がありません（フォルダまたは .lnk を選択）")
        return

    skipped = max(0, len(destinations) - MAX_OPEN_TABS)
    first_tab_id = None
    opened = 0

    for folder, cursor_target in destinations[:MAX_OPEN_TABS]:
        tab_id = fw.add_tab(folder)
        if not tab_id:
            host.log(f"open_in_new_tab: add_tab failed for {folder}",
                     host.LogLevel.WARNING)
            continue
        opened += 1
        if first_tab_id is None:
            first_tab_id = tab_id

        # 次の add_tab を呼ぶ前にこのタブの移動が終わったことを確認する。待たないと
        # 後続の移動がこのタブへ着地し、さらに set_cursor が読み込み前のフォルダに
        # 当たって失敗する（TAB_SETTLE_* の注記参照）。
        if not _wait_for_tab(fw, tab_id, folder):
            host.log(f"open_in_new_tab: tab did not settle on {folder}",
                     host.LogLevel.WARNING)

        if cursor_target:
            # カーソル合わせは active tab でないと効かない可能性があるため
            # 先にアクティブ化してから set_cursor する。
            fw.activate_tab(tab_id)
            if not fw.set_cursor(cursor_target, tab_id):
                host.log(f"open_in_new_tab: set_cursor failed for {cursor_target}",
                         host.LogLevel.WARNING)

    if first_tab_id is not None:
        fw.activate_tab(first_tab_id)

    if opened == 0:
        host.ui.ok_dialog("Open In New Tab",
                          "タブを開けませんでした。ログを確認してください。")
        return

    message = f"{opened}件のタブを開きました"
    if skipped > 0:
        message += f"（上限超過 {skipped} 件はスキップ）"
    _notify(host, message)
    host.log(f"open_in_new_tab: opened {opened} tab(s), skipped {skipped}")


if __name__ == "__main__":
    # ホスト外での .lnk 解決テスト: python open_in_new_tab.py <lnkファイル...>
    for arg in sys.argv[1:]:
        print(f"{arg}\n  -> {resolve_lnk(arg)}")
