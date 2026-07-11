from __future__ import annotations
from typing import TYPE_CHECKING, List, Optional
import ctypes
import io
import json
import os
import re
import sys
import urllib.request
import zipfile

# ---------------------------------------------------------------------------
# Type checking block (editor IntelliSense only; not loaded at runtime).
# ---------------------------------------------------------------------------
if TYPE_CHECKING:
    try:
        from host_stubs import HostAPI
        host: HostAPI = ...
    except ImportError:
        pass

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

try:
    from toast_helper import show_toast, show_list_toast, dismiss_toast
except Exception:  # noqa: BLE001
    show_toast = None
    show_list_toast = None
    dismiss_toast = None

# ===========================================================================
# Migemo Jump / Incremental Search（オールインワン）
# ---------------------------------------------------------------------------
# エントリポイント（Filedini のスクリプト登録に指定する関数）:
#
#   migemo_jump     どこでもジャンプ（統合ピッカー）
#     開いているタブ / ブックマーク / フォルダ履歴 を1つの migemo ピッカーで
#     横断検索します（VSCode の Ctrl+P 的な体験）。
#       [T] 開いているタブ   → 確定でそのタブに切り替え
#       [B] ブックマーク     → 確定で新しいタブで開く
#       [H] フォルダ履歴     → 確定で新しいタブで開く
#     同じパスが複数ソースにある場合は T > B > H の優先で1件に統合します。
#
#   migemo_isearch  フォルダ内インクリメンタル検索
#     ローマ字入力でカーソルが最初の一致へライブ追従します。
#
# migemo エンジン（migemo.dll + 辞書）は初回に migemo_runtime/ へ自動取得。
# 依存: 同一フォルダの toast_helper.py（OSD候補リスト表示。無くても動作は
# しますが候補リストが出ません）
# ===========================================================================

# ---------------------------------------------------------------------------
# Migemo engine constants
# ---------------------------------------------------------------------------
DLL_NAME = "migemo.dll"
DICT_REL = os.path.join("dict", "utf-8", "migemo-dict")

# Runtime files live in a migemo_runtime/ folder next to this script.
_RUNTIME_DIR = os.path.normpath(os.path.join(_SCRIPT_DIR, "migemo_runtime"))

# C/Migemo Windows distribution (migemo.dll + dict/utf-8/migemo-dict).
# The kaoriya "goto" link is a redirect to the current win64 zip; urllib follows it.
DOWNLOAD_URLS = [
    "https://files.kaoriya.net/goto/cmigemo_w64",
]


# ---------------------------------------------------------------------------
# Match layer (pure logic, unit-tested)
# ---------------------------------------------------------------------------
def _build_pattern(engine, text: str) -> Optional["re.Pattern"]:
    """Build a case-insensitive regex from a romaji query via migemo.

    Falls back to an escaped substring match when migemo is unavailable or
    produces an invalid pattern. Returns None for empty input.
    """
    if not text:
        return None
    regex = None
    if engine is not None:
        try:
            regex = engine.query(text)
        except Exception:
            regex = None
    if regex:
        try:
            return re.compile(regex, re.IGNORECASE)
        except re.error:
            pass
    return re.compile(re.escape(text), re.IGNORECASE)


def match_items(items, engine, text: str) -> List:
    """Return items whose .name matches the migemo pattern, in display order."""
    pattern = _build_pattern(engine, text)
    if pattern is None:
        return []
    return [it for it in items if pattern.search(getattr(it, "name", "") or "")]


def match_entries(entries: List, engine, text: str) -> List:
    """migemoパターンで entries（.search_text 持ち）を絞り込む。空クエリは全件。"""
    if not text:
        return list(entries)
    pattern = _build_pattern(engine, text)
    if pattern is None:
        return list(entries)
    return [e for e in entries if pattern.search(e.search_text)]


# ---------------------------------------------------------------------------
# Runtime extraction (pure logic, unit-tested)
# ---------------------------------------------------------------------------
def _find_by_basename(names, basename: str) -> Optional[str]:
    target = basename.lower()
    for n in names:
        if n.replace("\\", "/").split("/")[-1].lower() == target:
            return n
    return None


def _extract_runtime(zip_bytes: bytes, dest_dir: str):
    """Extract migemo.dll and the whole UTF-8 dict dir from a cmigemo zip.

    The entire ``dict/utf-8/`` directory is extracted (not just migemo-dict),
    because migemo_open auto-loads the sibling conversion tables
    (roma2hira.dat, hira2kata.dat, ...) from the dict's directory. Without
    them, romaji-to-hiragana conversion is disabled.

    Returns (dll_path, dict_path). Raises RuntimeError if the dll or the
    migemo-dict are absent.
    """
    os.makedirs(dest_dir, exist_ok=True)
    dll_path = os.path.join(dest_dir, DLL_NAME)
    utf8_dir = os.path.join(dest_dir, "dict", "utf-8")
    dict_path = os.path.join(dest_dir, DICT_REL)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        dll_member = _find_by_basename(names, DLL_NAME)
        utf8_members = [
            n for n in names
            if "/dict/utf-8/" in ("/" + n.replace("\\", "/").lower())
            and not n.replace("\\", "/").endswith("/")
        ]
        has_dict = any(
            n.replace("\\", "/").lower().endswith("utf-8/migemo-dict")
            for n in utf8_members
        )
        if not dll_member or not has_dict:
            raise RuntimeError(
                "zip does not contain expected migemo.dll / utf-8 migemo-dict"
            )
        os.makedirs(utf8_dir, exist_ok=True)
        with zf.open(dll_member) as src, open(dll_path, "wb") as dst:
            dst.write(src.read())
        for member in utf8_members:
            basename = member.replace("\\", "/").split("/")[-1]
            with zf.open(member) as src, open(os.path.join(utf8_dir, basename), "wb") as dst:
                dst.write(src.read())
    return dll_path, dict_path


# ---------------------------------------------------------------------------
# Engine layer (ctypes wrapper + auto setup) — verified on a live host
# ---------------------------------------------------------------------------
class MigemoEngine:
    """Thin ctypes wrapper over C/Migemo (migemo.dll), loaded by absolute path."""

    def __init__(self, dll_path: str, dict_path: str):
        runtime_dir = os.path.dirname(os.path.abspath(dll_path))
        # Ensure dependent DLL resolution works without relying on PATH.
        if hasattr(os, "add_dll_directory"):
            self._dll_dir = os.add_dll_directory(runtime_dir)
        self._lib = ctypes.CDLL(dll_path)
        self._lib.migemo_open.restype = ctypes.c_void_p
        self._lib.migemo_open.argtypes = [ctypes.c_char_p]
        self._lib.migemo_query.restype = ctypes.POINTER(ctypes.c_char)
        self._lib.migemo_query.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        self._lib.migemo_release.restype = None
        self._lib.migemo_release.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._lib.migemo_close.restype = None
        self._lib.migemo_close.argtypes = [ctypes.c_void_p]
        self._handle = self._lib.migemo_open(dict_path.encode("utf-8"))
        if not self._handle:
            raise RuntimeError("migemo_open returned NULL (dictionary load failed)")

    def query(self, text: str) -> str:
        ptr = self._lib.migemo_query(self._handle, text.encode("utf-8"))
        if not ptr:
            return ""
        try:
            value = ctypes.cast(ptr, ctypes.c_char_p).value
            return value.decode("utf-8", "replace") if value else ""
        finally:
            self._lib.migemo_release(self._handle, ctypes.cast(ptr, ctypes.c_void_p))

    def close(self) -> None:
        if getattr(self, "_handle", None):
            self._lib.migemo_close(self._handle)
            self._handle = None


_engine_cache = None


def _download(urls, log) -> bytes:
    last_error = None
    for url in urls:
        try:
            log(f"migemo: downloading {url}")
            with urllib.request.urlopen(url, timeout=60) as resp:
                return resp.read()
        except Exception as e:  # noqa: BLE001 - report and try next mirror
            last_error = e
            log(f"migemo: download failed for {url}: {e}")
    raise RuntimeError(f"all downloads failed: {last_error}")


def ensure_engine(host):
    """Load (and if needed auto-install) the migemo engine. Returns None on failure."""
    global _engine_cache
    if _engine_cache is not None:
        return _engine_cache

    dll_path = os.path.join(_RUNTIME_DIR, DLL_NAME)
    dict_path = os.path.join(_RUNTIME_DIR, DICT_REL)

    if not (os.path.exists(dll_path) and os.path.exists(dict_path)):
        try:
            data = _download(DOWNLOAD_URLS, host.log)
            _extract_runtime(data, _RUNTIME_DIR)
        except Exception as e:  # noqa: BLE001
            host.log(f"migemo setup failed: {e}", host.LogLevel.ERROR)
            host.ui.ok_dialog(
                "Migemo Setup Failed",
                "migemo.dll と辞書を自動取得できませんでした。\n"
                "次のフォルダに手動で配置してください:\n"
                f"{_RUNTIME_DIR}\n"
                f"- {DLL_NAME}\n"
                f"- {DICT_REL}\n\n"
                "入手元: https://www.kaoriya.net/software/cmigemo/",
            )
            return None

    try:
        _engine_cache = MigemoEngine(dll_path, dict_path)
    except Exception as e:  # noqa: BLE001
        host.log(f"migemo load failed: {e}", host.LogLevel.ERROR)
        host.ui.ok_dialog("Migemo Load Failed", f"migemo.dll の読み込みに失敗しました:\n{e}")
        return None
    return _engine_cache


# ===========================================================================
# Entry point 1: migemo_isearch（フォルダ内インクリメンタル検索）
# ===========================================================================
def migemo_isearch(host):
    """Entry point: incremental migemo search over the current folder.

    Type romaji; the pane cursor follows the first match live. Prev/Next cycle
    candidates. Close dismisses the dialog, leaving the cursor on the last match.
    """
    engine = ensure_engine(host)
    if engine is None:
        return  # ensure_engine already reported the failure.

    result = host.folder_window.get_items(limit=0)
    items = list(result.items) if result else []
    items = [it for it in items if it.name != ".." and getattr(it, "name", None) is not None]
    if not items:
        host.ui.ok_dialog("Migemo Search", "現在のフォルダに項目がありません。")
        return

    state = {"matches": [], "index": 0}

    dlg = host.ui.dialog("Migemo Search")
    tb = dlg.text("ローマ字:", "", initial_focus=True)
    buttons = dlg.group(host.ui.LayoutDirection.HORIZONTAL)
    prev_btn = buttons.button("Prev")
    next_btn = buttons.button("Next")
    close_btn = buttons.button("Close")

    def set_nav_enabled(enabled):
        # Runs only while the dialog is shown (from text_changed handler).
        try:
            prev_btn.enabled = enabled
            next_btn.enabled = enabled
        except Exception as e:  # noqa: BLE001
            host.log(f"migemo: enable toggle failed: {e}")

    def goto(idx):
        if not state["matches"]:
            return
        state["index"] = idx % len(state["matches"])
        try:
            host.folder_window.set_cursor(state["matches"][state["index"]].path)
        except Exception as e:  # noqa: BLE001
            host.log(f"migemo: set_cursor failed: {e}")

    def on_changed(sender, value):
        try:
            state["matches"] = match_items(items, engine, value)
            state["index"] = 0
            if state["matches"]:
                goto(0)
                set_nav_enabled(True)
            else:
                set_nav_enabled(False)
        except Exception as e:  # noqa: BLE001
            host.log(f"migemo: search error: {e}", host.LogLevel.ERROR)

    tb.text_changed += on_changed
    prev_btn.clicked += lambda s, e: goto(state["index"] - 1)
    next_btn.clicked += lambda s, e: goto(state["index"] + 1)
    close_btn.clicked += lambda s, e: dlg.close(host.ui.DialogResult.OK)

    dlg.show_modal()


# ===========================================================================
# Migemo Jump: entry model
# ===========================================================================

_LIST_LIMIT = 10       # OSDリストに同時表示する最大行数
_MAX_PATH_LEN = 60     # 候補行に表示するパスの最大長


def _truncate(path: str) -> str:
    if len(path) > _MAX_PATH_LEN:
        return path[:28] + "…" + path[-(_MAX_PATH_LEN - 29):]
    return path


def _tail_name(path: str) -> str:
    # os.path.basename は UNC 共有ルート (\\server\share) で空になるため、
    # 区切りで分割して末尾の要素を取る。
    parts = path.replace("/", "\\").rstrip("\\").split("\\")
    return parts[-1] if parts and parts[-1] else path


def _norm(path: str) -> str:
    return path.replace("/", "\\").rstrip("\\").lower()


class JumpEntry:
    """統合ピッカーの1候補。kind: 'T'（タブ）/ 'B'（ブックマーク）/ 'H'（履歴）"""

    def __init__(self, kind: str, name: str, search_text: str,
                 path: str = "", tab=None):
        self.kind = kind
        self.name = name
        self.search_text = search_text
        self.path = path
        self.tab = tab

    def display(self) -> str:
        if self.path:
            return f"[{self.kind}] {self.name} — {_truncate(self.path)}"
        return f"[{self.kind}] {self.name}"


# ---------------------------------------------------------------------------
# Source collectors (tabs / bookmarks / history)
# ---------------------------------------------------------------------------

def _find_config_file(name: str) -> Optional[str]:
    """Filedini の config フォルダ内ファイルのパスを返す（無ければ None）。"""
    candidates = [
        os.path.join(os.path.dirname(_SCRIPT_DIR), "config", name),
    ]
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(
            os.path.join(local_app_data, "Filedini", "config", name)
        )
    for path in candidates:
        if os.path.isfile(path):
            return path
    return None


def _load_json(host: "HostAPI", name: str) -> Optional[dict]:
    path = _find_config_file(name)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        host.log(f"jump: {name} load failed: {e}", host.LogLevel.WARNING)
        return None


def collect_tab_entries(host: "HostAPI") -> List[JumpEntry]:
    """開いているタブを表示順で JumpEntry にして返す。"""
    entries: List[JumpEntry] = []
    try:
        tabs = host.folder_window.get_tabs() or []
        dual = host.folder_window.PaneState.DUAL_PANE
    except Exception as e:  # noqa: BLE001
        host.log(f"jump: get_tabs failed: {e}", host.LogLevel.WARNING)
        return entries
    for tab in tabs:
        pane_count = 2 if getattr(tab, "pane_state", None) == dual else 1
        paths: List[str] = []
        for pane_index in range(pane_count):
            try:
                pane = host.folder_window.get_pane(tab.id, pane_index)
            except Exception:  # noqa: BLE001
                pane = None
            if pane and getattr(pane, "folder_path", None):
                paths.append(pane.folder_path)
        name = getattr(tab, "name", "") or "(no name)"
        entry = JumpEntry(
            "T", name,
            " ".join([name, *paths]).strip(),
            path=paths[0] if paths else "",
            tab=tab,
        )
        entry.all_paths = paths  # 重複統合用（両ペイン分）
        entries.append(entry)
    return entries


def collect_bookmark_entries(host: "HostAPI") -> List[JumpEntry]:
    """Bookmark.json からパスが設定されているスロットを JumpEntry にして返す。"""
    data = _load_json(host, "Bookmark.json")
    if not data:
        return []
    entries: List[JumpEntry] = []
    for _slot, item in (data.get("Paths") or {}).items():
        path = (item or {}).get("Path") or ""
        if not path.strip():
            continue
        caption = (item.get("Caption") or "").strip()
        name = caption or _tail_name(path)
        entries.append(JumpEntry(
            "B", name, " ".join([caption, path]).strip(), path=path
        ))
    return entries


def collect_history_entries(host: "HostAPI") -> List[JumpEntry]:
    """History.json から最近のフォルダ履歴を（新しい順で）JumpEntry にして返す。"""
    data = _load_json(host, "History.json")
    if not data:
        return []
    folders = data.get("MostRecentDestinationFolders") or []
    return [
        JumpEntry("H", _tail_name(p), p, path=p)
        for p in folders
        if isinstance(p, str) and p.strip()
    ]


def collect_jump_entries(host: "HostAPI") -> List[JumpEntry]:
    """タブ / ブックマーク / 履歴を T > B > H の優先で重複統合して返す。"""
    entries: List[JumpEntry] = []
    seen: set = set()

    for entry in collect_tab_entries(host):
        entries.append(entry)
        for p in getattr(entry, "all_paths", []):
            seen.add(_norm(p))

    for entry in collect_bookmark_entries(host):
        key = _norm(entry.path)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    for entry in collect_history_entries(host):
        key = _norm(entry.path)
        if key in seen:
            continue
        seen.add(key)
        entries.append(entry)

    return entries


# ---------------------------------------------------------------------------
# Picker UI (migemo incremental search + OSD candidate list)
# ---------------------------------------------------------------------------

def _migemo_pick(
    host: "HostAPI",
    engine,
    entries: List,
    on_confirm,
    title: str,
    list_limit: int = _LIST_LIMIT,
) -> None:
    """入力ダイアログとOSD候補リストを表示し、確定時に on_confirm を呼ぶ。

    entries: .search_text (str) と .display() -> str を持つオブジェクトの list
    on_confirm(entry): Enter / Open / 行クリックで確定したときに呼ばれる
    """
    state = {"matches": list(entries), "index": 0, "view_map": [],
             "confirmed": False}

    dlg = host.ui.dialog(title)
    tb = dlg.text("ローマ字:", "", initial_focus=True)
    dlg.label("候補は画面下部のリストに表示されます。Enter または行クリックで決定。")
    buttons = dlg.group(host.ui.LayoutDirection.HORIZONTAL)
    prev_btn = buttons.button("Prev")
    next_btn = buttons.button("Next")
    open_btn = buttons.button("Open", is_primary=True)  # Enter で確定
    cancel_btn = buttons.button("Cancel")

    def confirm(entry):
        state["confirmed"] = True
        try:
            dlg.close(host.ui.DialogResult.OK)
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: dialog close failed: {e}", host.LogLevel.WARNING)
        try:
            on_confirm(entry)
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: confirm failed: {e}", host.LogLevel.ERROR)

    def on_row_click(row):
        # OSDリストのクリック（トースト側スレッドから呼ばれる）
        try:
            view_map = state.get("view_map") or []
            if 0 <= row < len(view_map) and view_map[row] is not None:
                confirm(state["matches"][view_map[row]])
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: row click failed: {e}", host.LogLevel.ERROR)

    def update_status():
        # ホストのダイアログはコントロールのテキストを後から変更できないため、
        # 候補の表示は自前のOSDリスト（toast_helper）で行う。
        matches = state["matches"]
        if show_list_toast is None:
            host.log(f"picker: {len(matches)} match(es)")
            return
        try:
            if not matches:
                state["view_map"] = []
                show_list_toast(["（一致なし）"], -1, duration_ms=8000)
                return
            total = len(matches)
            index = state["index"]
            # 選択行が中央に来るよう表示ウィンドウを切り出す
            start = 0
            if total > list_limit:
                start = min(max(0, index - list_limit // 2),
                            total - list_limit)
            visible = matches[start:start + list_limit]
            lines = [e.display() for e in visible]
            selected = index - start
            # 行番号 -> matches の絶対 index の対応（ヘッダ/フッタ行は None）
            view_map: List[Optional[int]] = [
                start + i for i in range(len(visible))
            ]
            if start > 0:
                lines.insert(0, f"  ↑ 他 {start} 件")
                view_map.insert(0, None)
                selected += 1
            hidden_below = total - (start + len(visible))
            if hidden_below > 0:
                lines.append(f"  ↓ 他 {hidden_below} 件")
                view_map.append(None)
            state["view_map"] = view_map
            show_list_toast(lines, selected, duration_ms=8000,
                            on_click=on_row_click)
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: list toast failed: {e}", host.LogLevel.WARNING)

    def set_nav_enabled(enabled):
        try:
            prev_btn.enabled = enabled
            next_btn.enabled = enabled
            open_btn.enabled = enabled
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: enable toggle failed: {e}")

    def goto(idx):
        if not state["matches"]:
            return
        state["index"] = idx % len(state["matches"])
        update_status()

    def on_changed(sender, value):
        try:
            state["matches"] = match_entries(entries, engine, value)
            state["index"] = 0
            set_nav_enabled(bool(state["matches"]))
            update_status()
        except Exception as e:  # noqa: BLE001
            host.log(f"picker: search error: {e}", host.LogLevel.ERROR)

    def on_open(sender, _):
        if not state["matches"]:
            return
        confirm(state["matches"][state["index"]])

    def on_cancel(sender, _):
        dlg.close(host.ui.DialogResult.CANCEL)

    tb.text_changed += on_changed
    prev_btn.clicked += lambda s, e: goto(state["index"] - 1)
    next_btn.clicked += lambda s, e: goto(state["index"] + 1)
    open_btn.clicked += on_open
    cancel_btn.clicked += on_cancel

    update_status()  # 初期候補リストを表示
    dlg.show_modal()

    # Escape やクローズボタンなど、どの経路で閉じてもリストを片付ける。
    # 確定時は on_confirm 側の完了トーストを消さないため除外する。
    if not state["confirmed"] and dismiss_toast is not None:
        try:
            dismiss_toast()
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# Entry point 2: migemo_jump（どこでもジャンプ）
# ===========================================================================

def migemo_jump(host: "HostAPI") -> None:
    """
    Entry point: タブ / ブックマーク / 履歴の統合 migemo ジャンプ。
    Enter / 行クリックで、タブは切り替え、それ以外は新しいタブで開く。
    """
    engine = ensure_engine(host)
    if engine is None:
        return  # ensure_engine already reported the failure.

    entries = collect_jump_entries(host)
    if not entries:
        host.ui.ok_dialog("Migemo Jump", "ジャンプ先の候補がありません。")
        return

    def on_confirm(entry: JumpEntry) -> None:
        if entry.kind == "T" and entry.tab is not None:
            try:
                ok = host.folder_window.activate_tab(entry.tab.id)
            except Exception as e:  # noqa: BLE001
                host.log(f"jump: activate_tab failed: {e}", host.LogLevel.ERROR)
                ok = False
            if ok:
                if show_toast is not None:
                    try:
                        show_toast(f"タブを切り替えました: {entry.name}")
                    except Exception:  # noqa: BLE001
                        pass
                host.log(f"jump: activated tab {entry.name}")
            else:
                host.ui.ok_dialog(
                    "Migemo Jump",
                    f"タブを切り替えられませんでした: {entry.name}",
                )
            return

        try:
            tab_id = host.folder_window.add_tab(entry.path)
        except Exception as e:  # noqa: BLE001
            host.log(f"jump: add_tab failed: {e}", host.LogLevel.ERROR)
            tab_id = None
        if tab_id:
            try:
                host.folder_window.activate_tab(tab_id)
            except Exception as e:  # noqa: BLE001
                host.log(f"jump: activate_tab failed: {e}")
            if show_toast is not None:
                try:
                    show_toast(f"開きました: {entry.name}")
                except Exception:  # noqa: BLE001
                    pass
            host.log(f"jump: opened [{entry.kind}] {entry.path}")
        else:
            host.ui.ok_dialog(
                "Migemo Jump",
                f"タブを開けませんでした:\n{entry.path}",
            )

    _migemo_pick(host, engine, entries, on_confirm, title="Migemo Jump")


if __name__ == "__main__":
    # ホスト外での簡易ロジック確認用（UI は起動しない。タブは空になる）。
    class _FakeLog:
        DEBUG = INFO = WARNING = ERROR = 0

    class _FakeFolderWindow:
        class PaneState:
            SINGLE_PANE = 0
            DUAL_PANE = 1

        @staticmethod
        def get_tabs():
            return []

    class _FakeHost:
        LogLevel = _FakeLog
        folder_window = _FakeFolderWindow()

        def log(self, msg, level=None):
            print("LOG:", msg)

        class ui:  # noqa: N801
            @staticmethod
            def ok_dialog(title, message):
                print(f"DIALOG [{title}] {message}")

    fake = _FakeHost()
    engine = ensure_engine(fake)
    print("engine:", "OK" if engine else "unavailable")
    jump_entries = collect_jump_entries(fake)
    print(f"{len(jump_entries)} entries")
    for entry in jump_entries[:5]:
        print(" ", entry.display())
    if engine:
        for q in ["kanri", "iel"]:
            r = match_entries(jump_entries, engine, q)
            print(f"{q!r}: {len(r)} ->", [e.name for e in r][:4])
