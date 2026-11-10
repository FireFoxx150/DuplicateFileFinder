"""Sort bookmarks into topic folders, then one folder per website inside each topic.

Topics come from the website's address, using the first matching rule. Anything unmatched goes to
Other. Moving a bookmark in Firefox bumps its sync counter, so Firefox Sync uploads the change.
"""
import json
import os
import re
import secrets
import sqlite3
import time
import urllib.parse

from bookmarks import _brave_load, _brave_urls, backup, browser_running, brave_profiles, firefox_profiles

# (topic, words that appear in the website address). Order matters: the first match wins.
TOPICS = [
    ("Adult", ("pornhub", "xvideos", "xhamster", "xtube", "pornmd", "pornskirt", "pornsocket", "thumbzilla",
               "rule34", "luscious", "e621", "hentai", "yaoi", "u18chan", "loverslab", "f95zone",
               "naughtymachinima", "animopron", "animestigma", "yaoihavenreborn", "slushe", "baraag")),
    ("Art & Creative", ("furaffinity", "inkbunny", "deviantart", "artstation", "picarto", "pixiv",
                        "hentai-foundry")),
    ("3D & AI", ("civitai", "daz3d", "3d-load", "render-state", "sketchfab", "blender")),
    ("Social", ("twitter", "x.com", "reddit", "facebook", "instagram", "tumblr", "bsky", "mastodon",
                "threads.net", "discord")),
    ("Video", ("youtube", "twitch", "vimeo", "dailymotion")),
    ("Dev & Tech", ("github", "gitlab", "stackoverflow", "stackexchange", "mozilla", "pypi", "npmjs",
                    "kernel.org", "archlinux", "ubuntu", "linux")),
    ("Files & Cloud", ("mega.nz", "drive.google", "docs.google", "dropbox", "onedrive", "mediafire")),
    ("Shopping", ("amazon", "ebay", "etsy", "aliexpress", "argos", "currys")),
    ("News", ("bbc.", "cnn.", "guardian", "reuters", "news")),
    ("Learning", ("zenva", "coursera", "udemy", "khanacademy", "wikipedia")),
]
OTHER = "Other"


def site_of(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host.removeprefix("www.") or "(no site)"


def topic_of(url):
    """Topic from the website name only; a word must start a name, so "x.com" does not match "sex.com"."""
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    for topic, words in TOPICS:
        if any(re.search(r"(^|[.\-])" + re.escape(w), host) for w in words):
            return topic
    return OTHER


def plan(urls):
    """{topic: {site: [urls]}} for a list of web addresses, with topics and sites sorted."""
    tree = {}
    for url in urls:
        tree.setdefault(topic_of(url), {}).setdefault(site_of(url), []).append(url)
    return {t: dict(sorted(sites.items())) for t, sites in sorted(tree.items())}


def _firefox_rows(path):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only here
    try:
        return con.execute("""SELECT b.id, p.url FROM moz_bookmarks b JOIN moz_places p ON p.id = b.fk
                              WHERE b.type = 1 AND p.url LIKE 'http%'""").fetchall()
    finally:
        con.close()


FIREFOX_ROOTS = ("root________", "menu________", "toolbar_____", "unfiled_____", "mobile______")


def apply_firefox(path):
    """Put every bookmark into topic -> website folders on the toolbar, then remove folders left empty.

    Every move and removal is recorded in moz_bookmarks_deleted or moz_bookmarks.syncChangeCounter,
    so Firefox Sync uploads it.
    """
    rows = _firefox_rows(path)
    tree = plan([url for _id, url in rows])
    ids_by_url = {}
    for bid, url in rows:
        ids_by_url.setdefault(url, []).append(bid)
    now = int(time.time() * 1000000)
    con = sqlite3.connect(path)
    removed = 0
    try:
        with con:  # one transaction: the whole change or nothing
            toolbar = con.execute("SELECT id FROM moz_bookmarks WHERE guid = 'toolbar_____'").fetchone()[0]
            for tpos, (topic, sites) in enumerate(tree.items()):
                tid = _firefox_folder(con, toolbar, topic, tpos, now)
                for spos, (site, urls) in enumerate(sites.items()):
                    sid = _firefox_folder(con, tid, site, spos, now)
                    for pos, url in enumerate(urls):
                        bid = ids_by_url[url].pop(0)
                        con.execute("""UPDATE moz_bookmarks SET parent = ?, position = ?, lastModified = ?,
                                       syncChangeCounter = syncChangeCounter + 1 WHERE id = ?""",
                                    (sid, pos, now, bid))
            removed = _firefox_remove_empty(con, now)
    finally:
        con.close()
    return tree, removed


def _firefox_remove_empty(con, now):
    """Delete user folders that hold nothing, repeating until none are left. Records each deletion."""
    total = 0
    while True:
        empties = con.execute("""SELECT f.id, f.guid FROM moz_bookmarks f WHERE f.type = 2
                                 AND f.guid NOT IN ({}) AND NOT EXISTS
                                 (SELECT 1 FROM moz_bookmarks c WHERE c.parent = f.id)""".format(
            ",".join("?" * len(FIREFOX_ROOTS))), FIREFOX_ROOTS).fetchall()
        if not empties:
            return total
        for fid, guid in empties:
            con.execute("INSERT OR REPLACE INTO moz_bookmarks_deleted (guid, dateRemoved) VALUES (?, ?)",
                        (guid, now))
            con.execute("DELETE FROM moz_bookmarks WHERE id = ?", (fid,))
            total += 1


def _firefox_folder(con, parent, title, position, now):
    cur = con.execute("""INSERT INTO moz_bookmarks (type, fk, parent, position, title, dateAdded, lastModified,
                         guid, syncStatus, syncChangeCounter) VALUES (2, NULL, ?, ?, ?, ?, ?, ?, 0, 1)""",
                      (parent, position, title, now, now, secrets.token_urlsafe(9)[:12]))
    return cur.lastrowid


def apply_brave(path):
    """Put every bookmark into topic -> website folders on the bookmark bar, then remove empty folders."""
    data = _brave_load(path)
    nodes = _brave_urls(data)
    tree = plan([n["url"] for n, _p in nodes])
    pool = {}
    for node, parent in nodes:
        pool.setdefault(node["url"], []).append((node, parent))
    next_id = max(int(n["id"]) for n, _p in nodes) + 1
    bar = data["roots"]["bookmark_bar"]

    def new_folder(parent_list, name):
        nonlocal next_id
        f = {"id": str(next_id), "type": "folder", "name": name, "children": [],
             "date_added": str(int(time.time() * 1000000)), "date_modified": "0"}
        next_id += 1
        parent_list.append(f)
        return f["children"]

    for topic, sites in tree.items():
        tlist = new_folder(bar["children"], topic)
        for site, urls in sites.items():
            slist = new_folder(tlist, site)
            for url in urls:
                node, parent = pool[url].pop(0)
                parent.remove(node)
                slist.append(node)
    removed = _brave_remove_empty(data)
    data.pop("checksum", None)
    tmp = path + ".dupefinder-tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=3)
    os.replace(tmp, path)
    return tree, removed


def _brave_remove_empty(data):
    """Drop folders that hold no bookmarks anywhere under them, in every root. Keeps the roots themselves."""
    removed = 0

    def has_url(node):
        return any(c.get("type") == "url" or has_url(c) for c in node.get("children", []))

    def prune(node):
        nonlocal removed
        keep = []
        for c in node.get("children", []):
            if c.get("type") == "folder":
                prune(c)
                if not has_url(c):
                    removed += 1
                    continue
            keep.append(c)
        node["children"] = keep

    for root in data["roots"].values():
        if isinstance(root, dict):
            prune(root)
    return removed


def _show(label, tree):
    total = sum(len(urls) for sites in tree.values() for urls in sites.values())
    lines = [f"{label}: {total} bookmarks in {len(tree)} topic(s)"]
    for topic, sites in tree.items():
        lines.append(f"  {topic}  ({sum(len(u) for u in sites.values())})")
        for site, urls in list(sites.items())[:6]:
            lines.append(f"    {site}  ({len(urls)})")
        if len(sites) > 6:
            lines.append(f"    ... {len(sites) - 6} more site(s)")
    return lines


def run(apply=False, allow_running=False):
    """Plan, and if apply, back up and organize every profile. Returns the lines to show."""
    firefox, brave = firefox_profiles()[:1], brave_profiles()[:1]
    lines = []
    for path in firefox:
        lines += _show("firefox", plan([url for _id, url in _firefox_rows(path)]))
    for path in brave:
        lines += _show("brave", plan([n["url"] for n, _p in _brave_urls(_brave_load(path))]))
    if not apply:
        return lines + ["Dry run: nothing was changed. Organize to make the change."]
    running = [b for b, present in (("firefox", firefox), ("brave", brave)) if present and browser_running(b)]
    if running and not allow_running:
        return [f"Close {', '.join(running)} first. Nothing was changed."]
    lines.append(f"Backed up to {backup(firefox + brave)}")
    for path in firefox:
        _tree, removed = apply_firefox(path)
        lines.append(f"firefox: organized, removed {removed} empty folder(s)")
    for path in brave:
        _tree, removed = apply_brave(path)
        lines.append(f"brave: organized, removed {removed} empty folder(s)")
    return lines


def main(argv=None):
    import argparse
    p = argparse.ArgumentParser(prog="dupefinder-cli organize",
                                description="Sort bookmarks into topic folders, one folder per website.")
    p.add_argument("--apply", action="store_true", help="make the change (default: show the layout only)")
    p.add_argument("--allow-running", action="store_true")
    args = p.parse_args(argv)
    lines = run(apply=args.apply, allow_running=args.allow_running)
    print("\n".join(lines))
    return 2 if lines and lines[0].startswith("Close ") else 0
