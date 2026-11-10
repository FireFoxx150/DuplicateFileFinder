import base64
import io
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from PIL import Image

import actions
import analysis
import schedule
import settings
import theme
import trash
import watcher
from actions import (KEEP_RULES, append_history, export, hardlink_copies, keeper, load_results, move_copies,
                     open_path, plan_trash, raw_pair_members, read_history, run_action, save_results,
                     select_by_rule, show_in_folder, trash_copies, undo_moves)
from drives import list_drives
from engine import IMAGE_EXTS, SKIP_PRESET, Options, compare_folders, find_duplicates
from i18n import _
from previews import exif_summary, image_info, preview

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except ImportError:  # drag-and-drop is optional
    DND_FILES, TkinterDnD = None, None

COMPARE = _("Compare two folders")
MODE_KEYS = {_("Exact copies"): "exact", _("Similar images"): "images", _("Similar documents"): "documents",
             _("Similar videos"): "videos", _("Similar audio"): "audio", COMPARE: "exact"}
MODES = tuple(MODE_KEYS)
THRESHOLD_LABEL = {"images": _("Max distance"), "videos": _("Min similarity %"),
                   "documents": _("Min similarity %"), "audio": _("Min similarity %")}
DEFAULT_THRESHOLD = {"images": "16", "videos": "90", "documents": "70", "audio": "75"}
RECENT_MAX = 8
THUMB_PX = 24
_STATE = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
_CACHE = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
_CONFIG = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
UNDO_PATH = os.path.join(_STATE, "dupefinder", "undo.json")
TRASH_LOG = os.path.join(_STATE, "dupefinder", "trashed.json")
HISTORY_PATH = os.path.join(_STATE, "dupefinder", "history.jsonl")
REPORT_PATH = os.path.join(_STATE, "dupefinder", "reports", "latest.html")
IGNORE_PATH = os.path.join(_CONFIG, "dupefinder", "ignored.json")
CACHE_PATH = os.path.join(_CACHE, "dupefinder", "hashes-v2.db")
WATCH_SECONDS = 600
SHORTCUTS = [
    _("Ctrl+Enter or F5 — scan"), _("Escape — cancel scan"), _("Ctrl+P — pause or resume scan"),
    _("Ctrl+F — search results"), _("Ctrl+A — select all files in results"), _("Enter — open selected file"),
    _("Delete — move selected copies to trash"), _("Ctrl+E — export"), _("Ctrl+Z — undo last move"),
]
THEMES = (("system", _("System")), ("light", _("Light")), ("dark", _("Dark")), ("high contrast", _("High contrast")))
PROFILE_KEYS = ("mode", "folder", "folder_b", "min_size_kb", "extensions", "exclude_dirs", "include_hidden",
                "max_distance", "skip_presets", "max_depth", "verify_bytes")


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def drive_label(d):
    return f"{d.name or d.mountpoint}  ·  {human(d.free)} free of {human(d.total)} ({d.fstype})"


def _png_photo(img, size=None):
    img = img.convert("RGBA")
    if size:
        img.thumbnail(size)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return tk.PhotoImage(data=base64.b64encode(buf.getvalue()))


def _cli_path():
    return shutil.which("dupefinder-cli") or os.path.join(os.path.dirname(os.path.abspath(sys.argv[0])), "dupefinder-cli")


def _read_list(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _write_list(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _is_file(iid):
    """Tree rows for files are named by their absolute path; group and month rows are not."""
    return iid.startswith(os.sep)


def _mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


class App:
    def __init__(self, root, start_folder=None):
        self.root = root
        self.root.title(_("Duplicate File Finder"))
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.results = queue.Queue()
        self.cancel_event = self.pause_event = self.watch_event = None
        self.scan_started = 0.0
        self.pending_mode = "exact"
        self.groups, self.sizes, self.unmatched, self.errors, self.name_clashes = [], [], [], [], []
        self.result_mode = "exact"
        self.sort_col, self.sort_reverse = "size", True
        self.thumbs = {}
        self.ignored = actions.load_ignored(IGNORE_PATH)
        self.drive_map = {}

        cfg = settings.load()
        self.cfg = cfg
        self.recent = list(cfg["recent"])
        self.profiles = dict(cfg["profiles"])
        self.mode = tk.StringVar(value=cfg["mode"])
        self.folder = tk.StringVar(value=cfg["folder"] or os.path.expanduser("~"))
        self.folder_b = tk.StringVar(value=cfg["folder_b"])
        self.min_size_kb = tk.StringVar(value=cfg["min_size_kb"])
        self.extensions = tk.StringVar(value=cfg["extensions"])
        self.exclude = tk.StringVar(value=cfg["exclude_dirs"])
        self.include_hidden = tk.BooleanVar(value=cfg["include_hidden"])
        self.skip_presets = tk.BooleanVar(value=cfg["skip_presets"])
        self.verify_bytes = tk.BooleanVar(value=cfg["verify_bytes"])
        self.max_depth = tk.StringVar(value=cfg["max_depth"])
        self.max_size_mb = tk.StringVar()
        self.modified_days = tk.StringVar()
        self.protect_raw = tk.BooleanVar(value=True)
        self.threshold = tk.StringVar(value=cfg["max_distance"])
        self.threshold_text = tk.StringVar()
        self.keep_rule = tk.StringVar(value=cfg["keep_rule"])
        self.keep_folder = tk.StringVar(value=cfg["keep_folder"])
        self.profile = tk.StringVar()
        self.theme_var = tk.StringVar(value=cfg["theme"])
        self.font_size = tk.IntVar(value=cfg["font_size"])
        self.query = tk.StringVar()
        self.group_by = tk.StringVar(value=_("Duplicate group"))
        self.query.trace_add("write", lambda *_a: self._render())
        self.status = tk.StringVar(value=_("Pick a folder and press Scan."))
        self.selection = tk.StringVar(value="")
        self.watch_var = tk.BooleanVar(value=False)

        self._build_menu()
        self._build_top()
        self._build_panes()
        self._build_actions()
        self._bind_keys()
        self._sync_mode()
        self._apply_theme()
        if cfg["geometry"]:
            self.root.geometry(cfg["geometry"])
        if start_folder:
            self.folder.set(start_folder)
            self._remember_folder(start_folder)

    # ----- layout -------------------------------------------------------------

    def _build_menu(self):
        bar = tk.Menu(self.root, tearoff=False)
        f = tk.Menu(bar, tearoff=False)
        f.add_command(label=_("Save results…"), command=self.save_results_dialog)
        f.add_command(label=_("Load results…"), command=self.load_results_dialog)
        f.add_command(label=_("Export…"), command=self.export_dialog, accelerator="Ctrl+E")
        f.add_separator()
        f.add_command(label=_("Quit"), command=self.close)
        bar.add_cascade(label=_("File"), menu=f)
        e = tk.Menu(bar, tearoff=False)
        e.add_command(label=_("Undo last move"), command=self.undo_move, accelerator="Ctrl+Z")
        e.add_command(label=_("Restore last trashed"), command=self.restore_trashed)
        e.add_command(label=_("Set quarantine folder…"), command=self.set_quarantine)
        bar.add_cascade(label=_("Edit"), menu=e)
        t = tk.Menu(bar, tearoff=False)
        t.add_command(label=_("Schedule scans…"), command=self.schedule_dialog)
        t.add_checkbutton(label=_("Watch this folder"), variable=self.watch_var, command=self.toggle_watch)
        t.add_command(label=_("Space by type…"), command=self.space_by_type_window)
        t.add_command(label=_("Disk usage map…"), command=self.disk_map_window)
        t.add_command(label=_("Custom actions…"), command=self.actions_dialog)
        t.add_separator()
        t.add_command(label=_("Remove duplicate bookmarks…"), command=self.bookmarks_dedupe)
        t.add_command(label=_("Organize bookmarks…"), command=self.bookmarks_organize)
        t.add_command(label=_("Command palette"), command=self.command_palette, accelerator="Ctrl+K")
        t.add_command(label=_("History…"), command=self.history_window)
        bar.add_cascade(label=_("Tools"), menu=t)
        v = tk.Menu(bar, tearoff=False)
        th = tk.Menu(v, tearoff=False)
        for key, label in THEMES:
            th.add_radiobutton(label=label, variable=self.theme_var, value=key, command=self._apply_theme)
        v.add_cascade(label=_("Theme"), menu=th)
        v.add_command(label=_("Larger text"), command=lambda: self._font_step(1))
        v.add_command(label=_("Smaller text"), command=lambda: self._font_step(-1))
        bar.add_cascade(label=_("View"), menu=v)
        h = tk.Menu(bar, tearoff=False)
        h.add_command(label=_("Keyboard shortcuts"), command=self.show_shortcuts)
        bar.add_cascade(label=_("Help"), menu=h)
        self.root.config(menu=bar)
        self.menus = [bar, f, e, t, v, th, h]
        self.menu_tree = tk.Menu(self.root, tearoff=False)
        self.menu_actions = tk.Menu(self.root, tearoff=False)

    def _build_top(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")
        ttk.Label(top, text=_("Mode")).grid(row=0, column=0, sticky="w")
        mode_box = ttk.Combobox(top, textvariable=self.mode, values=MODES, state="readonly", width=22)
        mode_box.grid(row=0, column=1, sticky="w")
        mode_box.bind("<<ComboboxSelected>>", lambda e: self._sync_mode())
        ttk.Label(top, text=_("Folder")).grid(row=1, column=0, sticky="w")
        self.folder_box = ttk.Combobox(top, textvariable=self.folder, values=self.recent)
        self.folder_box.grid(row=1, column=1, sticky="ew")
        self._browse_menu(top, self.folder).grid(row=1, column=2)
        self.drive_a = self._drive_box(top, self.folder)
        self.drive_a.grid(row=1, column=3, padx=(4, 0))
        ttk.Label(top, text=_("Folder B")).grid(row=2, column=0, sticky="w")
        self.folder_b_box = ttk.Combobox(top, textvariable=self.folder_b)
        self.folder_b_box.grid(row=2, column=1, sticky="ew")
        self.btn_b = self._browse_menu(top, self.folder_b)
        self.btn_b.grid(row=2, column=2)
        self.drive_b = self._drive_box(top, self.folder_b)
        self.drive_b.grid(row=2, column=3, padx=(4, 0))
        top.columnconfigure(1, weight=1)
        if DND_FILES:
            for box, var in ((self.folder_box, self.folder), (self.folder_b_box, self.folder_b)):
                box.drop_target_register(DND_FILES)
                box.dnd_bind("<<Drop>>", lambda e, v=var: self._on_drop(e, v))

        filt = ttk.Frame(self.root, padding=(6, 0))
        filt.pack(fill="x")
        for label, var, width in ((_("Min size (KB)"), self.min_size_kb, 6),
                                  (_("Only extensions"), self.extensions, 12),
                                  (_("Skip names"), self.exclude, 12),
                                  (_("Max subfolder levels"), self.max_depth, 4),
                                  (_("Max size (MB)"), self.max_size_mb, 6),
                                  (_("Modified within (days)"), self.modified_days, 6)):
            ttk.Label(filt, text=label).pack(side="left", padx=(0, 2))
            ttk.Entry(filt, textvariable=var, width=width).pack(side="left", padx=(0, 8))
        ttk.Label(filt, textvariable=self.threshold_text).pack(side="left", padx=(0, 2))
        self.threshold_entry = ttk.Entry(filt, textvariable=self.threshold, width=5)
        self.threshold_entry.pack(side="left", padx=(0, 8))
        ttk.Checkbutton(filt, text=_("Include hidden"), variable=self.include_hidden).pack(side="left")
        ttk.Checkbutton(filt, text=_("Skip caches and build folders"), variable=self.skip_presets).pack(side="left", padx=6)
        ttk.Checkbutton(filt, text=_("Verify bytes"), variable=self.verify_bytes).pack(side="left")

        prof = ttk.Frame(self.root, padding=(6, 0))
        prof.pack(fill="x")
        ttk.Label(prof, text=_("Profile")).pack(side="left")
        self.profile_box = ttk.Combobox(prof, textvariable=self.profile, values=sorted(self.profiles), width=20)
        self.profile_box.pack(side="left", padx=4)
        self.profile_box.bind("<<ComboboxSelected>>", lambda e: self.load_profile())
        ttk.Button(prof, text=_("Save profile…"), command=self.save_profile).pack(side="left")
        ttk.Button(prof, text=_("Delete profile"), command=self.delete_profile).pack(side="left", padx=4)

        run = ttk.Frame(self.root, padding=6)
        run.pack(fill="x")
        self.scan_btn = ttk.Button(run, text=_("Scan"), command=self.scan)
        self.scan_btn.pack(side="left")
        self.pause_btn = ttk.Button(run, text=_("Pause"), command=self.toggle_pause)
        self.pause_btn.pack(side="left", padx=4)
        self.pause_btn.state(["disabled"])
        self.cancel_btn = ttk.Button(run, text=_("Cancel"), command=self.cancel)
        self.cancel_btn.pack(side="left")
        self.cancel_btn.state(["disabled"])
        self.bar = ttk.Progressbar(run, length=220)
        self.bar.pack(side="left", padx=8)
        ttk.Label(run, textvariable=self.status).pack(side="left")

    def _build_panes(self):
        panes = ttk.PanedWindow(self.root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=6, pady=6)
        left = ttk.Frame(panes)
        search = ttk.Frame(left)
        search.pack(fill="x", pady=(0, 4))
        ttk.Label(search, text=_("Search")).pack(side="left")
        self.search_entry = ttk.Entry(search, textvariable=self.query)
        self.search_entry.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Label(search, text=_("Group by")).pack(side="left")
        group_box = ttk.Combobox(search, textvariable=self.group_by, state="readonly", width=16,
                                 values=(_("Duplicate group"), _("Month taken")))
        group_box.pack(side="left", padx=(4, 0))
        group_box.bind("<<ComboboxSelected>>", lambda e: self._render())
        box = ttk.Frame(left)
        box.pack(fill="both", expand=True)
        scroll = ttk.Scrollbar(box, orient="vertical")
        cols = ("size", "copies", "modified", "taken", "camera")
        self.tree = ttk.Treeview(box, columns=cols, show="tree headings", selectmode="extended")
        self.tree.heading("#0", text=_("Group / file"), command=lambda: self.sort_by("name"))
        self.tree.heading("size", text=_("Size"), command=lambda: self.sort_by("size"))
        self.tree.heading("copies", text=_("Copies"), command=lambda: self.sort_by("copies"))
        self.tree.heading("modified", text=_("Modified"), command=lambda: self.sort_by("modified"))
        self.tree.heading("taken", text=_("Taken"), command=lambda: self.sort_by("taken"))
        self.tree.heading("camera", text=_("Camera"), command=lambda: self.sort_by("camera"))
        self.tree.column("#0", width=300)
        self.tree.column("size", width=80, anchor="e", stretch=False)
        self.tree.column("copies", width=60, anchor="e", stretch=False)
        self.tree.column("modified", width=130, stretch=False)
        self.tree.column("taken", width=130, stretch=False)
        self.tree.column("camera", width=140, stretch=False)
        scroll.configure(command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<Button-3>", self._context_menu)
        panes.add(left, weight=1)
        right = ttk.Frame(panes)
        self.preview = tk.Text(right, wrap="word", width=60, state="disabled")
        self.preview.pack(fill="both", expand=True)
        panes.add(right, weight=1)

    def _build_actions(self):
        acts = ttk.Frame(self.root, padding=6)
        acts.pack(fill="x")
        ttk.Label(acts, text=_("Keep")).pack(side="left")
        ttk.Combobox(acts, textvariable=self.keep_rule, values=KEEP_RULES, state="readonly",
                     width=13).pack(side="left", padx=(2, 2))
        ttk.Entry(acts, textvariable=self.keep_folder, width=14).pack(side="left", padx=(0, 4))
        ttk.Button(acts, text=_("Select extra copies"), command=self.select_extras).pack(side="left")
        ttk.Button(acts, text=_("Move to trash"), command=self.trash_selected).pack(side="left", padx=4)
        ttk.Button(acts, text=_("Quarantine"), command=self.quarantine_selected).pack(side="left")
        ttk.Button(acts, text=_("Move to folder…"), command=self.move_selected).pack(side="left", padx=4)
        ttk.Button(acts, text=_("Hard-link copies"), command=self.hardlink_selected).pack(side="left")
        ttk.Button(acts, text=_("Open file"), command=lambda: self._act(open_path)).pack(side="left", padx=4)
        ttk.Button(acts, text=_("Show in folder"), command=lambda: self._act(show_in_folder)).pack(side="left")
        ttk.Button(acts, text=_("Compare two"), command=self.compare_selected).pack(side="left", padx=4)
        ttk.Button(acts, text=_("Export…"), command=self.export_dialog).pack(side="left")
        ttk.Button(acts, text=_("Preview"), command=self.preview_plan).pack(side="left", padx=4)
        ttk.Button(acts, text=_("Select by rule…"), command=self.select_by_rule_dialog).pack(side="left")
        ttk.Button(acts, text=_("History"), command=self.history_window).pack(side="left", padx=4)
        ttk.Checkbutton(acts, text=_("Protect RAW pairs"), variable=self.protect_raw).pack(side="left")
        ttk.Label(acts, textvariable=self.selection).pack(side="right", padx=(8, 0))
        ttk.Button(acts, text=_("Errors"), command=self.show_errors).pack(side="right")
        ttk.Button(acts, text=_("Name clashes"), command=self.show_clashes).pack(side="right", padx=4)
        ttk.Button(acts, text=_("Only in A"), command=self.show_unmatched).pack(side="right")

    def _bind_keys(self):
        r = self.root
        r.bind("<Control-Return>", lambda e: self.scan())
        r.bind("<F5>", lambda e: self.scan())
        r.bind("<Escape>", lambda e: self.cancel())
        r.bind("<Control-p>", lambda e: self.toggle_pause())
        r.bind("<Control-f>", lambda e: self.search_entry.focus_set())
        r.bind("<Control-e>", lambda e: self.export_dialog())
        r.bind("<Control-z>", lambda e: self.undo_move())
        r.bind("<Control-k>", lambda e: self.command_palette())
        self.tree.bind("<Control-a>", self._select_all_files)
        self.tree.bind("<Return>", lambda e: self._act(open_path))
        self.tree.bind("<Delete>", lambda e: self.trash_selected())

    def _browse_menu(self, parent, var):
        btn = ttk.Menubutton(parent, text=_("Browse…"))
        menu = tk.Menu(btn, tearoff=False, postcommand=lambda: self._fill_browse_menu(menu, var))
        btn.configure(menu=menu)
        return btn

    def _fill_browse_menu(self, menu, var):
        menu.delete(0, "end")
        drives = list_drives()
        for d in drives:
            menu.add_command(label=drive_label(d), command=lambda p=d.mountpoint: var.set(p))
        if drives:
            menu.add_separator()
        menu.add_command(label=_("Choose folder…"), command=lambda: self._browse(var))

    def _drive_box(self, parent, var):
        box = ttk.Combobox(parent, state="readonly", width=40)
        box.configure(postcommand=lambda: self._load_drives(box))
        box.bind("<<ComboboxSelected>>", lambda e: var.set(self.drive_map[box.get()]))
        return box

    def _load_drives(self, box):
        drives = list_drives()
        self.drive_map = {drive_label(d): d.mountpoint for d in drives}
        box.configure(values=list(self.drive_map))

    def _browse(self, var):
        chosen = filedialog.askdirectory(initialdir=var.get() or os.path.expanduser("~"))
        if chosen:
            var.set(chosen)

    def _on_drop(self, event, var):
        paths = self.root.tk.splitlist(event.data)
        if paths and os.path.isdir(paths[0]):
            var.set(paths[0])
        return event.action

    # ----- theme and settings -------------------------------------------------

    def _apply_theme(self):
        p = theme.apply(self.root, self.theme_var.get(), self.font_size.get())
        self.p = p
        self.root.configure(bg=p["bg"])
        for m in self.menus + [self.menu_tree, self.menu_actions]:
            m.configure(bg=p["bg"], fg=p["fg"], activebackground=p["select"], activeforeground=p["fg"])
        self.preview.configure(bg=p["field"], fg=p["fg"], insertbackground=p["fg"])

    def _font_step(self, delta):
        self.font_size.set(max(7, min(24, self.font_size.get() + delta)))
        self._apply_theme()

    def _settings(self):
        return {
            "mode": self.mode.get(), "folder": self.folder.get(), "folder_b": self.folder_b.get(),
            "min_size_kb": self.min_size_kb.get(), "extensions": self.extensions.get(),
            "exclude_dirs": self.exclude.get(), "include_hidden": self.include_hidden.get(),
            "max_distance": self.threshold.get(), "keep_rule": self.keep_rule.get(),
            "theme": self.theme_var.get(), "font_size": self.font_size.get(),
            "geometry": self.root.winfo_geometry(), "recent": self.recent,
            "skip_presets": self.skip_presets.get(), "max_depth": self.max_depth.get(),
            "verify_bytes": self.verify_bytes.get(), "keep_folder": self.keep_folder.get(),
            "quarantine": self.cfg.get("quarantine", ""), "profiles": self.profiles,
            "actions": self.cfg.get("actions", []),
        }

    def _remember_folder(self, path):
        self.recent = ([path] + [p for p in self.recent if p != path])[:RECENT_MAX]
        self.folder_box.configure(values=self.recent)

    def _sync_mode(self):
        mode = self.mode.get()
        compare = mode == COMPARE
        state = ["!disabled"] if compare else ["disabled"]
        self.folder_b_box.state(state)
        self.btn_b.state(state)
        self.drive_b.state(state)
        key = MODE_KEYS[mode]
        self.threshold_text.set(THRESHOLD_LABEL.get(key, ""))
        if key in DEFAULT_THRESHOLD:
            self.threshold_entry.state(["!disabled"])
            if self.threshold.get() in ("5", "16", "28", "70", "80", "75", "90"):
                self.threshold.set(DEFAULT_THRESHOLD[key])
        else:
            self.threshold_entry.state(["disabled"])

    # ----- profiles -----------------------------------------------------------

    def _profile_data(self):
        values = {"mode": self.mode.get(), "folder": self.folder.get(), "folder_b": self.folder_b.get(),
                  "min_size_kb": self.min_size_kb.get(), "extensions": self.extensions.get(),
                  "exclude_dirs": self.exclude.get(), "include_hidden": self.include_hidden.get(),
                  "max_distance": self.threshold.get(), "skip_presets": self.skip_presets.get(),
                  "max_depth": self.max_depth.get(), "verify_bytes": self.verify_bytes.get()}
        return {k: values[k] for k in PROFILE_KEYS}

    def save_profile(self):
        name = simpledialog.askstring(_("Save profile"), _("Name for these scan settings:"), parent=self.root)
        if not name or not name.strip():
            return
        self.profiles[name.strip()] = self._profile_data()
        self.profile_box.configure(values=sorted(self.profiles))
        self.profile.set(name.strip())
        self.status.set(_("Saved profile “{name}”.").format(name=name.strip()))

    def load_profile(self):
        data = self.profiles.get(self.profile.get())
        if not data:
            return
        self.mode.set(data["mode"])
        self.folder.set(data["folder"])
        self.folder_b.set(data["folder_b"])
        self.min_size_kb.set(data["min_size_kb"])
        self.extensions.set(data["extensions"])
        self.exclude.set(data["exclude_dirs"])
        self.include_hidden.set(data["include_hidden"])
        self.threshold.set(data["max_distance"])
        self.skip_presets.set(data["skip_presets"])
        self.max_depth.set(data["max_depth"])
        self.verify_bytes.set(data["verify_bytes"])
        self._sync_mode()

    def delete_profile(self):
        if self.profiles.pop(self.profile.get(), None) is not None:
            self.profile_box.configure(values=sorted(self.profiles))
            self.profile.set("")

    # ----- building options and scanning --------------------------------------

    def _options(self, compare):
        key = MODE_KEYS[self.mode.get()]
        min_size = int(float(self.min_size_kb.get().strip() or "0") * 1024)
        exts = {"." + e.strip().lower().lstrip(".") for e in self.extensions.get().split(",") if e.strip()}
        patterns = {d.strip() for d in self.exclude.get().split(",") if d.strip()}
        if self.skip_presets.get():
            patterns |= set(SKIP_PRESET)
        depth_text = self.max_depth.get().strip()
        max_depth = int(depth_text) if depth_text else None
        if max_depth is not None and max_depth < 0:
            raise ValueError(_("subfolder levels must be 0 or more"))
        size_text = self.max_size_mb.get().strip()
        max_size = int(float(size_text) * 1_000_000) if size_text else None
        days_text = self.modified_days.get().strip()
        modified_after = time.time() - float(days_text) * 86400 if days_text else None
        threshold = float(self.threshold.get().strip() or "0")
        if key == "images" and not 0 <= threshold <= 256:
            raise ValueError(_("max distance must be 0-256"))
        if key in ("documents", "audio", "videos") and not 0 < threshold <= 100:
            raise ValueError(_("similarity must be 1-100"))
        return Options(
            min_size=min_size,
            max_size=max_size,
            modified_after=modified_after,
            include_hidden=self.include_hidden.get(),
            exclude_patterns=frozenset(patterns),
            max_depth=max_depth,
            extensions=frozenset(exts) if exts else None,
            mode="exact" if compare else key,
            threshold=threshold,
            verify_bytes=self.verify_bytes.get(),
        )

    def scan(self):
        if self.cancel_event is not None and not self.cancel_btn.instate(["disabled"]):
            return  # already scanning
        compare = self.mode.get() == COMPARE
        a, b = self.folder.get().strip(), self.folder_b.get().strip()
        if not os.path.isdir(a):
            self.status.set(_("Folder A does not exist."))
            return
        if compare and not os.path.isdir(b):
            self.status.set(_("Folder B does not exist."))
            return
        try:
            opts = self._options(compare)
        except ValueError as e:
            self.status.set(_("Check settings: {error}").format(error=e))
            return
        self._remember_folder(a)
        settings.save(self._settings())
        self.cancel_event, self.pause_event = threading.Event(), threading.Event()
        self.scan_started = time.time()
        self.pending_mode = "exact" if compare else opts.mode
        self.scan_btn.state(["disabled"])
        self.cancel_btn.state(["!disabled"])
        self.pause_btn.state(["!disabled"])
        self.pause_btn.configure(text=_("Pause"))
        self.status.set(_("Scanning…"))
        threading.Thread(target=self._worker, args=(compare, a, b, opts, self.cancel_event, self.pause_event),
                         daemon=True).start()
        self.root.after(100, self._poll)

    def _worker(self, compare, a, b, opts, cancel, pause):
        def progress(label, done, total):
            self.results.put(("progress", label, done, total))

        started = time.time()
        if compare:
            res = compare_folders(a, b, opts, cancel, progress, CACHE_PATH, pause)
        else:
            res = find_duplicates(a, opts, cancel, progress, CACHE_PATH, pause)
        self.results.put(("done", res, time.time() - started))

    def cancel(self):
        if self.cancel_event and not self.cancel_btn.instate(["disabled"]):
            self.cancel_event.set()
            self.pause_event.clear()
            self.status.set(_("Cancelling…"))

    def toggle_pause(self):
        if not self.pause_event or self.pause_btn.instate(["disabled"]):
            return
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.configure(text=_("Pause"))
            self.status.set(_("Resumed."))
        else:
            self.pause_event.set()
            self.pause_btn.configure(text=_("Resume"))
            self.status.set(_("Paused."))

    def _poll(self):
        done = None
        while True:
            try:
                msg = self.results.get_nowait()
            except queue.Empty:
                break
            if msg[0] == "progress":
                _kind, label, n, total = msg
                self.bar.configure(maximum=max(total, 1), value=n)
                if not self.pause_event.is_set():
                    elapsed = time.time() - self.scan_started
                    eta = _(" · about {s}s left").format(s=f"{elapsed / n * (total - n):.0f}") if n else ""
                    self.status.set(f"{label}… {n}/{total}{eta}")
            else:
                done = msg
        if done is None:
            self.root.after(100, self._poll)
            return
        self._finish(done[1], done[2])

    def _finish(self, res, secs):
        self.scan_btn.state(["!disabled"])
        self.cancel_btn.state(["disabled"])
        self.pause_btn.state(["disabled"])
        self.bar.configure(value=0)
        self.result_mode = self.pending_mode
        self.groups = actions.without_ignored(res.groups, self.ignored)
        self.unmatched, self.errors = res.unmatched, res.errors
        self.name_clashes = res.name_clashes
        self.sizes = [os.path.getsize(g[0]) for g in self.groups]
        self._render()
        if res.cancelled:
            self.status.set(_("Scan cancelled."))
            return
        wasted = sum(s * (len(g) - 1) for s, g in zip(self.sizes, self.groups))
        text = _("{n} groups, {w} reclaimable, {e} skipped, {s:.1f}s").format(
            n=len(self.groups), w=human(wasted), e=len(self.errors), s=secs)
        if self.mode.get() == COMPARE:
            text += _(", {n} only in A").format(n=len(self.unmatched))
        self.status.set(text)
        self._notify(text)

    def _notify(self, text):
        if shutil.which("notify-send"):
            subprocess.Popen(["notify-send", _("Duplicate File Finder"), text])

    # ----- watching a folder --------------------------------------------------

    def toggle_watch(self):
        if not self.watch_var.get():
            if self.watch_event:
                self.watch_event.set()
            self.status.set(_("Stopped watching."))
            return
        folder = self.folder.get().strip()
        if not os.path.isdir(folder):
            self.watch_var.set(False)
            self.status.set(_("Folder A does not exist."))
            return
        self.watch_event = threading.Event()
        self.status.set(_("Watching {folder} every {m} minutes.").format(folder=folder, m=WATCH_SECONDS // 60))
        threading.Thread(target=self._watch_loop, args=(folder, self.watch_event), daemon=True).start()

    def _watch_loop(self, folder, stop):
        """Rescan when files change (Linux inotify). Falls back to a timed rescan if inotify is unavailable."""
        changed = threading.Event()
        try:
            threading.Thread(target=watcher.watch, args=(folder, lambda p, m: changed.set(), stop),
                             daemon=True).start()
        except OSError:
            changed.set()
        seen = set()
        while not stop.is_set():
            changed.wait(WATCH_SECONDS)  # wakes on a change, or after the timed fallback
            if stop.wait(2):  # let copies finish before scanning
                break
            changed.clear()
            res = find_duplicates(folder, Options(), cache_path=CACHE_PATH)
            new = [g for g in res.groups if frozenset(g) not in seen]
            seen.update(frozenset(g) for g in res.groups)
            if new:
                self._notify(_("{n} new duplicate group(s) in {folder}").format(n=len(new), folder=folder))

    # ----- command palette ----------------------------------------------------

    def _commands(self):
        return {
            _("Scan"): self.scan, _("Cancel scan"): self.cancel, _("Pause or resume scan"): self.toggle_pause,
            _("Export…"): self.export_dialog, _("Undo last move"): self.undo_move,
            _("Restore last trashed"): self.restore_trashed, _("Move to trash"): self.trash_selected,
            _("Preview"): self.preview_plan, _("Hard-link copies"): self.hardlink_selected,
            _("Select extra copies"): self.select_extras, _("Select by rule…"): self.select_by_rule_dialog,
            _("History"): self.history_window, _("Schedule scans…"): self.schedule_dialog,
            _("Space by type…"): self.space_by_type_window, _("Disk usage map…"): self.disk_map_window,
            _("Custom actions…"): self.actions_dialog, _("Keyboard shortcuts"): self.show_shortcuts,
            _("Larger text"): lambda: self._font_step(1), _("Smaller text"): lambda: self._font_step(-1),
            _("Theme: system"): lambda: self._set_theme("system"), _("Theme: light"): lambda: self._set_theme("light"),
            _("Theme: dark"): lambda: self._set_theme("dark"),
            _("Theme: high contrast"): lambda: self._set_theme("high contrast"),
        }

    def _set_theme(self, name):
        self.theme_var.set(name)
        self._apply_theme()

    def command_palette(self):
        win = tk.Toplevel(self.root)
        win.title(_("Command palette"))
        win.configure(bg=self.p["bg"])
        commands = self._commands()
        query = tk.StringVar()
        entry = ttk.Entry(win, textvariable=query, width=50)
        entry.pack(fill="x", padx=6, pady=6)
        listbox = tk.Listbox(win, width=60, height=14, bg=self.p["field"], fg=self.p["fg"])
        listbox.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        shown = []

        def refresh(*_a):
            shown[:] = filter_commands(list(commands), query.get())
            listbox.delete(0, "end")
            for name in shown:
                listbox.insert("end", name)
            if shown:
                listbox.selection_set(0)

        def run(_event=None):
            if not shown:
                return
            sel = listbox.curselection()
            name = shown[sel[0]] if sel else shown[0]
            win.destroy()
            commands[name]()

        query.trace_add("write", refresh)
        entry.bind("<Return>", run)
        listbox.bind("<Return>", run)
        listbox.bind("<Double-1>", run)
        entry.focus_set()
        refresh()

    # ----- results tree -------------------------------------------------------

    def _sort_key(self, i):
        g = self.groups[i]
        is_image = os.path.splitext(g[0])[1].lower() in IMAGE_EXTS
        if self.sort_col == "name":
            return os.path.basename(g[0]).lower()
        if self.sort_col == "copies":
            return len(g)
        if self.sort_col == "modified":
            return _mtime(g[0])
        if self.sort_col == "taken":
            return exif_summary(g[0])[0] if is_image else ""
        if self.sort_col == "camera":
            return exif_summary(g[0])[1] if is_image else ""
        return self.sizes[i]

    def sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col, self.sort_reverse = col, False
        self._render()

    def _thumb(self, path):
        if path in self.thumbs:
            return self.thumbs[path]
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            return None
        try:
            with Image.open(path) as im:
                photo = _png_photo(im, (THUMB_PX, THUMB_PX))
        except Exception:
            photo = None
        self.thumbs[path] = photo
        return photo

    def _meta(self, path):
        mt = _mtime(path)
        modified = time.strftime("%Y-%m-%d %H:%M", time.localtime(mt)) if mt else ""
        if os.path.splitext(path)[1].lower() in IMAGE_EXTS:
            taken, camera = exif_summary(path)
        else:
            taken, camera = "", ""
        return modified, taken, camera

    def _month_of(self, group):
        taken = exif_summary(group[0])[0] if os.path.splitext(group[0])[1].lower() in IMAGE_EXTS else ""
        return taken[:7].replace(":", "-") if len(taken) >= 7 else _("No date")

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        q = self.query.get().lower()
        idxs = [i for i in range(len(self.groups)) if not q or any(q in p.lower() for p in self.groups[i])]
        idxs.sort(key=self._sort_key, reverse=self.sort_reverse)
        by_month = self.group_by.get() == _("Month taken")
        for i in idxs:
            g = self.groups[i]
            parent = ""
            if by_month:
                month = self._month_of(g)
                if not self.tree.exists(f"m:{month}"):
                    self.tree.insert("", "end", iid=f"m:{month}", text=month, open=True)
                parent = f"m:{month}"
            row = self.tree.insert(parent, "end", iid=f"g{i}", text=os.path.basename(g[0]),
                                   values=(human(self.sizes[i]), len(g), "", "", ""), open=True)
            for p in g:
                modified, taken, camera = self._meta(p)
                self.tree.insert(row, "end", iid=p, text=p, image=self._thumb(p) or "",
                                 values=(human(self.sizes[i]), "", modified, taken, camera))
        self._update_selection()

    def _selected_paths(self):
        return [s for s in self.tree.selection() if _is_file(s)]

    def _all_file_iids(self, parent=""):
        out = []
        for child in self.tree.get_children(parent):
            out += [child] if _is_file(child) else self._all_file_iids(child)
        return out

    def _update_selection(self):
        paths = self._selected_paths()
        total = 0
        for p in paths:
            try:
                total += os.path.getsize(p)
            except OSError:
                pass
        self.selection.set(_("{n} selected · {s}").format(n=len(paths), s=human(total)) if paths else "")

    def _on_select(self, _event):
        self._update_selection()
        paths = self._selected_paths()
        if len(paths) == 1:
            self._fill(self.preview, paths[0])

    def _on_double(self, event):
        row = self.tree.identify_row(event.y)
        if row and _is_file(row):  # file rows only; group rows keep their expand/collapse
            open_path(row)

    def _select_all_files(self, _event):
        self.tree.selection_set(self._all_file_iids())
        return "break"

    def _group_of(self, path):
        for i, g in enumerate(self.groups):
            if path in g:
                return i
        return None

    def _context_menu(self, event):
        row = self.tree.identify_row(event.y)
        if not row or row.startswith("m:"):  # month headings have no menu
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        m = self.menu_tree
        m.delete(0, "end")
        if _is_file(row):
            m.add_command(label=_("Open file"), command=lambda: self._act(open_path))
            m.add_command(label=_("Show in folder"), command=lambda: self._act(show_in_folder))
            m.add_command(label=_("Copy path"), command=self.copy_path)
            m.add_command(label=_("View full size"), command=self.view_full)
            if len(self._selected_paths()) == 2:
                m.add_command(label=_("Compare the two selected"), command=self.compare_selected)
            m.add_separator()
            m.add_command(label=_("Ignore this group"), command=lambda: self.ignore_group(row))
            if self.cfg.get("actions"):
                self._fill_actions_menu()
                m.add_cascade(label=_("Run action"), menu=self.menu_actions)
        else:
            m.add_command(label=_("Select extra copies in this group"), command=lambda: self.select_group_extras(row))
            m.add_command(label=_("Ignore this group"), command=lambda: self.ignore_group(row))
        m.add_separator()
        m.add_command(label=_("Move to trash"), command=self.trash_selected)
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()

    def ignore_group(self, row):
        i = int(row[1:]) if row.startswith("g") else self._group_of(row)
        if i is None:
            return
        self.ignored.add(frozenset(self.groups[i]))
        actions.save_ignored(IGNORE_PATH, self.ignored)
        self.groups.pop(i)
        self.sizes.pop(i)
        self._render()
        self.status.set(_("Group ignored. It will not show again."))

    def select_group_extras(self, row):
        i = int(row[1:])
        k = keeper(self.groups[i], self.keep_rule.get(), self.keep_folder.get().strip())
        self.tree.selection_set([p for p in self.groups[i] if p != k])

    def select_extras(self):
        extras = []
        for g in self.groups:
            k = keeper(g, self.keep_rule.get(), self.keep_folder.get().strip())
            extras += [p for p in g if p != k]
        self.tree.selection_set([p for p in extras if self.tree.exists(p)])

    def _act(self, fn):
        paths = self._selected_paths()
        if not paths:
            self.status.set(_("Select a file first."))
            return
        fn(paths[0])

    def copy_path(self):
        paths = self._selected_paths()
        if paths:
            self.root.clipboard_clear()
            self.root.clipboard_append(paths[0])
            self.status.set(_("Path copied."))

    # ----- preview ------------------------------------------------------------

    def _fill(self, widget, path):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        try:
            st = os.stat(path)
            header = f"{path}\n{human(st.st_size)} · " + _("modified {t}").format(t=time.ctime(st.st_mtime)) + "\n"
        except OSError as e:
            header = f"{path}\n{e}\n"
        if os.path.splitext(path)[1].lower() in IMAGE_EXTS:
            try:
                header += image_info(path) + "\n"
            except Exception:
                pass
        widget.insert("end", header + "\n")
        kind, payload = preview(path)
        if kind == "image":
            photo = tk.PhotoImage(data=base64.b64encode(payload))
            widget.image_create("end", image=photo)
            widget._photo = photo  # Tk drops images that have no Python reference
        else:
            widget.insert("end", payload)
        widget.configure(state="disabled")

    def view_full(self):
        paths = self._selected_paths()
        if not paths:
            return
        path = paths[0]
        if os.path.splitext(path)[1].lower() not in IMAGE_EXTS:
            self.status.set(_("Full-size view is for images."))
            return
        try:
            with Image.open(path) as im:
                base = _png_photo(im)
        except Exception as e:
            self.status.set(_("Cannot show image: {error}").format(error=e))
            return
        win = tk.Toplevel(self.root)
        win.title(os.path.basename(path))
        win.configure(bg=self.p["bg"])
        label = tk.Label(win, bg=self.p["bg"])
        label.pack(fill="both", expand=True)
        bar = ttk.Frame(win, padding=4)
        bar.pack(fill="x")
        scale = {"n": 0}

        def show():
            n = scale["n"]
            img = base.zoom(2 ** n) if n > 0 else base.subsample(2 ** -n) if n < 0 else base
            label.configure(image=img)
            label.image = img

        def step(delta):
            scale["n"] = max(-3, min(2, scale["n"] + delta))
            show()

        ttk.Button(bar, text=_("Zoom in"), command=lambda: step(1)).pack(side="left")
        ttk.Button(bar, text=_("Zoom out"), command=lambda: step(-1)).pack(side="left", padx=4)
        ttk.Button(bar, text=_("Actual size"), command=lambda: (scale.update(n=0), show())).pack(side="left")
        show()

    def compare_selected(self):
        paths = self._selected_paths()
        if len(paths) != 2:
            self.status.set(_("Select exactly two files to compare."))
            return
        win = tk.Toplevel(self.root)
        win.title(_("Compare"))
        panes = ttk.PanedWindow(win, orient="horizontal")
        panes.pack(fill="both", expand=True)
        for path in paths:
            text = tk.Text(panes, wrap="word", width=60, bg=self.p["field"], fg=self.p["fg"],
                           insertbackground=self.p["fg"], state="disabled")
            panes.add(text, weight=1)
            self._fill(text, path)

    # ----- actions on copies --------------------------------------------------

    def _forget(self, paths):
        gone = set(paths)
        pairs = [(s, [p for p in g if p not in gone]) for s, g in zip(self.sizes, self.groups)]
        pairs = [(s, g) for s, g in pairs if len(g) > 1]
        self.sizes, self.groups = [s for s, _g in pairs], [g for _s, g in pairs]
        self.unmatched = [p for p in self.unmatched if p not in gone]
        clashes = [[p for p in c if p not in gone] for c in self.name_clashes]
        self.name_clashes = [c for c in clashes if len(c) > 1]
        self._render()

    def _unprotected(self, paths):
        """Copies to act on: RAW files whose JPEG twin sits beside them are left alone when protected."""
        if not self.protect_raw.get():
            return paths
        guarded = raw_pair_members(paths)
        if guarded:
            self.status.set(_("Left {n} RAW file(s) alone: their JPEG pair is in the same folder.").format(
                n=len(guarded)))
        return [p for p in paths if p not in guarded]

    def preview_plan(self):
        """Show what trash, move or hard-link would act on, without changing anything."""
        chosen = self._unprotected(self._selected_paths())
        if not chosen:
            self.status.set(_("Select copies first."))
            return
        would, kept = plan_trash(self.groups, set(chosen))
        lines = [_("Would act on {n} copy(ies):").format(n=len(would))] + [f"  {p}" for p in sorted(would)]
        lines += [_("Kept, to protect each group:")] + [f"  {p}" for p in sorted(kept)]
        lines.append(_("Nothing has been changed."))
        self._listing(_("Preview"), lines)

    def select_by_rule_dialog(self):
        win = tk.Toplevel(self.root)
        win.title(_("Select by rule"))
        win.configure(bg=self.p["bg"])
        kind, value = tk.StringVar(value=_("older than days")), tk.StringVar(value="90")
        ttk.Label(win, text=_("Select copies (not the one kept) that are:")).grid(row=0, column=0, columnspan=2, padx=6, pady=4)
        ttk.Combobox(win, textvariable=kind, state="readonly", width=22,
                     values=(_("older than days"), _("larger than MB"))).grid(row=1, column=0, padx=6)
        ttk.Entry(win, textvariable=value, width=8).grid(row=1, column=1, padx=6)

        def apply():
            try:
                number = float(value.get())
            except ValueError:
                self.status.set(_("Enter a number."))
                return
            rule = "older than days" if kind.get() == _("older than days") else "larger than MB"
            chosen = select_by_rule(self.groups, self.keep_rule.get(), self.keep_folder.get().strip(), rule, number)
            self.tree.selection_set([p for p in chosen if self.tree.exists(p)])
            self.status.set(_("Selected {n} copy(ies) by rule.").format(n=len(chosen)))
            win.destroy()

        ttk.Button(win, text=_("Select"), command=apply).grid(row=2, column=0, columnspan=2, pady=6)

    def history_window(self):
        entries = read_history(HISTORY_PATH)
        lines = [f"{e.get('time', '')}  {e.get('action', '')}  {len(e.get('paths', []))} file(s)  {e.get('detail', '')}"
                 for e in reversed(entries)]
        self._listing(_("History"), lines)

    def trash_selected(self):
        chosen = self._unprotected(self._selected_paths())
        if not chosen:
            self.status.set(_("Select copies to move to trash first."))
            return
        if not messagebox.askyesno(_("Move to trash"),
                                   _("Move {n} file(s) to the trash?").format(n=len(chosen)) + "\n" +
                                   _("At least one copy in each group is always kept.")):
            return
        trashed, kept, failed = trash_copies(self.groups, set(chosen))
        _write_list(TRASH_LOG, trashed)
        append_history(HISTORY_PATH, "trash", trashed)
        self._forget(trashed)
        self.status.set(_("Moved {n} file(s) to trash; kept {k} to protect groups.").format(
            n=len(trashed), k=len(kept)))
        if failed:
            messagebox.showwarning(_("Some files could not be trashed"),
                                   "\n".join(f"{p}: {msg}" for p, msg in failed))

    def restore_trashed(self):
        paths = _read_list(TRASH_LOG)
        if not paths:
            self.status.set(_("Nothing to restore."))
            return
        mounts = [d.mountpoint for d in list_drives()]
        restored, missing = 0, []
        for p in paths:
            try:
                if trash.restore(p, mounts):
                    restored += 1
                    continue
            except (OSError, FileExistsError):
                pass
            missing.append(p)
        _write_list(TRASH_LOG, [])
        self.status.set(_("Restored {n} file(s) from the trash. Rescan to see them in groups.").format(n=restored))
        if missing:
            messagebox.showwarning(_("Some files could not be restored"), "\n".join(missing))

    def quarantine_selected(self):
        dest = self.cfg.get("quarantine", "")
        if not dest:
            self.status.set(_("Set a quarantine folder first (Edit menu)."))
            return
        self._move_to(dest)

    def set_quarantine(self):
        chosen = filedialog.askdirectory(title=_("Quarantine folder"))
        if chosen:
            self.cfg["quarantine"] = chosen
            settings.save(self._settings())
            self.status.set(_("Quarantine folder set to {f}.").format(f=chosen))

    def move_selected(self):
        dest = filedialog.askdirectory(title=_("Move copies into"))
        if dest:
            self._move_to(dest)

    def _move_to(self, dest):
        chosen = self._unprotected(self._selected_paths())
        if not chosen:
            self.status.set(_("Select copies to move first."))
            return
        moved, kept, failed = move_copies(self.groups, set(chosen), dest, UNDO_PATH)
        append_history(HISTORY_PATH, "move", moved, detail=dest)
        self._forget(moved)
        self.status.set(_("Moved {n} file(s) to {d}. Ctrl+Z undoes this move.").format(n=len(moved), d=dest))
        if failed:
            messagebox.showwarning(_("Some files could not be moved"),
                                   "\n".join(f"{p}: {msg}" for p, msg in failed))

    def undo_move(self):
        restored, failed = undo_moves(UNDO_PATH)
        self.status.set(_("Put back {n} file(s). Rescan to see them in groups.").format(n=len(restored))
                        if restored else _("Nothing to undo."))
        if failed:
            messagebox.showwarning(_("Some files could not be put back"),
                                   "\n".join(f"{p}: {msg}" for p, msg in failed))

    def hardlink_selected(self):
        if self.result_mode != "exact":
            self.status.set(_("Hard links are only offered for exact copies."))
            return
        chosen = self._selected_paths()
        if not chosen:
            self.status.set(_("Select copies to replace with hard links first."))
            return
        if not messagebox.askyesno(_("Hard-link copies"),
                                   _("Replace {n} file(s) with hard links to a kept copy?").format(n=len(chosen)) + "\n" +
                                   _("Their disk space is freed. Each file keeps its name and path.")):
            return
        linked, failed = hardlink_copies(self.groups, set(chosen))
        append_history(HISTORY_PATH, "hard link", linked)
        self.status.set(_("Replaced {n} file(s) with hard links.").format(n=len(linked)))
        if failed:
            messagebox.showwarning(_("Some files could not be linked"),
                                   "\n".join(f"{p}: {msg}" for p, msg in failed))

    def export_dialog(self):
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv"), ("JSON", "*.json"), ("HTML report", "*.html"),
                                                       ("Excel", "*.xlsx")])
        if path:
            export(self.groups, path)
            self.status.set(_("Exported {n} groups to {p}.").format(n=len(self.groups), p=path))

    def save_results_dialog(self):
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("Results", "*.json")])
        if path:
            save_results(path, self.groups, self.result_mode)
            self.status.set(_("Saved results to {p}.").format(p=path))

    def load_results_dialog(self):
        path = filedialog.askopenfilename(filetypes=[("Results", "*.json")])
        if not path:
            return
        groups, missing, mode = load_results(path)
        self.groups, self.result_mode = groups, mode
        self.sizes = [os.path.getsize(g[0]) for g in groups]
        self.unmatched, self.errors, self.name_clashes = [], [], []
        self._render()
        self.status.set(_("Loaded {n} groups; {m} files were no longer there.").format(n=len(groups), m=missing))

    # ----- tools --------------------------------------------------------------

    def schedule_dialog(self):
        win = tk.Toplevel(self.root)
        win.title(_("Schedule scans"))
        win.configure(bg=self.p["bg"])
        folder, hours, msg = tk.StringVar(value=self.folder.get()), tk.StringVar(value="24"), tk.StringVar()
        for row, (label, var) in enumerate(((_("Folder"), folder), (_("Every (hours)"), hours))):
            ttk.Label(win, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=4)
            ttk.Entry(win, textvariable=var, width=50).grid(row=row, column=1, padx=6, pady=4)
        ttk.Label(win, textvariable=msg, wraplength=420).grid(row=2, column=0, columnspan=2, padx=6)

        def install():
            try:
                h = max(1, int(hours.get()))
            except ValueError:
                msg.set(_("Hours must be a whole number."))
                return
            os.makedirs(os.path.dirname(REPORT_PATH), exist_ok=True)
            schedule.write_units(folder.get(), h, _cli_path(), REPORT_PATH)
            for cmd in (["daemon-reload"], ["enable", "--now", schedule.TIMER]):
                subprocess.run(["systemctl", "--user", *cmd], capture_output=True)
            msg.set(_("Scheduled. Reports are written to {r}.").format(r=REPORT_PATH))

        def remove():
            subprocess.run(["systemctl", "--user", "disable", "--now", schedule.TIMER], capture_output=True)
            for name in (schedule.SERVICE, schedule.TIMER):
                try:
                    os.remove(os.path.join(schedule.unit_dir(), name))
                except OSError:
                    pass
            msg.set(_("Schedule removed."))

        bar = ttk.Frame(win, padding=6)
        bar.grid(row=3, column=0, columnspan=2, sticky="e")
        ttk.Button(bar, text=_("Install schedule"), command=install).pack(side="left")
        ttk.Button(bar, text=_("Remove schedule"), command=remove).pack(side="left", padx=4)

    def space_by_type_window(self):
        if not self.groups:
            self.status.set(_("Scan first."))
            return
        rows = [f"{ext:<12} {human(n)}" for ext, n in analysis.space_by_type(self.groups)]
        self._listing(_("Reclaimable space by type"), rows)

    def disk_map_window(self):
        if not self.groups:
            self.status.set(_("Scan first."))
            return
        win = tk.Toplevel(self.root)
        win.title(_("Disk usage map"))
        win.configure(bg=self.p["bg"])
        top = ttk.Frame(win, padding=4)
        top.pack(fill="x")
        where = tk.StringVar()
        base = {"path": self.folder.get().strip()}
        ttk.Label(top, textvariable=where).pack(side="left")
        ttk.Button(top, text=_("Up"), command=lambda: go(os.path.dirname(base["path"]))).pack(side="right")
        w, h = 800, 500
        canvas = tk.Canvas(win, width=w, height=h, bg=self.p["field"], highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        palette = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948", "#b07aa1", "#9c755f"]
        rects = []

        def draw():
            canvas.delete("all")
            rects.clear()
            where.set(base["path"])
            items = analysis.reclaimable_by_folder(self.groups, base["path"])
            if not items:
                canvas.create_text(20, 20, anchor="nw", fill=self.p["fg"], text=_("Nothing to map here."))
                return
            values = dict(items)
            for i, (label, _v, x, y, rw, rh) in enumerate(analysis.treemap(items, 0, 0, w, h)):
                canvas.create_rectangle(x, y, x + rw, y + rh, fill=palette[i % len(palette)], outline=self.p["bg"])
                if rw > 60 and rh > 30:
                    canvas.create_text(x + 6, y + 6, anchor="nw", fill="white",
                                       text=f"{label}\n{human(values[label])}")
                rects.append((x, y, x + rw, y + rh, label))

        def go(path):
            if path and os.path.isdir(path):
                base["path"] = path
                draw()

        def click(event):
            for x0, y0, x1, y1, label in rects:
                if x0 <= event.x <= x1 and y0 <= event.y <= y1 and label not in (
                        _("(this folder)"), _("(top level)")):
                    go(os.path.join(base["path"], label))  # drill into the clicked folder
                    return

        canvas.bind("<Button-1>", click)
        draw()

    def _bookmark_task(self, title, dry_run, apply_run, question):
        """Show the plan, ask for confirmation, then make the change and show what was done."""
        self._listing(title, dry_run())
        if not messagebox.askyesno(title, question):
            return
        self._listing(title, apply_run())

    def bookmarks_dedupe(self):
        import bookmarks
        self._bookmark_task(
            _("Remove duplicate bookmarks"),
            lambda: bookmarks.run(apply=False),
            lambda: bookmarks.run(apply=True),
            _("Remove the extra copies? The oldest copy of each address is kept. Backups are made first."))

    def bookmarks_organize(self):
        import organize
        self._bookmark_task(
            _("Organize bookmarks"),
            lambda: organize.run(apply=False),
            lambda: organize.run(apply=True),
            _("Organize the bookmarks into topic and website folders? Backups are made first."))

    def actions_dialog(self):
        win = tk.Toplevel(self.root)
        win.title(_("Custom actions"))
        win.configure(bg=self.p["bg"])
        ttk.Label(win, text=_("Use {path} where the file path goes. Runs without a shell."),
                  wraplength=460).grid(row=0, column=0, columnspan=2, padx=6, pady=4, sticky="w")
        listbox = tk.Listbox(win, width=70, height=8, bg=self.p["field"], fg=self.p["fg"])
        listbox.grid(row=1, column=0, columnspan=2, padx=6)
        name, command = tk.StringVar(), tk.StringVar()
        ttk.Label(win, text=_("Name")).grid(row=2, column=0, sticky="w", padx=6)
        ttk.Entry(win, textvariable=name, width=60).grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(win, text=_("Command")).grid(row=3, column=0, sticky="w", padx=6)
        ttk.Entry(win, textvariable=command, width=60).grid(row=3, column=1, sticky="w", padx=6)
        items = list(self.cfg.get("actions", []))

        def refresh():
            listbox.delete(0, "end")
            for a in items:
                listbox.insert("end", f"{a['name']}  →  {a['command']}")

        def save_items():
            self.cfg["actions"] = list(items)
            settings.save(self._settings())
            refresh()

        def add():
            if name.get().strip() and command.get().strip():
                items.append({"name": name.get().strip(), "command": command.get().strip()})
                save_items()

        def remove():
            sel = listbox.curselection()
            if sel:
                del items[sel[0]]
                save_items()

        bar = ttk.Frame(win, padding=6)
        bar.grid(row=4, column=0, columnspan=2, sticky="e")
        ttk.Button(bar, text=_("Add action"), command=add).pack(side="left")
        ttk.Button(bar, text=_("Remove selected"), command=remove).pack(side="left", padx=4)
        refresh()

    def _fill_actions_menu(self):
        m = self.menu_actions
        m.delete(0, "end")
        for a in self.cfg.get("actions", []):
            m.add_command(label=a["name"], command=lambda cmd=a["command"]: self.run_custom(cmd))

    def run_custom(self, command):
        paths = self._selected_paths()
        if not paths:
            self.status.set(_("Select a file first."))
            return
        lines = []
        for p in paths:
            try:
                code, out = run_action(command, p)
                lines.append(f"{p}\n  exit {code}" + (f"\n  {out}" if out else ""))
            except (OSError, ValueError) as e:
                lines.append(f"{p}\n  {e}")
        self._listing(_("Custom action"), lines)

    # ----- listings -----------------------------------------------------------

    def _listing(self, title, lines):
        win = tk.Toplevel(self.root)
        win.title(title)
        win.geometry("700x400")
        win.configure(bg=self.p["bg"])
        txt = tk.Text(win, wrap="none", bg=self.p["field"], fg=self.p["fg"])
        txt.pack(fill="both", expand=True)
        txt.insert("end", "\n".join(lines) or _("Nothing to show."))
        txt.configure(state="disabled")

    def show_errors(self):
        self._listing(_("Skipped files ({n})").format(n=len(self.errors)), self.errors)

    def show_unmatched(self):
        self._listing(_("Files in A with no copy in B ({n})").format(n=len(self.unmatched)), self.unmatched)

    def show_clashes(self):
        lines = []
        for c in self.name_clashes:
            lines += c + [""]
        self._listing(_("Name clashes ({n})").format(n=len(self.name_clashes)), lines)

    def show_shortcuts(self):
        self._listing(_("Keyboard shortcuts"), SHORTCUTS)

    def close(self):
        if self.cancel_event:
            self.cancel_event.set()
        if self.watch_event:
            self.watch_event.set()
        settings.save(self._settings())
        self.root.destroy()


def filter_commands(names, query):
    """Commands whose names contain every word of the query, in their original order."""
    words = query.lower().split()
    return [n for n in names if all(w in n.lower() for w in words)]


def main():
    root = TkinterDnD.Tk() if TkinterDnD else tk.Tk()
    start = sys.argv[1] if len(sys.argv) > 1 and os.path.isdir(sys.argv[1]) else None  # e.g. from Nemo
    App(root, start_folder=start)
    root.mainloop()


if __name__ == "__main__":
    main()
