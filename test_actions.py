import csv
import json
import os
import tempfile
import unittest

from PIL import Image

import previews
import settings
from actions import export, keeper, trash_copies


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class KeeperTest(unittest.TestCase):
    def test_rules_pick_expected_copy(self):
        with tempfile.TemporaryDirectory() as root:
            old = _write(root, "a/old.txt", b"x")
            new = _write(root, "much/longer/new.txt", b"x")
            os.utime(old, (1_000_000, 1_000_000))
            os.utime(new, (2_000_000, 2_000_000))
            group = [old, new]
            self.assertEqual(keeper(group, "newest"), new)
            self.assertEqual(keeper(group, "oldest"), old)
            self.assertEqual(keeper(group, "shortest path"), old)


class TrashTest(unittest.TestCase):
    def test_never_trashes_last_copy_of_group(self):
        groups = [["/a/1", "/a/2", "/a/3"], ["/b/1", "/b/2"]]
        trashed_calls = []
        trashed, kept, failed = trash_copies(groups, {"/a/1", "/a/2", "/a/3", "/b/1"},
                                             trasher=trashed_calls.append)
        self.assertEqual(kept, ["/a/1"])  # all of group a selected: spare the first
        self.assertNotIn("/a/1", trashed_calls)
        self.assertEqual(sorted(trashed_calls), ["/a/2", "/a/3", "/b/1"])
        self.assertEqual(failed, [])

    def test_reports_files_the_system_refuses_to_trash(self):
        def refuse(path):
            raise OSError("permission denied")

        trashed, kept, failed = trash_copies([["/x/1", "/x/2"]], {"/x/2"}, trasher=refuse)
        self.assertEqual(trashed, [])
        self.assertEqual(failed, [("/x/2", "permission denied")])


class ExportTest(unittest.TestCase):
    def test_csv_and_json(self):
        with tempfile.TemporaryDirectory() as root:
            p1 = _write(root, "a.txt", b"12345")
            p2 = _write(root, "b.txt", b"12345")
            groups = [[p1, p2]]

            csv_path = os.path.join(root, "out.csv")
            export(groups, csv_path)
            with open(csv_path, newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(rows[0], ["group", "path", "size"])
            self.assertEqual(rows[1], ["1", p1, "5"])

            json_path = os.path.join(root, "out.json")
            export(groups, json_path)
            with open(json_path) as f:
                data = json.load(f)
            self.assertEqual(data[1], {"group": 1, "path": p2, "size": 5})


class SettingsTest(unittest.TestCase):
    def test_round_trip_and_missing_file_gives_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "sub", "settings.json")
            self.assertEqual(settings.load(path), settings.DEFAULTS)
            data = {**settings.DEFAULTS, "folder": "/data", "include_hidden": True}
            settings.save(data, path)
            self.assertEqual(settings.load(path), data)


class PreviewTest(unittest.TestCase):
    def test_text_binary_and_image(self):
        with tempfile.TemporaryDirectory() as root:
            t = _write(root, "n.txt", b"hello")
            b = _write(root, "n.bin", b"\0\1\2")
            i = os.path.join(root, "p.png")
            Image.new("RGB", (8, 8), (1, 2, 3)).save(i)
            self.assertEqual(previews.preview(t), ("text", "hello"))
            kind, text = previews.preview(b)
            self.assertEqual(kind, "text")
            self.assertTrue(text.startswith("Binary file, hex view:"))
            self.assertIn("00000000  00 01 02", text)
            kind, png = previews.preview(i)
            self.assertEqual(kind, "image")
            self.assertTrue(png.startswith(b"\x89PNG"))

    def test_pdf_renders_first_page(self):
        try:
            import pypdfium2  # noqa: F401
        except ImportError:
            self.skipTest("pypdfium2 not installed")
        with tempfile.TemporaryDirectory() as root:
            pdf = os.path.join(root, "doc.pdf")
            Image.new("RGB", (100, 100), (255, 255, 255)).save(pdf)
            kind, png = previews.preview(pdf)
            self.assertEqual(kind, "image")
            self.assertTrue(png.startswith(b"\x89PNG"))

    def test_corrupt_image_gives_info_not_crash(self):
        with tempfile.TemporaryDirectory() as root:
            bad = _write(root, "bad.png", b"not really a png")
            self.assertEqual(previews.preview(bad)[0], "info")


if __name__ == "__main__":
    unittest.main()
