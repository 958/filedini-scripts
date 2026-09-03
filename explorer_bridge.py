from __future__ import annotations
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
import base64
import ctypes
import os
import subprocess
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
# explorer_bridge - エクスプローラで開いているフォルダを Filedini に取り込む
# ---------------------------------------------------------------------------
# エントリポイント:
#   import_explorer_folders 開いているエクスプローラのフォルダを新タブに取り込み、
#                           取り込めたウィンドウを閉じる
#
# エクスプローラウィンドウの列挙は Shell.Application COM 経由。COM の IDispatch を
# ctypes で直接叩くと実装が長くなるため PowerShell に委譲する。
# ===========================================================================

MAX_IMPORT_TABS = 10

_CREATE_NO_WINDOW = 0x08000000
_PS_TIMEOUT_SEC = 20

_user32 = ctypes.WinDLL("user32")


def _notify(host: "HostAPI", message: str) -> None:
    if show_toast is not None:
        try:
            show_toast(message)
            return
        except Exception:  # noqa: BLE001
            pass
    host.log(f"explorer_bridge: {message}")


# HWND<TAB>パス を1行ずつ出力する。エクスプローラ以外（IE等）の
# Shell ウィンドウは FullName で除外する。
#
# Windows 11 のタブ付きエクスプローラでは、1ウィンドウ（＝1 HWND）の各タブが
# 別々の Shell ウィンドウとして返る。つまり同じ HWND がタブ数ぶん出力される。
_PS_LIST = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$shell = New-Object -ComObject Shell.Application
foreach ($w in @($shell.Windows())) {
    try {
        if ($w.FullName -and $w.FullName.ToLower().EndsWith('explorer.exe')) {
            $path = $w.Document.Folder.Self.Path
            if ($path) { "$($w.HWND)`t$path" }
        }
    } catch { }
}
"""

_WM_CLOSE = 0x0010
_CLOSE_INTERVAL_SEC = 0.15


def _run_powershell(script: str) -> str:
    """PowerShell スクリプトを実行して標準出力を返す。失敗時は例外。"""
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    proc = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive",
         "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
        timeout=_PS_TIMEOUT_SEC,
    )
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise RuntimeError(detail or f"powershell exited with {proc.returncode}")
    return proc.stdout.decode("utf-8", "replace")


def list_explorer_windows() -> List[Tuple[int, str]]:
    """
    開いているエクスプローラの (HWND, フォルダパス) 一覧。
    タブ付きウィンドウは、同じ HWND がタブごとに1件ずつ現れる。
    """
    windows: List[Tuple[int, str]] = []
    for line in _run_powershell(_PS_LIST).splitlines():
        hwnd_text, sep, path = line.partition("\t")
        if not sep or not path.strip():
            continue
        try:
            hwnd = int(hwnd_text.strip())
        except ValueError:
            continue
        windows.append((hwnd, path.strip()))
    return windows


def close_explorer_window(hwnd: int, tab_count: int) -> None:
    """
    エクスプローラウィンドウをタブごと閉じる。

    WM_CLOSE も IWebBrowser2::Quit() も「アクティブタブ1枚」しか閉じないため、
    タブ数ぶん送る（最後の1枚を閉じた時点でウィンドウが消える）。余分に送っても
    宛先が無くなった時点で捨てられるだけだが、閉じ切る前に次を送らないよう間隔を置く。
    """
    for _ in range(max(1, tab_count)):
        _user32.PostMessageW(hwnd, _WM_CLOSE, 0, 0)
        time.sleep(_CLOSE_INTERVAL_SEC)


# add_tab(path) は同期的にナビゲートしない（2026-09 実測）。新タブは「その時点の
# アクティブタブのフォルダ」で生成され、path への移動は非同期に後追いで走る。しかも
# その移動は実行時点でアクティブなタブに着地するため、待たずに次の add_tab を呼ぶと
# 移動が同じタブに積み上がり、履歴が [1件目, 2件目] になったタブと、移動されないまま
# 元のフォルダに残ったタブができる。そこで1件ずつ着地を確認して直列化する。
# 同じロジックを clipboard_tools.py / open_in_new_tab.py にも複写している。
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


def import_explorer_folders(host: "HostAPI") -> None:
    """
    開いているエクスプローラウィンドウのフォルダを新しいタブで取り込み、
    全タブを取り込めたウィンドウを閉じる。

    タブの挿入位置は Filedini 本体の設定（設定 > 振る舞い > 新しいタブの挿入位置。
    実体は config/App.json の NewWorkspaceInsertPosition）に従う。スクリプト側では
    位置を制御しない。
    """
    fw = host.folder_window

    try:
        windows = list_explorer_windows()
    except Exception as exc:  # noqa: BLE001
        host.log(f"explorer_bridge: enumeration failed: {exc}", host.LogLevel.ERROR)
        host.ui.ok_dialog(
            "Import Explorer Folders",
            f"エクスプローラウィンドウの列挙に失敗しました:\n{exc}",
        )
        return

    if not windows:
        _notify(host, "エクスプローラウィンドウが開いていません")
        return

    # ウィンドウ単位で見たいので HWND ごとにまとめる（タブ付きウィンドウ対策）。
    tab_total: Dict[int, int] = {}
    for hwnd, _path in windows:
        tab_total[hwnd] = tab_total.get(hwnd, 0) + 1

    # 「PC」「ごみ箱」等の特殊フォルダは実体パスを持たないためタブにできない。
    targets = [(hwnd, path) for hwnd, path in windows if os.path.isdir(path)]
    special = len(windows) - len(targets)

    if not targets:
        _notify(host, f"取り込めるフォルダがありません（特殊フォルダ {special} 件）")
        return

    skipped = max(0, len(targets) - MAX_IMPORT_TABS)

    imported: Dict[int, int] = {}  # HWND -> 取り込めたタブ数
    first_tab_id: Optional[str] = None

    for hwnd, path in targets[:MAX_IMPORT_TABS]:
        tab_id = fw.add_tab(path)
        if not tab_id:
            host.log(f"explorer_bridge: add_tab failed for {path}",
                     host.LogLevel.WARNING)
            continue

        # 次の add_tab を呼ぶ前にこのタブの移動が終わったことを確認する。待たないと
        # 後続の移動がこのタブへ着地する（TAB_SETTLE_* の注記参照）。取り込み成功を
        # 根拠にエクスプローラ窓を閉じるので、着地を見届けてから数える。
        if not _wait_for_tab(fw, tab_id, path):
            host.log(f"explorer_bridge: tab did not settle on {path}",
                     host.LogLevel.WARNING)

        imported[hwnd] = imported.get(hwnd, 0) + 1
        if first_tab_id is None:
            first_tab_id = tab_id

    if not imported:
        host.ui.ok_dialog(
            "Import Explorer Folders",
            "タブを開けませんでした。ログを確認してください。",
        )
        return

    # 全タブを取り込めたウィンドウだけ閉じる。1枚でも取り込めなかったタブ
    # （特殊フォルダ・上限超過・add_tab 失敗）が残るウィンドウを閉じると、
    # そのタブが失われてしまう。
    closed = 0
    left_open = 0
    for hwnd, count in imported.items():
        if count < tab_total[hwnd]:
            left_open += 1
            continue
        try:
            close_explorer_window(hwnd, count)
            closed += 1
        except Exception as exc:  # noqa: BLE001
            # 取り込み自体は成功しているので警告に留める。
            host.log(f"explorer_bridge: close failed for hwnd={hwnd}: {exc}",
                     host.LogLevel.WARNING)

    if first_tab_id is not None:
        fw.activate_tab(first_tab_id)

    imported_tabs = sum(imported.values())
    message = f"{imported_tabs}件取り込みました"
    extra = []
    if special > 0:
        extra.append(f"特殊フォルダ {special} 件")
    if skipped > 0:
        extra.append(f"上限超過 {skipped} 件")
    if extra:
        message += f"（{' / '.join(extra)}はスキップ）"
    if left_open > 0:
        message += f"／{left_open}窓は未取り込みタブが残るため閉じません"
    _notify(host, message)
    host.log(
        f"explorer_bridge: imported {imported_tabs} tab(s), closed {closed} window(s), "
        f"left_open {left_open}, special {special}, skipped {skipped}"
    )


if __name__ == "__main__":
    # ホスト外での列挙テスト: python explorer_bridge.py
    # 同じ HWND が複数行に出るものは、タブ付きウィンドウ。
    for hwnd, path in list_explorer_windows():
        print(f"{hwnd}\t{path}")
