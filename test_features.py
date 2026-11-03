import contextlib
import io
import itertools
import json
import os
import random
import subprocess
import tempfile
import threading
import time
import unittest
from unittest import mock

from PIL import Image

import actions
import cli
import previews
import report
import theme
from engine import Options, find_duplicates


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _pairs(sets):
    return {frozenset(p) for s in sets for p in itertools.combinations(s, 2)}


class ScanOptionsTest(unittest.TestCase):
    def test_skip_pattern_hides_matching_files(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a.tmp", b"same")
            _write(root, "b.tmp", b"same")
            _write(root, "c.txt", b"same")
            _write(root, "d.txt", b"same")
            res = find_duplicates(root, Options(exclude_patterns=frozenset({"*.tmp"})))
            self.assertEqual([sorted(os.path.basename(p) for p in g) for g in res.groups], [["c.txt", "d.txt"]])

    def test_max_depth_limits_subfolder_levels(self):
        with tempfile.TemporaryDirectory() as root:
            for rel in ("top1.bin", "a/one.bin", "a/b/two.bin"):
                _write(root, rel, b"dup")
            names = {os.path.basename(p) for g in find_duplicates(root, Options(max_depth=1)).groups for p in g}
            self.assertEqual(names, {"top1.bin", "one.bin"})

    def test_pause_holds_work_until_resumed(self):
        with tempfile.TemporaryDirectory() as root:
            for i in range(4):
                _write(root, f"f{i}.bin", b"same")
            pause = threading.Event()
            pause.set()
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("res", find_duplicates(root, pause=pause)))
            t.start()
            t.join(0.5)
            self.assertTrue(t.is_alive(), "scan should wait while paused")
            pause.clear()
            t.join(10)
            self.assertFalse(t.is_alive())
            self.assertEqual(len(out["res"].groups), 1)

    def test_cancel_releases_a_paused_scan(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", b"x")
            cancel, pause = threading.Event(), threading.Event()
            pause.set()
            out = {}
            t = threading.Thread(target=lambda: out.setdefault("res", find_duplicates(root, cancel=cancel, pause=pause)))
            t.start()
            time.sleep(0.2)
            cancel.set()
            pause.clear()
            t.join(10)
            self.assertTrue(out["res"].cancelled)


class NameClashTest(unittest.TestCase):
    def test_same_name_different_content_is_reported_but_copies_are_not(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "x/notes.txt", b"version one of the notes")
            _write(root, "y/notes.txt", b"a different version of notes")
            _write(root, "x/photo.jpg", b"same picture")
            _write(root, "y/photo.jpg", b"same picture")
            res = find_duplicates(root)
            self.assertEqual(len(res.name_clashes), 1)
            self.assertTrue(all(os.path.basename(p) == "notes.txt" for p in res.name_clashes[0]))


class DocumentSimilarityTest(unittest.TestCase):
    def test_edited_copies_match_and_unrelated_documents_do_not(self):
        rng = random.Random(3)
        vocab = ["w" + "".join(rng.choice("abcdefghijklmnop") for _ in range(6)) for _ in range(3000)]
        with tempfile.TemporaryDirectory() as root:
            truth = []
            for i in range(15):
                words = [rng.choice(vocab) for _ in range(400)]
                original = _write(root, f"doc{i}.txt", " ".join(words).encode())
                edited = list(words)
                for _ in range(3):  # change a few words, as in a lightly edited copy
                    edited[rng.randrange(len(edited))] = rng.choice(vocab)
                copy = _write(root, f"edit{i}.md", " ".join(edited).encode())
                truth.append([original, copy])
            res = find_duplicates(root, Options(mode="documents", threshold=80))
            truth_pairs, found = _pairs(truth), _pairs(res.groups)
            true_pos = len(truth_pairs & found)
            precision, recall = true_pos / len(found), true_pos / len(truth_pairs)
            print(f"\ndocuments: precision={precision:.3f} recall={recall:.3f} pairs={len(truth_pairs)}")
            self.assertGreaterEqual(precision, 0.9)
            self.assertGreaterEqual(recall, 0.9)


class VideoSimilarityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import imageio_ffmpeg
            exe = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise unittest.SkipTest("needs imageio-ffmpeg to generate test clips")
        cls.exe = exe
        cls.bin_dir = tempfile.mkdtemp()
        os.symlink(exe, os.path.join(cls.bin_dir, "ffmpeg"))

    def _run(self, *args):
        subprocess.run([self.exe, "-v", "error", "-y", *args], check=True, capture_output=True)

    def test_reencoded_copy_matches_and_different_content_does_not(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
                os.environ, {"PATH": self.bin_dir + os.pathsep + os.environ["PATH"]}):
            a = os.path.join(root, "a.mp4")
            copy = os.path.join(root, "copy.mkv")
            other = os.path.join(root, "other.mp4")
            self._run("-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=10", "-pix_fmt", "yuv420p", a)
            self._run("-i", a, "-vf", "scale=160:120", "-c:v", "mpeg4", copy)
            self._run("-f", "lavfi", "-i", "mandelbrot=size=320x240:rate=10", "-t", "8", "-pix_fmt", "yuv420p", other)
            res = find_duplicates(root, Options(mode="videos", threshold=90))
            self.assertEqual(res.errors, [])
            self.assertEqual(_pairs(res.groups), {frozenset([a, copy])})


class ActionsTest(unittest.TestCase):
    def test_hardlink_replaces_copy_with_link_to_kept_file(self):
        with tempfile.TemporaryDirectory() as root:
            keep = _write(root, "keep.bin", b"payload")
            extra = _write(root, "extra.bin", b"payload")
            linked, failed = actions.hardlink_copies([[keep, extra]], {extra})
            self.assertEqual((linked, failed), ([extra], []))
            self.assertEqual(os.stat(keep).st_ino, os.stat(extra).st_ino)
            with open(extra, "rb") as f:
                self.assertEqual(f.read(), b"payload")

    def test_hardlink_never_touches_a_group_with_every_copy_selected(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a", b"x")
            b = _write(root, "b", b"x")
            self.assertEqual(actions.hardlink_copies([[a, b]], {a, b}), ([], []))
            self.assertNotEqual(os.stat(a).st_ino, os.stat(b).st_ino)

    def test_move_then_undo_restores_files(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "src/a.txt", b"one")
            b = _write(root, "src/b.txt", b"one")
            dest, undo = os.path.join(root, "dest"), os.path.join(root, "undo.json")
            moved, kept, failed = actions.move_copies([[a, b]], {b}, dest, undo)
            self.assertEqual((moved, failed), ([b], []))
            self.assertFalse(os.path.exists(b))
            self.assertTrue(os.path.exists(os.path.join(dest, "b.txt")))
            restored, failed = actions.undo_moves(undo)
            self.assertEqual((restored, failed), ([b], []))
            self.assertTrue(os.path.exists(b))

    def test_move_avoids_overwriting_a_name_clash_in_destination(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "src/a.txt", b"one")
            b = _write(root, "src/b/a.txt", b"one")
            _write(root, "dest/a.txt", b"already here")
            dest, undo = os.path.join(root, "dest"), os.path.join(root, "undo.json")
            actions.move_copies([[a, b]], {b}, dest, undo)
            self.assertTrue(os.path.exists(os.path.join(dest, "a (1).txt")))
            with open(os.path.join(dest, "a.txt"), "rb") as f:
                self.assertEqual(f.read(), b"already here")

    def test_save_and_load_results_drop_missing_files(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a", b"x")
            b = _write(root, "b", b"x")
            path = os.path.join(root, "r.json")
            actions.save_results(path, [[a, b]], "exact")
            os.remove(b)
            groups, missing, mode = actions.load_results(path)
            self.assertEqual((groups, missing, mode), ([], 1, "exact"))


class ReportAndCliTest(unittest.TestCase):
    def test_html_report_lists_and_escapes_paths(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "<b>x</b>.txt", b"same")
            b = _write(root, "y.txt", b"same")
            out = os.path.join(root, "r.html")
            report.write_html([[a, b]], out)
            with open(out, encoding="utf-8") as f:
                page = f.read()
            self.assertIn("Group 1: 2 copies", page)
            self.assertIn("&lt;b&gt;x&lt;/b&gt;.txt", page)
            self.assertNotIn("<b>x</b>", page)

    def test_cli_prints_json_groups(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a.txt", b"same")
            b = _write(root, "b.txt", b"same")
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                code = cli.main([root, "--json"])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(buf.getvalue())["groups"], [sorted([a, b])])


class PreviewAndThemeTest(unittest.TestCase):
    def test_image_info_reports_dimensions(self):
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "p.png")
            Image.new("RGB", (40, 30)).save(p)
            self.assertIn("Dimensions: 40 x 30", previews.image_info(p))

    def test_hexdump_format(self):
        self.assertEqual(previews.hexdump(b"AB"), "00000000  " + "41 42".ljust(47) + "  AB")

    def test_palettes(self):
        self.assertIs(theme.palette("dark"), theme.DARK)
        self.assertIs(theme.palette("light"), theme.LIGHT)


if __name__ == "__main__":
    unittest.main()
