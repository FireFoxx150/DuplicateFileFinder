"""Find and restore files sent to the desktop trash (freedesktop.org Trash specification)."""
import os
import shutil
import urllib.parse


def _home_trash():
    base = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(base, "Trash")


def trash_dirs(mounts):
    """(trash_dir, top_dir) pairs: the home trash, then each mounted drive's trash."""
    uid = os.getuid()
    dirs = [(_home_trash(), "/")]
    for m in mounts:
        for rel in (f".Trash-{uid}", os.path.join(".Trash", str(uid))):
            d = os.path.join(m, rel)
            if os.path.isdir(d):
                dirs.append((d, m))
    return dirs


def _read_info(path):
    info = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if "=" in line:
                key, value = line.rstrip("\n").split("=", 1)
                info[key] = value
    return info


def find_trashed(original, mounts):
    """Trash entries that came from original, newest first: list of (files_path, info_path)."""
    target = os.path.normpath(original)
    hits = []
    for trash, top in trash_dirs(mounts):
        info_dir = os.path.join(trash, "info")
        if not os.path.isdir(info_dir):
            continue
        for name in os.listdir(info_dir):
            if not name.endswith(".trashinfo"):
                continue
            info_path = os.path.join(info_dir, name)
            try:
                info = _read_info(info_path)
            except OSError:
                continue
            raw = urllib.parse.unquote(info.get("Path", ""))
            # Paths are absolute, or relative to the drive's top folder. Some tools write them
            # relative to the folder that holds the trash, so accept that too.
            candidates = [raw] if raw.startswith("/") else [
                os.path.join(top, raw), os.path.join(os.path.dirname(trash), raw)]
            files_path = os.path.join(trash, "files", name[:-len(".trashinfo")])
            if any(os.path.normpath(c) == target for c in candidates) and os.path.exists(files_path):
                hits.append((info.get("DeletionDate", ""), files_path, info_path))
    hits.sort(reverse=True)
    return [(files, info) for _, files, info in hits]


def restore(original, mounts):
    """Move the newest trashed copy of original back to its place. Returns True if one was found."""
    hits = find_trashed(original, mounts)
    if not hits:
        return False
    files_path, info_path = hits[0]
    if os.path.exists(original):
        raise FileExistsError(f"{original} already exists")
    os.makedirs(os.path.dirname(original), exist_ok=True)
    shutil.move(files_path, original)
    os.remove(info_path)
    return True
