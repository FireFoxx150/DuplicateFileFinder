"""Command-line duplicate finder: text or JSON output, cleanup with a dry run, watching, and webhooks."""
import argparse
import json
import os
import shutil
import smtplib
import subprocess
import sys
import threading
import time
import urllib.request

import actions
from actions import append_history, keeper, plan_trash, raw_pair_members, select_by_rule, trash_copies
from engine import SKIP_PRESET, Options, Result, find_duplicates
from report import write_html

STATE = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
HISTORY_PATH = os.path.join(STATE, "dupefinder", "history.jsonl")
DEBOUNCE_SECONDS = 2


def _notify(text):
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "Duplicate File Finder", text], check=False)


def post_webhook(url, payload, timeout=10):
    """POST a JSON document to url. Raises OSError (or a subclass) if the server cannot be reached."""
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status


def send_report(groups, args):
    """Email the HTML report as an attachment, through the SMTP server given on the command line."""
    import smtplib
    import tempfile
    from email.message import EmailMessage

    msg = EmailMessage()
    msg["Subject"] = f"Duplicate files in {args.folder}: {len(groups)} groups"
    msg["From"] = args.mail_from or args.mail_to
    msg["To"] = args.mail_to
    msg.set_content(f"{len(groups)} duplicate groups were found. The full report is attached.")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "duplicates.html")
        write_html(groups, path)
        with open(path, "rb") as f:
            msg.add_attachment(f.read(), maintype="text", subtype="html", filename="duplicates.html")
    with smtplib.SMTP(args.smtp_host, args.smtp_port, timeout=30) as smtp:
        if args.smtp_tls:
            smtp.starttls()
        smtp.send_message(msg)


def _options(args):
    threshold = args.threshold
    if threshold is None:
        threshold = {"images": 16, "videos": 70, "documents": 70, "audio": 75}.get(args.mode, 5)
    patterns = set(args.exclude) | (set(SKIP_PRESET) if args.skip_presets else set())
    now = time.time()
    return Options(
        min_size=int(args.min_size_kb * 1024),
        max_size=int(args.max_size_mb * 1_000_000) if args.max_size_mb else None,
        modified_after=now - args.modified_within_days * 86400 if args.modified_within_days else None,
        include_hidden=args.include_hidden, exclude_patterns=frozenset(patterns),
        max_depth=args.max_depth, mode=args.mode, threshold=threshold, verify_bytes=args.verify,
        respect_dupeignore=not args.no_dupeignore,
    )


def _emit(res, args):
    if args.html:
        write_html(res.groups, args.html)
    if args.json:
        json.dump({"groups": res.groups, "name_clashes": res.name_clashes, "errors": res.errors},
                  sys.stdout, indent=2)
        print()
    else:
        for i, group in enumerate(res.groups, 1):
            print(f"Group {i}:")
            for path in group:
                print(f"  {path}")
    sys.stdout.flush()  # results reach pipes and log files even if the process is stopped later
    summary = f"{len(res.groups)} duplicate groups, {len(res.errors)} files skipped."
    print(summary, file=sys.stderr)
    if args.notify:
        _notify(summary)
    if args.webhook and res.groups:
        try:
            post_webhook(args.webhook, {"folder": args.folder, "groups": res.groups})
        except OSError as e:
            print(f"webhook failed: {e}", file=sys.stderr)
    if args.mail_to and res.groups:
        if not args.smtp_host:
            print("email skipped: --mail-to needs --smtp-host", file=sys.stderr)
        else:
            try:
                send_report(res.groups, args)
            except (OSError, smtplib.SMTPException) as e:
                print(f"email failed: {e}", file=sys.stderr)


def _cleanup(args, opts):
    res = find_duplicates(args.folder, opts)
    if args.older_than or args.larger_than_mb:
        kind, value = (("older than days", args.older_than) if args.older_than
                       else ("larger than MB", args.larger_than_mb))
        chosen = select_by_rule(res.groups, args.keep_rule, args.prefer, kind, value)
    else:
        chosen = {p for g in res.groups for p in g if p != keeper(g, args.keep_rule, args.prefer)}
    if not args.no_protect_raw:
        chosen -= raw_pair_members(list(chosen))
    would, kept = plan_trash(res.groups, chosen)
    if args.apply and len(would) > args.max_files:  # a fixed cap, so a wrong keep rule cannot trash a library
        print(f"Refused: {len(would)} copies is more than --max-files {args.max_files}. Nothing was changed. "
              "Check the list with a dry run, then raise --max-files if it is right.", file=sys.stderr)
        return 2
    for p in sorted(would):
        print(f"  {'trash' if args.apply else 'would trash'}  {p}")
    print(f"{len(would)} copies selected; {len(kept)} group(s) keep their last copy untouched.")
    if not args.apply:
        print("Dry run: nothing was changed. Add --apply to move these to the trash.")
        return 0
    trashed, kept, failed = trash_copies(res.groups, chosen)
    append_history(HISTORY_PATH, "cleanup", trashed, detail=f"keep={args.keep_rule} folder={args.folder}")
    print(f"Moved {len(trashed)} copies to the trash.")
    for p, msg in failed:
        print(f"  could not trash {p}: {msg}", file=sys.stderr)
    return 1 if failed else 0


def _live(args, opts):
    """Watch the folder with inotify; after changes settle, rescan and report only new groups."""
    import watcher
    changed = threading.Event()
    stop = threading.Event()
    threading.Thread(target=watcher.watch, args=(args.folder, lambda p, m: changed.set(), stop),
                     daemon=True).start()
    seen = set()
    print(f"Watching {args.folder}. Press Ctrl+C to stop.", file=sys.stderr)
    try:
        while True:
            if not changed.wait(1):
                continue
            time.sleep(DEBOUNCE_SECONDS)  # let a copy finish writing before scanning
            changed.clear()
            res = find_duplicates(args.folder, opts)
            new = [g for g in res.groups if frozenset(g) not in seen]
            seen.update(frozenset(g) for g in res.groups)
            if new:
                _emit(Result(groups=new, errors=res.errors), args)
    except KeyboardInterrupt:
        stop.set()
    return 0


def main(argv=None):
    words = sys.argv[1:] if argv is None else argv
    if words[:1] == ["bookmarks"]:  # dupefinder-cli bookmarks [--apply]: browser bookmarks, not folders
        import bookmarks
        return bookmarks.main(words[1:])
    if words[:1] == ["organize"]:  # dupefinder-cli organize [--apply]: sort bookmarks into topic folders
        import organize
        return organize.main(words[1:])
    p = argparse.ArgumentParser(prog="dupefinder-cli", description="Find duplicate files.")
    p.add_argument("folder", help="folder to scan")
    p.add_argument("--mode", choices=["exact", "images", "documents", "videos", "audio"], default="exact")
    p.add_argument("--threshold", type=float, default=None,
                   help="images: max distance of 256 (default 16); videos: min share of matching frames, percent (default 90); "
                        "documents and audio: min similarity %% (default 70 and 75)")
    p.add_argument("--min-size-kb", type=float, default=0)
    p.add_argument("--max-size-mb", type=float, default=None, help="ignore files larger than this")
    p.add_argument("--modified-within-days", type=float, default=None, help="only files changed this recently")
    p.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                   help="skip files or folders matching GLOB (repeatable)")
    p.add_argument("--skip-presets", action="store_true", help="also skip cache and build folders")
    p.add_argument("--no-dupeignore", action="store_true", help="ignore .dupeignore files in folders")
    p.add_argument("--max-depth", type=int, default=None, help="subfolder levels to descend")
    p.add_argument("--include-hidden", action="store_true")
    p.add_argument("--verify", action="store_true", help="compare file bytes as well as hashes")
    p.add_argument("--json", action="store_true", help="print JSON instead of text")
    p.add_argument("--html", metavar="FILE", help="also write an HTML report")
    p.add_argument("--notify", action="store_true", help="show a desktop notification with the summary")
    p.add_argument("--webhook", metavar="URL", help="POST new groups to URL as JSON")
    p.add_argument("--watch", type=int, metavar="SECONDS",
                   help="keep running: rescan every SECONDS and report only groups not seen before")
    p.add_argument("--live", action="store_true",
                   help="keep running: rescan when files change (Linux inotify), report only new groups")
    cl = p.add_argument_group("cleanup (dry run unless --apply)")
    cl.add_argument("--cleanup", action="store_true", help="select copies to remove, keeping one per group")
    cl.add_argument("--keep-rule", choices=list(actions.KEEP_RULES), default="newest")
    cl.add_argument("--prefer", metavar="FOLDER", help="folder to prefer when keeping copies")
    cl.add_argument("--older-than", type=float, metavar="DAYS", help="select only copies older than DAYS")
    cl.add_argument("--larger-than-mb", type=float, metavar="MB", help="select only copies larger than MB")
    cl.add_argument("--no-protect-raw", action="store_true", help="allow RAW and JPEG pairs to be selected")
    cl.add_argument("--apply", action="store_true", help="actually move the selected copies to the trash")
    cl.add_argument("--max-files", type=int, default=500, help="with --apply, refuse to trash more than this many")
    p.add_argument("--hardlink-plan", action="store_true",
                   help="list copies that could be replaced by hard links (same drive as the kept copy)")
    p.add_argument("--mail-to", metavar="ADDRESS", help="email the HTML report to ADDRESS")
    p.add_argument("--smtp-host", help="SMTP server for --mail-to")
    p.add_argument("--smtp-port", type=int, default=25)
    p.add_argument("--smtp-tls", action="store_true", help="use STARTTLS for --mail-to")
    p.add_argument("--mail-from", help="sender address for --mail-to (default: the recipient)")
    args = p.parse_args(argv)
    opts = _options(args)

    if args.hardlink_plan:
        pairs = actions.hardlink_plan(find_duplicates(args.folder, opts).groups)
        for copy, src in pairs:
            print(f"  {copy}  ->  {src}")
        print(f"{len(pairs)} copies could be hard-linked to a kept copy on the same drive.")
        return 0
    if args.cleanup:
        return _cleanup(args, opts)
    if args.live:
        return _live(args, opts)
    if not args.watch:
        _emit(find_duplicates(args.folder, opts), args)
        return 0

    seen = set()
    while True:
        res = find_duplicates(args.folder, opts)
        new = [g for g in res.groups if frozenset(g) not in seen]
        seen.update(frozenset(g) for g in res.groups)
        if new:
            _emit(Result(groups=new, errors=res.errors), args)
        time.sleep(args.watch)


if __name__ == "__main__":
    sys.exit(main())
