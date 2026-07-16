from __future__ import annotations
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple
import os
import sys

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
# compare_panes - 左右2ペインのフォルダを突き合わせて差分を選択する
# ---------------------------------------------------------------------------
# エントリポイント:
#   select_only_here   実行ペインにあり、反対ペインに無いものを選択（フォルダも含む）
#   select_newer_here  両ペインにある同名ファイルのうち、実行ペイン側が新しいものを選択
#
# 選択は実行したペインにだけ付ける（コピー操作はアクティブペインにしか効かないため）。
# 比較対象は get_items が返すもの＝画面に見えているアイテムで、フィルタや検索で
# 絞った状態のまま比較できる。
# ===========================================================================

TITLE = "Compare Panes"

# タイムスタンプの許容差（ミリ秒）。FAT/exFAT は2秒粒度で、ネットワーク越しの
# コピーでも微小なズレが出るため、これ以内の差は「同じ」とみなす。
TIMESTAMP_TOLERANCE_MS = 2000


# ---------------------------------------------------------------------------
# Match layer (pure logic, unit-tested)
# ---------------------------------------------------------------------------

def _key(name: str) -> str:
    """比較キー。Windows なので大文字小文字は区別しない。"""
    return (name or "").lower()


def _index_by_name(items) -> Dict[str, object]:
    return {_key(getattr(it, "name", "")): it for it in items}


def pick_only_here(here, there) -> List:
    """here にあって there に無いものを、表示順で返す（フォルダも含む）。"""
    there_keys = set(_index_by_name(there))
    return [it for it in here if _key(getattr(it, "name", "")) not in there_keys]


def pick_newer_here(here, there, tolerance_ms: int = TIMESTAMP_TOLERANCE_MS) -> List:
    """両方にある同名ファイルのうち、here 側が新しいものを表示順で返す。

    フォルダは除く（中身を更新してもフォルダのタイムスタンプは変わらないことがあり、
    判定が当てにならないため）。tolerance_ms 以内の差は同じとみなす。
    """
    there_by_name = _index_by_name(there)
    picked = []
    for it in here:
        if getattr(it, "is_folder", False):
            continue
        other = there_by_name.get(_key(getattr(it, "name", "")))
        if other is None or getattr(other, "is_folder", False):
            continue
        if getattr(it, "timestamp", 0) - getattr(other, "timestamp", 0) > tolerance_ms:
            picked.append(it)
    return picked


# ---------------------------------------------------------------------------
# Host layer
# ---------------------------------------------------------------------------

def _notify(host: "HostAPI", message: str) -> None:
    if show_toast is not None:
        try:
            show_toast(message)
            return
        except Exception:  # noqa: BLE001
            pass
    host.log(f"compare_panes: {message}")


def _load_panes(host: "HostAPI") -> Optional[Tuple[str, int, List, List, bool]]:
    """(tab_id, 実行ペインのindex, こちらのitems, あちらのitems, truncated) を返す。

    2ペインでない等で比較できない場合はダイアログを出して None を返す。
    """
    fw = host.folder_window

    tab = fw.get_active_tab()
    if tab is None:
        host.ui.ok_dialog(TITLE, "アクティブなタブを取得できませんでした。")
        return None
    if tab.pane_state != fw.PaneState.DUAL_PANE:
        host.ui.ok_dialog(TITLE, "2ペインで実行してください。")
        return None

    pane = fw.get_active_pane()
    if pane is None:
        host.ui.ok_dialog(TITLE, "実行中のペインを特定できませんでした。")
        return None

    here_index = pane.pane_index
    there_index = 1 - here_index

    here = fw.get_items(tab.id, here_index)
    there = fw.get_items(tab.id, there_index)
    if here is None or there is None:
        host.ui.ok_dialog(TITLE, "ペインの一覧を取得できませんでした。")
        return None

    truncated = bool(here.truncated) or bool(there.truncated)
    return tab.id, here_index, list(here.items), list(there.items), truncated


def _apply(host: "HostAPI", tab_id: str, pane_index: int, picked: List,
           label: str, truncated: bool) -> None:
    """選択を適用して結果を通知する。"""
    suffix = "／1万件を超えたため一部のみ比較" if truncated else ""

    if not picked:
        # 選択を空にすると既存の選択まで解除してしまうため、何もしない。
        _notify(host, f"差分はありません（{label}）{suffix}")
        host.log(f"compare_panes: no diff ({label}), truncated={truncated}")
        return

    paths = [it.path for it in picked]
    if not host.folder_window.set_selection(paths, tab_id, pane_index):
        host.log("compare_panes: set_selection failed", host.LogLevel.WARNING)
        host.ui.ok_dialog(TITLE, "選択に失敗しました。")
        return

    _notify(host, f"{len(picked)}件 選択（{label}）{suffix}")
    host.log(f"compare_panes: selected {len(picked)} ({label}), truncated={truncated}")


def select_only_here(host: "HostAPI") -> None:
    """実行ペインにあり、反対ペインに無いものを選択する（フォルダも含む）。"""
    loaded = _load_panes(host)
    if loaded is None:
        return
    tab_id, pane_index, here, there, truncated = loaded
    _apply(host, tab_id, pane_index, pick_only_here(here, there),
           "こちらのみ", truncated)


def select_newer_here(host: "HostAPI") -> None:
    """両ペインにある同名ファイルのうち、実行ペイン側が新しいものを選択する。"""
    loaded = _load_panes(host)
    if loaded is None:
        return
    tab_id, pane_index, here, there, truncated = loaded
    _apply(host, tab_id, pane_index, pick_newer_here(here, there),
           "こちらが新しい", truncated)
