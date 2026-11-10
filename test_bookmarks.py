import json
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

import bookmarks


def _firefox_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER, parent INTEGER,
                                    title TEXT, dateAdded INTEGER, guid TEXT);
        CREATE TABLE moz_bookmarks_deleted (guid TEXT PRIMARY KEY, dateRemoved INTEGER);
        INSERT INTO moz_places VALUES (1, 'https://a.example/'), (2, 'https://b.example/'), (3, 'https://c.example/');
        -- 10 is the oldest copy of a; 11 is a copy in another folder; 12 is the only b; 13 and 14 copy c
        INSERT INTO moz_bookmarks VALUES (10, 1, 1, 2, 'a', 100, 'g10'), (11, 1, 1, 3, 'a', 300, 'g11'),
                                         (12, 1, 2, 2, 'b', 200, 'g12'), (13, 1, 3, 2, 'c', 400, 'g13'), (14, 1, 3, 3, 'c', 500, 'g14'),
                                         (15, 2, NULL, 1, 'folder', 50, 'g15');
    """)
    con.commit()
    con.close()


def _brave_json(path):
    def url(i, u, added):
        return {"id": str(i), "type": "url", "name": u, "url": u, "date_added": str(added)}
    tree = {"checksum": "stale", "roots": {
        "bookmark_bar": {"id": "1", "type": "folder", "name": "Bar", "children": [
            url(10, "https://a.example/", 100), url(12, "https://b.example/", 200)]},
        "other": {"id": "2", "type": "folder", "name": "Other", "children": [
            {"id": "3", "type": "folder", "name": "Sub", "children": [url(11, "https://a.example/", 300)]},
            url(13, "https://c.example/", 400), url(14, "https://c.example/", 500)]},
        "synced": {"id": "4", "type": "folder", "name": "Mobile", "children": []}}}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tree, f)


class FirefoxTest(unittest.TestCase):
    def test_extra_copies_found_across_folders_oldest_kept(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "places.sqlite")
            _firefox_db(db)
            self.assertEqual(sorted(bookmarks.plan_firefox(db)),
                             [(11, "https://a.example/"), (14, "https://c.example/")])

    def test_apply_removes_only_the_planned_rows(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "places.sqlite")
            _firefox_db(db)
            bookmarks.apply_firefox(db, [11, 14])
            con = sqlite3.connect(db)
            left = [r[0] for r in con.execute("SELECT id FROM moz_bookmarks ORDER BY id")]
            con.close()
            self.assertEqual(left, [10, 12, 13, 15])
            con = sqlite3.connect(db)
            tomb = sorted(r[0] for r in con.execute("SELECT guid FROM moz_bookmarks_deleted"))
            con.close()
            self.assertEqual(tomb, ["g11", "g14"])  # every deletion is recorded so Sync uploads it


class BraveTest(unittest.TestCase):
    def test_extra_copies_found_across_folders_oldest_kept(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Bookmarks")
            _brave_json(p)
            self.assertEqual(sorted(bookmarks.plan_brave(p)),
                             [("11", "https://a.example/"), ("14", "https://c.example/")])

    def test_apply_removes_extras_keeps_the_rest_and_drops_the_stale_checksum(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Bookmarks")
            _brave_json(p)
            bookmarks.apply_brave(p, ["11", "14"])
            data = json.load(open(p))
            self.assertNotIn("checksum", data)
            remaining = sorted(node["id"] for node, _parent in bookmarks._brave_urls(data))
            self.assertEqual(remaining, ["10", "12", "13"])


class SafetyTest(unittest.TestCase):
    def test_refuses_to_apply_while_the_browser_is_open(self):
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(bookmarks, "firefox_profiles", return_value=[]), \
                mock.patch.object(bookmarks, "brave_profiles", return_value=[os.path.join(d, "Bookmarks")]), \
                mock.patch.object(bookmarks, "plan_brave", return_value=[("11", "https://a.example/")]), \
                mock.patch.object(bookmarks, "browser_running", return_value=True), \
                mock.patch.object(bookmarks, "backup") as backup:
            code = bookmarks.main(["--apply"])
        self.assertEqual(code, 2)
        backup.assert_not_called()

    def test_dry_run_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Bookmarks")
            _brave_json(p)
            before = open(p).read()
            with mock.patch.object(bookmarks, "firefox_profiles", return_value=[]), \
                    mock.patch.object(bookmarks, "brave_profiles", return_value=[p]):
                self.assertEqual(bookmarks.main([]), 0)
            self.assertEqual(open(p).read(), before)

    def test_backup_copies_each_file_before_a_change(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(bookmarks, "BACKUP_ROOT", d):
            src = os.path.join(d, "Bookmarks")
            with open(src, "w") as f:
                f.write("original")
            dest = bookmarks.backup([src])
            with open(os.path.join(dest, "0-Bookmarks")) as f:
                self.assertEqual(f.read(), "original")


if __name__ == "__main__":
    unittest.main()
