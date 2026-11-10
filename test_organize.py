import json
import os
import sqlite3
import tempfile
import unittest

import bookmarks
import organize
from test_bookmarks import _brave_json


def _firefox_db(path):
    """Same table layout as a real Firefox profile, with a few bookmarks in two folders."""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE moz_places (id INTEGER PRIMARY KEY, url TEXT);
        CREATE TABLE moz_bookmarks (id INTEGER PRIMARY KEY, type INTEGER, fk INTEGER DEFAULT NULL,
            parent INTEGER, position INTEGER, title LONGVARCHAR, keyword_id INTEGER, folder_type TEXT,
            dateAdded INTEGER, lastModified INTEGER, guid TEXT, syncStatus INTEGER NOT NULL DEFAULT 0,
            syncChangeCounter INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE moz_bookmarks_deleted (guid TEXT PRIMARY KEY, dateRemoved INTEGER);
        INSERT INTO moz_places VALUES (1, 'https://github.com/a'), (2, 'https://example.org/'),
                                      (3, 'https://github.com/b');
        INSERT INTO moz_bookmarks (id, type, fk, parent, position, title, dateAdded, lastModified, guid)
        VALUES (2, 2, NULL, 1, 0, 'menu', 0, 0, 'menu________'),
               (10, 1, 1, 2, 0, 'a', 100, 100, 'g10'), (11, 1, 2, 2, 1, 'e', 200, 200, 'g11'),
               (12, 1, 3, 2, 2, 'b', 300, 300, 'g12');
    """)
    con.commit()
    con.close()


class TopicTest(unittest.TestCase):
    def test_topics_and_websites(self):
        self.assertEqual(organize.topic_of("https://www.github.com/x/y"), "Dev & Tech")
        self.assertEqual(organize.topic_of("https://www.youtube.com/watch"), "Video")
        self.assertEqual(organize.topic_of("https://example.org/page"), "Other")
        self.assertEqual(organize.site_of("https://www.github.com/x"), "github.com")

    def test_plan_groups_topic_then_site(self):
        tree = organize.plan(["https://github.com/a", "https://github.com/b", "https://example.org/"])
        self.assertEqual(tree, {"Dev & Tech": {"github.com": ["https://github.com/a", "https://github.com/b"]},
                                "Other": {"example.org": ["https://example.org/"]}})


class OrganizeRunTest(unittest.TestCase):
    def test_run_without_apply_changes_nothing(self):
        import tempfile as tf
        with tf.TemporaryDirectory() as d:
            db = os.path.join(d, "places.sqlite")
            _firefox_db(db)
            before = open(db, "rb").read()
            from unittest import mock
            with mock.patch.object(organize, "firefox_profiles", return_value=[db]), \
                    mock.patch.object(organize, "brave_profiles", return_value=[]):
                lines = organize.run(apply=False)
            self.assertTrue(lines[-1].startswith("Dry run"))
            self.assertEqual(open(db, "rb").read(), before)


class FirefoxOrganizeTest(unittest.TestCase):
    def test_bookmarks_move_into_topic_and_site_folders_and_sync_is_marked(self):
        with tempfile.TemporaryDirectory() as d:
            db = os.path.join(d, "places.sqlite")
            _firefox_db(db)
            con = sqlite3.connect(db)
            con.execute("INSERT INTO moz_bookmarks (id, type, fk, parent, position, title, dateAdded, lastModified, guid) "
                        "VALUES (3, 2, NULL, 1, 1, 'toolbar', 0, 0, 'toolbar_____')")
            con.execute("INSERT INTO moz_bookmarks (id, type, fk, parent, position, title, dateAdded, lastModified, guid) "
                        "VALUES (30, 2, NULL, 3, 0, 'Old folder', 0, 0, 'oldfolder01')")  # ends up empty
            con.commit()
            con.close()
            _tree, removed = organize.apply_firefox(db)
            self.assertEqual(removed, 1)
            con = sqlite3.connect(db)
            rows = con.execute("""SELECT p.url, f.title, b.syncChangeCounter FROM moz_bookmarks b
                                  JOIN moz_places p ON p.id = b.fk JOIN moz_bookmarks f ON f.id = b.parent
                                  WHERE b.type = 1""").fetchall()
            con.close()
            self.assertEqual(sorted(r[1] for r in rows), ["example.org", "github.com", "github.com"])
            self.assertTrue(all(r[2] > 1 for r in rows))  # changed, so Sync uploads it
            con = sqlite3.connect(db)
            top = con.execute("""SELECT t.parent FROM moz_bookmarks t WHERE t.type = 2 AND t.title = '3D & AI'
                                 OR t.title = 'Other'""").fetchall()
            toolbar = con.execute("SELECT id FROM moz_bookmarks WHERE guid = 'toolbar_____'").fetchone()[0]
            self.assertTrue(all(p == toolbar for (p,) in top))  # topic folders sit on the toolbar
            self.assertEqual(con.execute("SELECT count(*) FROM moz_bookmarks WHERE guid = 'oldfolder01'").fetchone()[0], 0)
            self.assertEqual(con.execute("SELECT guid FROM moz_bookmarks_deleted").fetchall(), [("oldfolder01",)])
            con.close()


class BraveOrganizeTest(unittest.TestCase):
    def test_bookmarks_move_into_topic_and_site_folders(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "Bookmarks")
            _brave_json(p)
            organize.apply_brave(p)
            data = json.load(open(p))
            names = [c["name"] for c in data["roots"]["bookmark_bar"]["children"] if c["type"] == "folder"]
            self.assertIn("Other", names)
            urls = [node["url"] for node, _parent in bookmarks._brave_urls(data)]
            self.assertEqual(len(urls), 5)


if __name__ == "__main__":
    unittest.main()
