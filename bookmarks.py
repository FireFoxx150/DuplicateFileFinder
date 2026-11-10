"""Find and remove duplicate bookmarks in Firefox and Brave (Chromium).

A duplicate is any extra bookmark for an address that is already saved, in any folder.
The oldest saved copy is kept. A dry run is the default; applying backs up both bookmark
files first and refuses to run while the browser is open.
"""
import json
import os
import shutil
import sqlite3
import subprocess
import time

BACKUP_ROOT = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
                           "dupefinder", "bookmark-backups")
BROWSER_PROCESS = {"firefox": "firefox", "brave": "brave"}


def firefox_profiles(base=os.path.expanduser("~/.mozilla/firefox")):
    if not os.path.isdir(base):
        return []
    out = []
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d, "places.sqlite")
        if os.path.exists(p):
            out.append(p)
    return out


def brave_profiles(base=os.path.expanduser("~/.config/BraveSoftware/Brave-Browser")):
    if not os.path.isdir(base):
        return []
    out = []
    for d in sorted(os.listdir(base)):
        p = os.path.join(base, d, "Bookmarks")
        if os.path.isfile(p):
            out.append(p)
    return out


def browser_running(browser):
    return subprocess.run(["pgrep", "-x", BROWSER_PROCESS[browser]], capture_output=True).returncode == 0


def _firefox_rows(path):
    """(bookmark id, url, date added) for every web bookmark in one Firefox profile."""
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only: never changes the profile here
    try:
        return con.execute(
            """SELECT b.id, p.url, COALESCE(b.dateAdded, 0) FROM moz_bookmarks b
               JOIN moz_places p ON p.id = b.fk WHERE b.type = 1 AND p.url LIKE 'http%'""").fetchall()
    finally:
        con.close()


def _brave_load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _brave_urls(data):
    """(node, parent children list) for every web bookmark in a Brave/Chromium bookmark tree."""
    found = []

    def walk(node):
        for child in node.get("children", []):
            if child.get("type") == "url":
                found.append((child, node["children"]))
            else:
                walk(child)

    for root in data["roots"].values():
        if isinstance(root, dict):
            walk(root)
    return found


def plan_firefox(path):
    """Extra bookmarks in one Firefox profile: list of (bookmark id, address)."""
    rows = _firefox_rows(path)
    keep = {}
    for bid, url, added in sorted(rows, key=lambda r: (r[2], r[0])):
        keep.setdefault(url, bid)
    return [(bid, url) for bid, url, _added in rows if keep[url] != bid]


def plan_brave(path):
    """Extra bookmarks in one Brave profile: list of (bookmark id, address)."""
    nodes = _brave_urls(_brave_load(path))
    keep = {}
    for node, _parent in sorted(nodes, key=lambda n: (int(n[0].get("date_added", 0)), int(n[0]["id"]))):
        keep.setdefault(node["url"], node["id"])
    return [(node["id"], node["url"]) for node, _parent in nodes if keep[node["url"]] != node["id"]]


def backup(paths):
    """Copy each file into a new timestamped folder under the state directory. Returns that folder."""
    dest = os.path.join(BACKUP_ROOT, time.strftime("%Y%m%d-%H%M%S"))
    os.makedirs(dest, exist_ok=True)
    for i, p in enumerate(paths):
        shutil.copy2(p, os.path.join(dest, f"{i}-{os.path.basename(p)}"))
    return dest


def apply_firefox(path, bookmark_ids):
    """Delete bookmarks and record each deletion in moz_bookmarks_deleted, which Firefox Sync uploads.

    Without that record, Sync still holds the bookmarks and restores them on the next start.
    """
    con = sqlite3.connect(path)
    try:
        with con:  # one transaction: all of the deletions or none
            for bid in bookmark_ids:
                guid = con.execute("SELECT guid FROM moz_bookmarks WHERE id = ?", (bid,)).fetchone()
                if guid is None:
                    continue
                con.execute("INSERT OR REPLACE INTO moz_bookmarks_deleted (guid, dateRemoved) VALUES (?, ?)",
                            (guid[0], int(time.time() * 1000000)))
                con.execute("DELETE FROM moz_bookmarks WHERE id = ?", (bid,))
    finally:
        con.close()


def apply_brave(path, bookmark_ids):
    data = _brave_load(path)
    drop = {str(b) for b in bookmark_ids}
    for node, parent in _brave_urls(data):
        if node["id"] in drop:
            parent.remove(node)
    data.pop("checksum", None)  # Chromium recalculates this on start; a stale value makes it warn
    tmp = path + ".dupefinder-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=3)
    os.replace(tmp, path)  # atomic swap, so the file is never left half-written


def run(apply=False, allow_running=False):
    """Plan, and if apply, back up and remove the extra copies. Returns the lines to show."""
    plans = []  # (browser, profile file, plan list, applier)
    for path in firefox_profiles():
        plans.append(("firefox", path, plan_firefox(path), apply_firefox))
    for path in brave_profiles():
        plans.append(("brave", path, plan_brave(path), apply_brave))
    if not plans:
        return ["No Firefox or Brave bookmark files found."]
    lines, total = [], 0
    for browser, path, extra, _apply in plans:
        total += len(extra)
        lines.append(f"{browser}: {len(extra)} extra bookmark(s)")
        for _bid, url in sorted(extra, key=lambda x: x[1])[:20]:
            lines.append(f"    would remove  {url}")
        if len(extra) > 20:
            lines.append(f"    ... and {len(extra) - 20} more")
    if not apply:
        return lines + [f"Dry run: nothing was changed. {total} extra bookmark(s) in total. Apply to remove them."]
    running = sorted({b for b, _p, extra, _a in plans if extra and browser_running(b)})
    if running and not allow_running:
        return [f"Close {', '.join(running)} first. Nothing was changed."]
    lines.append(f"Backed up the bookmark files to {backup([p for _b, p, extra, _a in plans if extra])}")
    for browser, path, extra, applier in plans:
        if extra:
            applier(path, [bid for bid, _url in extra])
            lines.append(f"{browser}: removed {len(extra)} extra bookmark(s)")
    return lines


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="dupefinder-cli bookmarks",
                                description="Remove duplicate bookmarks in Firefox and Brave (oldest copy kept).")
    p.add_argument("--apply", action="store_true", help="remove the extra copies (default: dry run)")
    p.add_argument("--allow-running", action="store_true",
                   help="apply even if the browser is open (not recommended: it can overwrite the change)")
    args = p.parse_args(argv)
    lines = run(apply=args.apply, allow_running=args.allow_running)
    print("\n".join(lines))
    return 2 if lines and lines[0].startswith("Close ") else 0
