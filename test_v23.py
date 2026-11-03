import http.server
import json
import os
import tempfile
import threading
import time
import unittest

from openpyxl import load_workbook
from PIL import Image

import actions
import cli
import watcher
from engine import Options, find_duplicates
from gui import filter_commands


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _names(groups):
    return sorted(sorted(os.path.basename(p) for p in g) for g in groups)


class KeepRulesTest(unittest.TestCase):
    def test_prefer_clean_name_over_copy_suffixes(self):
        with tempfile.TemporaryDirectory() as root:
            clean = _write(root, "photo.jpg", b"x")
            copy = _write(root, "photo (1).jpg", b"x")
            os.utime(copy, (9_999_999_999, 9_999_999_999))  # the copy is newer
            self.assertEqual(actions.keeper([clean, copy], "prefer clean name"), clean)

    def test_prefer_best_version_keeps_the_larger_image(self):
        with tempfile.TemporaryDirectory() as root:
            big = os.path.join(root, "big.png")
            small = os.path.join(root, "small.png")
            Image.new("RGB", (200, 200)).save(big)
            Image.new("RGB", (20, 20)).save(small)
            os.utime(small, (9_999_999_999, 9_999_999_999))
            self.assertEqual(actions.keeper([big, small], "prefer best version"), big)


class RawPairTest(unittest.TestCase):
    def test_raw_and_jpeg_in_the_same_folder_are_protected(self):
        with tempfile.TemporaryDirectory() as root:
            raw = _write(root, "shot.cr2", b"raw")
            jpg = _write(root, "shot.jpg", b"jpg")
            lone = _write(root, "other.cr2", b"raw")
            self.assertEqual(actions.raw_pair_members([raw, jpg, lone]), {raw, jpg})


class SelectByRuleTest(unittest.TestCase):
    def test_older_than_days_selects_only_old_copies_that_are_not_kept(self):
        with tempfile.TemporaryDirectory() as root:
            new = _write(root, "new.txt", b"x")
            old = _write(root, "old.txt", b"x")
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (9_999_999_999, 9_999_999_999))
            chosen = actions.select_by_rule([[new, old]], "newest", None, "older than days", 30)
            self.assertEqual(chosen, {old})

    def test_plan_trash_reports_what_would_happen_and_keeps_one(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a", b"x")
            b = _write(root, "b", b"x")
            would, kept = actions.plan_trash([[a, b]], {a, b})
            self.assertEqual((len(would), len(kept)), (1, 1))


class ScanFilterTest(unittest.TestCase):
    def test_dupeignore_file_hides_matching_files(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, ".dupeignore", b"# comment\n*.bak\n")
            _write(root, "a.bak", b"same")
            _write(root, "b.bak", b"same")
            _write(root, "c.txt", b"same")
            _write(root, "d.txt", b"same")
            self.assertEqual(_names(find_duplicates(root).groups), [["c.txt", "d.txt"]])

    def test_dupeignore_can_be_turned_off(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, ".dupeignore", b"*.bak\n")
            _write(root, "a.bak", b"same")
            _write(root, "b.bak", b"same")
            res = find_duplicates(root, Options(respect_dupeignore=False))
            self.assertEqual(_names(res.groups), [["a.bak", "b.bak"]])

    def test_max_size_keeps_only_small_files(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "big1", b"x" * 100)
            _write(root, "big2", b"x" * 100)
            _write(root, "small1", b"y")
            _write(root, "small2", b"y")
            self.assertEqual(_names(find_duplicates(root, Options(max_size=50)).groups), [["small1", "small2"]])
            self.assertEqual(len(find_duplicates(root).groups), 2)

    def test_modified_after_keeps_only_recent_files(self):
        with tempfile.TemporaryDirectory() as root:
            old1 = _write(root, "old1", b"same")
            old2 = _write(root, "old2", b"same")
            _write(root, "new1", b"fresh")
            _write(root, "new2", b"fresh")
            for p in (old1, old2):
                os.utime(p, (1_000_000, 1_000_000))
            res = find_duplicates(root, Options(modified_after=time.time() - 86400))
            self.assertEqual(_names(res.groups), [["new1", "new2"]])


class HistoryAndExportTest(unittest.TestCase):
    def test_history_round_trip(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "history.jsonl")
            actions.append_history(path, "trash", ["/x/a"], detail="test")
            actions.append_history(path, "move", ["/x/b"], detail="dest")
            entries = actions.read_history(path)
            self.assertEqual([e["action"] for e in entries], ["trash", "move"])
            self.assertEqual(entries[1]["detail"], "dest")

    def test_excel_export_opens_with_the_same_rows(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a.txt", b"12345")
            b = _write(root, "b.txt", b"12345")
            out = os.path.join(root, "out.xlsx")
            actions.export([[a, b]], out)
            rows = [list(r) for r in load_workbook(out).active.iter_rows(values_only=True)]
            self.assertEqual(rows[0], ["group", "path", "size"])
            self.assertEqual(rows[1:], [[1, a, 5], [1, b, 5]])


class WebhookTest(unittest.TestCase):
    def test_post_webhook_delivers_json(self):
        received = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers["Content-Length"])
                received["body"] = json.loads(self.rfile.read(length))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{server.server_address[1]}/hook"
            self.assertEqual(cli.post_webhook(url, {"groups": [["/a", "/b"]]}), 200)
            self.assertEqual(received["body"], {"groups": [["/a", "/b"]]})
        finally:
            server.shutdown()


class CommandPaletteTest(unittest.TestCase):
    def test_every_word_must_match(self):
        names = ["Scan", "Move to trash", "Theme: dark", "Theme: high contrast"]
        self.assertEqual(filter_commands(names, "theme dark"), ["Theme: dark"])
        self.assertEqual(filter_commands(names, ""), names)


class InotifyTest(unittest.TestCase):
    def test_new_files_and_new_folders_are_reported(self):
        with tempfile.TemporaryDirectory() as root:
            seen = []
            stop = threading.Event()
            t = threading.Thread(target=watcher.watch,
                                 args=(root, lambda p, m: seen.append(os.path.relpath(p, root)), stop), daemon=True)
            t.start()
            time.sleep(0.3)
            _write(root, "top.txt", b"x")
            os.makedirs(os.path.join(root, "deep"))
            time.sleep(0.3)
            _write(root, os.path.join("deep", "inner.txt"), b"y")
            time.sleep(1.2)
            stop.set()
            t.join(5)
            self.assertIn("top.txt", seen)
            self.assertIn(os.path.join("deep", "inner.txt"), seen)


if __name__ == "__main__":
    unittest.main()
