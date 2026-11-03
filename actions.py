"""Actions on duplicate groups: choosing keepers, trashing, moving, hard-linking, opening, exporting."""
import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import defaultdict

from PIL import Image
from send2trash import send2trash

from engine import AUDIO_EXTS, IMAGE_EXTS

KEEP_RULES = ("newest", "oldest", "shortest path", "prefer folder", "prefer clean name", "prefer best version")
RAW_EXTS = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2"}
JPEG_EXTS = {".jpg", ".jpeg"}
_COPY_SUFFIX = re.compile(r"( \(\d+\)| - copy| copy|~|\.copy)$", re.IGNORECASE)


def _clean_penalty(path):
    return 1 if _COPY_SUFFIX.search(os.path.splitext(os.path.basename(path))[0]) else 0


def _quality(path):
    """Resolution for images, bitrate for audio, otherwise file size."""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in IMAGE_EXTS:
            with Image.open(path) as im:
                return im.width * im.height
        if ext in AUDIO_EXTS:
            import mutagen
            info = mutagen.File(path)
            if info is not None and getattr(info.info, "bitrate", None):
                return info.info.bitrate
    except Exception:
        pass
    return os.path.getsize(path)


def keeper(group, rule, prefer=None):
    if rule == "prefer folder" and prefer:
        inside = [p for p in group if p.startswith(prefer.rstrip(os.sep) + os.sep)]
        if inside:
            group = inside
        rule = "newest"
    if rule == "newest":
        return max(group, key=lambda p: (os.path.getmtime(p), p))
    if rule == "oldest":
        return min(group, key=lambda p: (os.path.getmtime(p), p))
    if rule == "prefer clean name":
        return min(group, key=lambda p: (_clean_penalty(p), -os.path.getmtime(p), p))
    if rule == "prefer best version":
        return max(group, key=lambda p: (_quality(p), os.path.getmtime(p), p))
    return min(group, key=lambda p: (len(p), p))


def raw_pair_members(paths):
    """Paths that are one half of a camera RAW and JPEG pair (same name, same folder)."""
    out = set()
    for folder in {os.path.dirname(p) for p in paths}:
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        exts = defaultdict(set)
        for name in names:
            stem, ext = os.path.splitext(name)
            exts[stem.lower()].add(ext.lower())
        for p in paths:
            if os.path.dirname(p) != folder:
                continue
            stem, ext = os.path.splitext(os.path.basename(p))
            ext, siblings = ext.lower(), exts[stem.lower()]
            if (ext in RAW_EXTS and siblings & JPEG_EXTS) or (ext in JPEG_EXTS and siblings & RAW_EXTS):
                out.add(p)
    return out


def plan_trash(groups, selected):
    """What trash_copies would do, without doing it: (paths_to_act_on, kept_copies)."""
    plan, kept = _split_selection(groups, selected)
    return [p for _, chosen in plan for p in chosen], kept


def select_by_rule(groups, keep_rule, prefer, kind, value):
    """Copies that are not the keeper and match a rule: 'older than days' or 'larger than MB'."""
    now = time.time()
    out = set()
    for g in groups:
        k = keeper(g, keep_rule, prefer)
        for p in g:
            if p == k:
                continue
            if kind == "older than days" and os.path.getmtime(p) < now - value * 86400:
                out.add(p)
            elif kind == "larger than MB" and os.path.getsize(p) > value * 1_000_000:
                out.add(p)
    return out


def hardlink_plan(groups):
    """Copies that could become hard links to their group's keeper: exact copies on the same drive.

    Returns (copy, keeper) pairs. Copies on another drive are left out, since hard links cannot cross drives.
    """
    pairs = []
    for g in groups:
        k = keeper(g, "newest")
        dev = os.stat(k).st_dev
        pairs += [(p, k) for p in g if p != k and os.stat(p).st_dev == dev]
    return pairs


def append_history(path, action, paths, detail=""):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    entry = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, "paths": list(paths), "detail": detail}
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")


def read_history(path):
    entries = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    entries.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        pass
    return entries


def _split_selection(groups, selected):
    """For each group, the copies to act on, always leaving at least one copy out of the action."""
    plan, kept = [], []
    for group in groups:
        chosen = [p for p in group if p in selected]
        if len(chosen) == len(group):
            kept.append(chosen.pop(0))
        if chosen:
            plan.append((group, chosen))
    return plan, kept


def trash_copies(groups, selected, trasher=send2trash):
    """Send selected copies to trash. Never trashes the last copy of a group.

    Returns (trashed, kept, failed): kept lists the copies spared to protect a group,
    failed lists (path, message) for files the system refused to trash.
    """
    plan, kept = _split_selection(groups, selected)
    trashed, failed = [], []
    for _, chosen in plan:
        for path in chosen:
            try:
                trasher(path)
                trashed.append(path)
            except OSError as e:
                failed.append((path, str(e)))
    return trashed, kept, failed


def move_copies(groups, selected, dest, undo_path):
    """Move selected copies into dest. The last copy of a group stays put.

    Records the moves in undo_path, replacing any earlier record, so undo_moves can reverse them.
    Returns (moved, kept, failed).
    """
    plan, kept = _split_selection(groups, selected)
    os.makedirs(dest, exist_ok=True)
    moved, failed, log = [], [], []
    for _, chosen in plan:
        for path in chosen:
            target = _free_name(os.path.join(dest, os.path.basename(path)))
            try:
                shutil.move(path, target)
                moved.append(path)
                log.append({"from": path, "to": target})
            except OSError as e:
                failed.append((path, str(e)))
    _write_json(undo_path, log)
    return moved, kept, failed


def undo_moves(undo_path):
    """Put back the files from the last move. Returns (restored, failed)."""
    restored, failed = [], []
    for item in _read_json(undo_path, []):
        if not os.path.exists(item["to"]):
            failed.append((item["to"], "no longer there"))
            continue
        try:
            os.makedirs(os.path.dirname(item["from"]), exist_ok=True)
            shutil.move(item["to"], item["from"])
            restored.append(item["from"])
        except OSError as e:
            failed.append((item["to"], str(e)))
    _write_json(undo_path, [])
    return restored, failed


def hardlink_copies(groups, selected):
    """Replace selected copies with hard links to a kept copy, which frees their disk space.

    Only use this on exact-content groups: a hard link shares the bytes of the file it points to.
    The first unselected copy in each group is the source. Returns (linked, failed).
    """
    linked, failed = [], []
    for group in groups:
        source = next((p for p in group if p not in selected), None)
        if source is None:
            continue
        for path in group:
            if path == source or path not in selected:
                continue
            tmp = path + ".dupefinder-link"
            try:
                if os.stat(path).st_dev != os.stat(source).st_dev:
                    raise OSError("on a different drive, so a hard link is not possible")
                os.link(source, tmp)
                os.replace(tmp, path)  # atomic swap, so the path always points at real data
                linked.append(path)
            except OSError as e:
                if os.path.lexists(tmp):
                    os.unlink(tmp)
                failed.append((path, str(e)))
    return linked, failed


def save_results(path, groups, mode):
    _write_json(path, {"mode": mode, "groups": groups})


def load_results(path):
    """Return (groups, missing, mode): groups whose files still exist, how many were gone, and the scan mode."""
    data = _read_json(path, {"groups": []})
    groups, missing = [], 0
    for group in data.get("groups", []):
        present = [p for p in group if os.path.exists(p)]
        missing += len(group) - len(present)
        if len(present) > 1:
            groups.append(present)
    return groups, missing, data.get("mode", "unknown")


def _free_name(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists(f"{base} ({n}){ext}"):
        n += 1
    return f"{base} ({n}){ext}"


def _write_json(path, data):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def load_ignored(path):
    """Groups the user has marked as not duplicates, as frozensets of paths."""
    return {frozenset(g) for g in _read_json(path, [])}


def save_ignored(path, ignored):
    _write_json(path, [sorted(g) for g in ignored])


def without_ignored(groups, ignored):
    return [g for g in groups if frozenset(g) not in ignored]


def run_action(template, path):
    """Run a user-defined command with {path} replaced by the file. No shell is involved.

    Returns (exit_code, output).
    """
    args = [path if a == "{path}" else a.replace("{path}", path) for a in shlex.split(template)]
    proc = subprocess.run(args, capture_output=True, text=True, timeout=120)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])


def show_in_folder(path):
    open_path(os.path.dirname(path))


def export(groups, path):
    if path.lower().endswith(".html"):
        from report import write_html
        write_html(groups, path)
        return
    rows = [(i, p, os.path.getsize(p)) for i, g in enumerate(groups, 1) for p in g]
    if path.lower().endswith(".xlsx"):
        from openpyxl import Workbook
        wb = Workbook()
        ws = wb.active
        ws.title = "Duplicates"
        ws.append(["group", "path", "size"])
        for row in rows:
            ws.append(list(row))
        wb.save(path)
        return
    if path.lower().endswith(".json"):
        with open(path, "w") as f:
            json.dump([{"group": i, "path": p, "size": s} for i, p, s in rows], f, indent=2)
    else:
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["group", "path", "size"])
            w.writerows(rows)
