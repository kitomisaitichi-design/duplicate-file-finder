"""Lightweight duplicate-file finder for Windows.

The application deliberately performs no scan on startup. It restores the last
completed result from a small JSON cache, and scans only after the user clicks
Scan Now. A scan runs in a worker thread so cached results stay usable while a
new scan is in progress.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import queue
import stat
import tempfile
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple


APP_NAME = "Duplicate File Finder"
CACHE_VERSION = 1
HASH_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class ScanProgress:
    files_seen: int
    candidates: int
    hashed: int


def cache_file_path() -> Path:
    """Return a writable per-user cache location, creating its folder."""

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        preferred = Path(local_app_data) / "DuplicateFileFinder" / "scan_cache.json"
    else:
        preferred = Path.home() / ".duplicate_file_finder" / "scan_cache.json"
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        return preferred
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "DuplicateFileFinder" / "scan_cache.json"
        fallback.parent.mkdir(parents=True, exist_ok=True)
        return fallback


def canonical_path(path: str) -> str:
    """Normalize a path for comparisons without requiring it to exist."""

    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def display_size(size: int) -> str:
    value = float(max(0, size))
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{size} B"


def format_timestamp(value: float) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(value))
    except (OverflowError, OSError, ValueError):
        return "unknown"


def hash_file(path: str, cancel: Optional[threading.Event] = None) -> Optional[Tuple[str, int]]:
    """Hash one regular file and return (sha256, final_size), or None on failure."""

    digest = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as handle:
            while True:
                if cancel is not None and cancel.is_set():
                    return None
                chunk = handle.read(HASH_CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                total += len(chunk)
    except (OSError, PermissionError):
        return None
    return digest.hexdigest(), total


def iter_regular_files(
    roots: Sequence[str],
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[ScanProgress], None]] = None,
) -> Iterable[Tuple[str, int]]:
    """Yield regular file paths and sizes, skipping inaccessible entries."""

    seen: Set[str] = set()
    files_seen = 0
    candidates = 0

    def process_filenames(directory: str, filenames: Iterable[str]) -> Iterable[Tuple[str, int]]:
        nonlocal files_seen, candidates
        for filename in filenames:
            if cancel is not None and cancel.is_set():
                return
            path = os.path.abspath(os.path.join(directory, filename))
            key = canonical_path(path)
            if key in seen or os.path.islink(path):
                continue
            seen.add(key)
            files_seen += 1
            try:
                info = os.stat(path)
            except (OSError, PermissionError):
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            candidates += 1
            if progress is not None and (files_seen == 1 or files_seen % 200 == 0):
                progress(ScanProgress(files_seen, candidates, 0))
            yield path, int(info.st_size)

    for root in roots:
        if cancel is not None and cancel.is_set():
            return
        root_path = os.path.abspath(os.path.expanduser(root))
        if not os.path.exists(root_path):
            continue
        if os.path.isfile(root_path) and not os.path.islink(root_path):
            yield from process_filenames(os.path.dirname(root_path), [os.path.basename(root_path)])
            continue
        try:
            walker = os.walk(root_path, topdown=True, followlinks=False, onerror=lambda _error: None)
            for directory, subdirectories, filenames in walker:
                if cancel is not None and cancel.is_set():
                    return
                subdirectories[:] = [
                    name
                    for name in subdirectories
                    if not os.path.islink(os.path.join(directory, name))
                ]
                yield from process_filenames(directory, filenames)
        except (OSError, PermissionError):
            continue


def find_duplicate_groups(
    roots: Sequence[str],
    cancel: Optional[threading.Event] = None,
    progress: Optional[Callable[[ScanProgress], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Find groups with matching size and SHA-256 content."""

    by_size: Dict[int, List[str]] = {}
    files_seen = 0
    candidates = 0
    for path, size in iter_regular_files(roots, cancel, progress):
        if cancel is not None and cancel.is_set():
            return None
        by_size.setdefault(size, []).append(path)
        files_seen += 1
        candidates += 1

    possible = [paths for paths in by_size.values() if len(paths) > 1]
    groups: List[Dict[str, Any]] = []
    hashed = 0
    for paths in possible:
        if cancel is not None and cancel.is_set():
            return None
        by_hash: Dict[str, List[str]] = {}
        for path in paths:
            result = hash_file(path, cancel)
            if result is None:
                if cancel is not None and cancel.is_set():
                    return None
                continue
            digest, final_size = result
            try:
                current_size = os.path.getsize(path)
            except OSError:
                continue
            if final_size != current_size:
                continue
            by_hash.setdefault(digest, []).append(path)
            hashed += 1
            if progress is not None:
                progress(ScanProgress(files_seen, candidates, hashed))
        for digest, matching_paths in by_hash.items():
            if len(matching_paths) < 2:
                continue
            ordered_paths = sorted(matching_paths, key=lambda item: canonical_path(item))
            groups.append(
                {
                    "id": f"{len(matching_paths)}-{digest}",
                    "size": int(os.path.getsize(ordered_paths[0])),
                    "hash": digest,
                    "paths": ordered_paths,
                }
            )

    groups.sort(key=lambda group: (-int(group["size"]), canonical_path(group["paths"][0])))
    return {
        "version": CACHE_VERSION,
        "roots": [os.path.abspath(os.path.expanduser(root)) for root in roots],
        "scanned_at": time.time(),
        "groups": groups,
    }


def _valid_group(group: Any) -> bool:
    if not isinstance(group, dict):
        return False
    if not isinstance(group.get("hash"), str) or len(group["hash"]) != 64:
        return False
    if not isinstance(group.get("size"), int) or group["size"] < 0:
        return False
    paths = group.get("paths")
    return isinstance(paths, list) and len(paths) >= 2 and all(isinstance(path, str) for path in paths)


def load_cache(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    path = path or cache_file_path()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(data, dict) or data.get("version") != CACHE_VERSION:
        return None
    groups = [group for group in data.get("groups", []) if _valid_group(group)]
    roots = data.get("roots", [])
    if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
        roots = []
    return {
        "version": CACHE_VERSION,
        "roots": roots,
        "scanned_at": data.get("scanned_at", 0),
        "groups": groups,
    }


def save_cache(data: Dict[str, Any], path: Optional[Path] = None) -> None:
    path = path or cache_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(data, ensure_ascii=False, indent=2)
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(str(temporary), str(path))


def select_safe_paths(groups: Sequence[Dict[str, Any]], selected: Set[str]) -> List[str]:
    """Return selected paths while preserving one existing file per group."""

    safe: List[str] = []
    for group in groups:
        paths = [str(path) for path in group.get("paths", [])]
        existing = [path for path in paths if os.path.isfile(path) and not os.path.islink(path)]
        if len(existing) < 2:
            continue
        keep = sorted(existing, key=canonical_path)[0]
        for path in existing:
            if path != keep and canonical_path(path) in {canonical_path(item) for item in selected}:
                safe.append(path)
    return safe


def _recycle_bin_delete(paths: Sequence[str]) -> Tuple[bool, str]:
    if not paths:
        return True, ""
    if os.name != "nt":
        return False, "The Windows Recycle Bin is only available on Windows."
    try:
        class SHFILEOPSTRUCTW(ctypes.Structure):
            _fields_ = [
                ("hwnd", ctypes.c_void_p),
                ("wFunc", ctypes.c_uint),
                ("pFrom", ctypes.c_wchar_p),
                ("pTo", ctypes.c_wchar_p),
                ("fFlags", ctypes.c_ushort),
                ("fAnyOperationsAborted", ctypes.c_int),
                ("hNameMappings", ctypes.c_void_p),
                ("lpszProgressTitle", ctypes.c_wchar_p),
            ]

        double_null_terminated = "\0".join(paths) + "\0\0"
        operation = SHFILEOPSTRUCTW()
        operation.wFunc = 3  # FO_DELETE
        operation.pFrom = double_null_terminated
        operation.fFlags = 0x0040 | 0x0010 | 0x0020 | 0x0100  # ALLOWUNDO, NOCONFIRM, SILENT, NOERRORUI
        result = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation))
        if result != 0 or operation.fAnyOperationsAborted:
            return False, f"Windows rejected the recycle operation (code {result})."
        return True, ""
    except (AttributeError, OSError, TypeError) as error:
        return False, str(error)


def delete_paths(paths: Sequence[str], recycle: bool = True) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Delete files, returning (deleted, failures). Direct deletion is test-only."""

    deleted: List[str] = []
    failures: List[Tuple[str, str]] = []
    if recycle:
        success, reason = _recycle_bin_delete(paths)
        if success:
            return list(paths), failures
        return [], [(path, reason) for path in paths]
    for path in paths:
        try:
            os.unlink(path)
            deleted.append(path)
        except OSError as error:
            failures.append((path, str(error)))
    return deleted, failures


class DuplicateFileFinderApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_NAME)
        self.geometry("1180x760")
        self.minsize(820, 560)

        self.cache_path = cache_file_path()
        self.cache: Optional[Dict[str, Any]] = load_cache(self.cache_path)
        self.groups: List[Dict[str, Any]] = list(self.cache.get("groups", [])) if self.cache else []
        self.selected_paths: Set[str] = set()
        self.item_meta: Dict[str, Tuple[str, Any]] = {}
        self.queue: "queue.Queue[Tuple[str, Any]]" = queue.Queue()
        self.scan_thread: Optional[threading.Thread] = None
        self.cancel_event = threading.Event()
        self.sort_choice = tk.StringVar(value="Largest first")
        self.status_text = tk.StringVar()
        self.cache_text = tk.StringVar()

        self._build_ui()
        self._load_roots_from_cache()
        self._render_tree()
        self._set_initial_status()
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._close)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        heading = ttk.Frame(outer)
        heading.pack(fill="x")
        ttk.Label(heading, text=APP_NAME, font=("Segoe UI", 16, "bold")).pack(side="left")
        ttk.Label(heading, textvariable=self.cache_text).pack(side="right")

        roots_frame = ttk.LabelFrame(outer, text="Scan locations", padding=7)
        roots_frame.pack(fill="x", pady=(10, 8))
        root_list_frame = ttk.Frame(roots_frame)
        root_list_frame.pack(side="left", fill="x", expand=True)
        self.root_list = tk.Listbox(root_list_frame, height=3, selectmode=tk.EXTENDED, exportselection=False)
        root_scroll = ttk.Scrollbar(root_list_frame, orient="vertical", command=self.root_list.yview)
        self.root_list.configure(yscrollcommand=root_scroll.set)
        self.root_list.pack(side="left", fill="x", expand=True)
        root_scroll.pack(side="right", fill="y")

        root_buttons = ttk.Frame(roots_frame)
        root_buttons.pack(side="left", padx=(8, 0))
        ttk.Button(root_buttons, text="Add folder…", command=self._add_folder).pack(fill="x")
        ttk.Button(root_buttons, text="All local drives", command=self._add_local_drives).pack(fill="x", pady=3)
        ttk.Button(root_buttons, text="Remove selected", command=self._remove_roots).pack(fill="x")
        ttk.Button(root_buttons, text="Clear", command=self._clear_roots).pack(fill="x", pady=(3, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=(0, 8))
        self.scan_button = ttk.Button(controls, text="Scan Now", command=self._start_scan)
        self.scan_button.pack(side="left")
        self.cancel_button = ttk.Button(controls, text="Cancel scan", command=self._cancel_scan, state="disabled")
        self.cancel_button.pack(side="left", padx=(6, 0))
        ttk.Separator(controls, orient="vertical").pack(side="left", fill="y", padx=12)
        ttk.Button(controls, text="Toggle selected", command=self._toggle_selected).pack(side="left")
        ttk.Button(controls, text="Select safe files", command=self._select_safe).pack(side="left", padx=(6, 0))
        ttk.Button(controls, text="Clear selection", command=self._clear_selection).pack(side="left", padx=(6, 0))
        self.delete_button = ttk.Button(controls, text="Delete selected", command=self._delete_selected)
        self.delete_button.pack(side="left", padx=(6, 0))
        ttk.Label(controls, text="Order:").pack(side="right", padx=(8, 4))
        sort_menu = ttk.Combobox(
            controls,
            textvariable=self.sort_choice,
            values=("Largest first", "Smallest first", "Most copies", "Path"),
            state="readonly",
            width=16,
        )
        sort_menu.pack(side="right")
        sort_menu.bind("<<ComboboxSelected>>", lambda _event: self._render_tree())

        status_frame = ttk.Frame(outer)
        status_frame.pack(fill="x", pady=(0, 5))
        ttk.Label(status_frame, textvariable=self.status_text).pack(side="left")
        ttk.Label(status_frame, text="Double-click a file or group to select it.", foreground="#666666").pack(side="right")

        tree_frame = ttk.Frame(outer)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(
            tree_frame,
            columns=("pick", "kind", "size", "modified", "location"),
            show="tree headings",
            selectmode="extended",
        )
        self.tree.heading("#0", text="Duplicate group")
        self.tree.heading("pick", text="Pick")
        self.tree.heading("kind", text="Type")
        self.tree.heading("size", text="Size")
        self.tree.heading("modified", text="Modified")
        self.tree.heading("location", text="File path")
        self.tree.column("#0", width=270, stretch=False)
        self.tree.column("pick", width=52, anchor="center", stretch=False)
        self.tree.column("kind", width=100, anchor="center", stretch=False)
        self.tree.column("size", width=100, anchor="e", stretch=False)
        self.tree.column("modified", width=130, anchor="center", stretch=False)
        self.tree.column("location", width=500, stretch=True)
        tree_y = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        tree_x = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=tree_y.set, xscrollcommand=tree_x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        tree_y.grid(row=0, column=1, sticky="ns")
        tree_x.grid(row=1, column=0, sticky="ew")
        tree_frame.rowconfigure(0, weight=1)
        tree_frame.columnconfigure(0, weight=1)
        self.tree.bind("<Double-1>", self._double_click_tree)

    def _set_initial_status(self) -> None:
        if self.cache and self.groups:
            scanned = format_timestamp(float(self.cache.get("scanned_at", 0)))
            self.cache_text.set(f"Cached result: {scanned}")
            self.status_text.set(f"{len(self.groups)} duplicate groups loaded. No scan will run until you click Scan Now.")
        elif self.cache:
            self.cache_text.set("Cached result: no duplicates")
            self.status_text.set("The last scan found no duplicate groups. Choose locations and click Scan Now to rescan.")
        else:
            self.cache_text.set("No saved scan")
            self.status_text.set("Choose folders or all local drives, then click Scan Now.")

    def _load_roots_from_cache(self) -> None:
        roots = self.cache.get("roots", []) if self.cache else []
        for root in roots:
            self.root_list.insert(tk.END, root)

    def _roots(self) -> List[str]:
        return [str(self.root_list.get(index)) for index in range(self.root_list.size())]

    def _add_folder(self) -> None:
        selected = filedialog.askdirectory(title="Choose a folder to scan")
        if selected:
            self._append_roots([selected])

    def _add_local_drives(self) -> None:
        drives: List[str] = []
        if os.name == "nt":
            try:
                bitmask = ctypes.windll.kernel32.GetLogicalDrives()
                for index in range(26):
                    if bitmask & (1 << index):
                        drive = f"{chr(65 + index)}:\\"
                        if ctypes.windll.kernel32.GetDriveTypeW(drive) == 3:  # DRIVE_FIXED
                            drives.append(drive)
            except (AttributeError, OSError):
                pass
        if not drives:
            drives = [os.path.abspath(os.sep)]
        self._append_roots(drives)

    def _append_roots(self, roots: Sequence[str]) -> None:
        existing = {canonical_path(root) for root in self._roots()}
        for root in roots:
            if canonical_path(root) not in existing:
                self.root_list.insert(tk.END, os.path.abspath(os.path.expanduser(root)))
                existing.add(canonical_path(root))

    def _remove_roots(self) -> None:
        for index in reversed(self.root_list.curselection()):
            self.root_list.delete(index)

    def _clear_roots(self) -> None:
        self.root_list.delete(0, tk.END)

    def _start_scan(self) -> None:
        if self.scan_thread is not None and self.scan_thread.is_alive():
            return
        roots = self._roots()
        if not roots:
            messagebox.showinfo(APP_NAME, "Choose at least one folder, or use All local drives.", parent=self)
            return
        self.cancel_event.clear()
        self.scan_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.status_text.set("Scanning… cached results remain available and can still be deleted.")
        self.scan_thread = threading.Thread(target=self._scan_worker, args=(roots,), daemon=True)
        self.scan_thread.start()

    def _cancel_scan(self) -> None:
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.cancel_event.set()
            self.status_text.set("Cancelling scan… cached results remain available.")

    def _scan_worker(self, roots: Sequence[str]) -> None:
        def report(progress: ScanProgress) -> None:
            if progress.files_seen == 1 or progress.files_seen % 200 == 0 or progress.hashed % 100 == 0:
                self.queue.put(("progress", progress))

        try:
            result = find_duplicate_groups(roots, self.cancel_event, report)
            if result is None:
                self.queue.put(("cancelled", None))
                return
            try:
                save_cache(result, self.cache_path)
            except OSError as error:
                self.queue.put(("cache_error", str(error)))
            self.queue.put(("result", result))
        except Exception as error:  # keep worker failures visible in the UI
            self.queue.put(("error", f"{type(error).__name__}: {error}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "progress":
                    progress = payload
                    self.status_text.set(
                        f"Scanning… {progress.files_seen:,} files seen; {progress.hashed:,} candidates hashed."
                    )
                elif kind == "cache_error":
                    self.status_text.set(f"Scan finished, but the cache could not be saved: {payload}")
                elif kind == "cancelled":
                    self._finish_scan()
                    self.status_text.set("Scan cancelled. The previous cached result was kept.")
                elif kind == "result":
                    self.cache = payload
                    self.groups = list(payload.get("groups", []))
                    self.selected_paths.clear()
                    self._finish_scan()
                    self._render_tree()
                    self.cache_text.set(f"Cached result: {format_timestamp(float(payload.get('scanned_at', 0)))}")
                    self.status_text.set(f"Scan finished: {len(self.groups)} duplicate groups found.")
                elif kind == "error":
                    self._finish_scan()
                    self.status_text.set("Scan failed; the previous cached result was kept.")
                    messagebox.showerror(APP_NAME, f"The scan failed:\n\n{payload}", parent=self)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _finish_scan(self) -> None:
        self.scan_thread = None
        self.scan_button.configure(state="normal")
        self.cancel_button.configure(state="disabled")

    def _sorted_groups(self) -> List[Dict[str, Any]]:
        groups = list(self.groups)
        choice = self.sort_choice.get()
        if choice == "Smallest first":
            return sorted(groups, key=lambda group: (int(group.get("size", 0)), canonical_path(group["paths"][0])))
        if choice == "Most copies":
            return sorted(groups, key=lambda group: (-len(group.get("paths", [])), -int(group.get("size", 0))))
        if choice == "Path":
            return sorted(groups, key=lambda group: canonical_path(group["paths"][0]))
        return sorted(groups, key=lambda group: (-int(group.get("size", 0)), canonical_path(group["paths"][0])))

    def _group_keep_path(self, group: Dict[str, Any]) -> Optional[str]:
        existing = [
            path
            for path in group.get("paths", [])
            if os.path.isfile(path) and not os.path.islink(path)
        ]
        return sorted(existing, key=canonical_path)[0] if existing else None

    def _render_tree(self) -> None:
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self.item_meta.clear()
        for group_index, group in enumerate(self._sorted_groups()):
            group_item = f"group_{group_index}"
            paths = list(group.get("paths", []))
            size = int(group.get("size", 0))
            self.tree.insert(
                "",
                tk.END,
                iid=group_item,
                text=f"{len(paths)} copies · {display_size(size)} each",
                values=("", "group", display_size(size), "", "Expand to see files"),
                open=True,
            )
            self.item_meta[group_item] = ("group", group)
            keep = self._group_keep_path(group)
            for file_index, path in enumerate(sorted(paths, key=canonical_path)):
                item = f"file_{group_index}_{file_index}"
                picked = canonical_path(path) in {canonical_path(item) for item in self.selected_paths}
                kind = "keep" if path == keep else "file"
                try:
                    modified = format_timestamp(os.path.getmtime(path))
                except OSError:
                    modified = "missing"
                self.tree.insert(
                    group_item,
                    tk.END,
                    iid=item,
                    text="",
                    values=("☑" if picked else "☐", kind, display_size(size), modified, path),
                )
                self.item_meta[item] = ("file", path)

    def _double_click_tree(self, _event: tk.Event) -> None:
        self._toggle_selected()

    def _toggle_selected(self) -> None:
        selected_items = self.tree.selection()
        if not selected_items:
            return
        for item in selected_items:
            kind, value = self.item_meta.get(item, ("", None))
            paths: List[str] = []
            if kind == "file":
                paths = [str(value)]
            elif kind == "group":
                paths = [str(path) for path in value.get("paths", [])]
                keep = self._group_keep_path(value)
                paths = [path for path in paths if path != keep]
            for path in paths:
                key = canonical_path(path)
                if key in {canonical_path(item) for item in self.selected_paths}:
                    self.selected_paths = {item for item in self.selected_paths if canonical_path(item) != key}
                else:
                    self.selected_paths.add(path)
        self._render_tree()
        self.status_text.set(f"{len(self.selected_paths)} file(s) selected; kept files are protected.")

    def _select_safe(self) -> None:
        self.selected_paths.clear()
        for group in self.groups:
            keep = self._group_keep_path(group)
            self.selected_paths.update(path for path in group.get("paths", []) if path != keep)
        self._render_tree()
        self.status_text.set(f"{len(self.selected_paths)} safe duplicate file(s) selected.")

    def _clear_selection(self) -> None:
        self.selected_paths.clear()
        self._render_tree()
        self.status_text.set("Selection cleared.")

    def _delete_selected(self) -> None:
        if not self.selected_paths:
            messagebox.showinfo(APP_NAME, "Select one or more file rows first.", parent=self)
            return
        safe = select_safe_paths(self.groups, self.selected_paths)
        if not safe:
            messagebox.showinfo(APP_NAME, "There are no currently safe files selected. The last copy in each group is protected.", parent=self)
            return

        verified: List[str] = []
        skipped: List[str] = []
        group_by_path: Dict[str, Dict[str, Any]] = {}
        for group in self.groups:
            for path in group.get("paths", []):
                group_by_path[canonical_path(path)] = group
        for path in safe:
            group = group_by_path.get(canonical_path(path))
            if group is None:
                skipped.append(path)
                continue
            try:
                info = os.stat(path)
                if int(info.st_size) != int(group.get("size", -1)):
                    skipped.append(path)
                    continue
            except OSError:
                skipped.append(path)
                continue
            current_hash = hash_file(path)
            if current_hash is None or current_hash[0] != group.get("hash"):
                skipped.append(path)
                continue
            verified.append(path)

        if not verified:
            messagebox.showinfo(APP_NAME, "The selected files changed or disappeared since the last scan; nothing was deleted.", parent=self)
            return
        answer = messagebox.askyesno(
            APP_NAME,
            f"Move {len(verified)} verified duplicate file(s) to the Recycle Bin?\n\nThe final copy in each group will be kept.",
            parent=self,
        )
        if not answer:
            return
        deleted, failures = delete_paths(verified, recycle=(os.name == "nt"))
        deleted_keys = {canonical_path(path) for path in deleted}
        if deleted_keys:
            for group in self.groups:
                group["paths"] = [path for path in group.get("paths", []) if canonical_path(path) not in deleted_keys]
            self.groups = [group for group in self.groups if len(group.get("paths", [])) >= 2]
            self.selected_paths = {path for path in self.selected_paths if canonical_path(path) not in deleted_keys}
            if self.cache is None:
                self.cache = {"version": CACHE_VERSION, "roots": self._roots(), "scanned_at": time.time(), "groups": []}
            self.cache["groups"] = self.groups
            try:
                save_cache(self.cache, self.cache_path)
            except OSError:
                pass
            self._render_tree()
        summary = f"Moved {len(deleted)} file(s) to the Recycle Bin."
        if failures or skipped:
            summary += f" Skipped {len(failures) + len(skipped)} file(s) that could not be verified or moved."
        if self.scan_thread is not None and self.scan_thread.is_alive():
            summary += " The scan is still running."
        self.status_text.set(summary)
        if failures:
            messagebox.showwarning(APP_NAME, summary, parent=self)

    def _close(self) -> None:
        if self.scan_thread is not None and self.scan_thread.is_alive():
            self.cancel_event.set()
        self.destroy()


def main() -> None:
    app = DuplicateFileFinderApp()
    app.mainloop()


if __name__ == "__main__":
    main()
