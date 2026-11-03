import itertools
import math
import os
import random
import struct
import subprocess
import tempfile
import unittest
import wave
from unittest import mock

import actions
import analysis
import schedule
import trash
from engine import Options, find_duplicates
from i18n import _
from previews import exif_summary


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _pairs(sets):
    return {frozenset(p) for s in sets for p in itertools.combinations(s, 2)}


class VerifyBytesTest(unittest.TestCase):
    def test_same_bytes_still_grouped_with_verification(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a.bin", b"same content")
            _write(root, "b.bin", b"same content")
            _write(root, "c.bin", b"other content")
            res = find_duplicates(root, Options(verify_bytes=True))
            self.assertEqual([sorted(os.path.basename(p) for p in g) for g in res.groups], [["a.bin", "b.bin"]])


class KeepRuleTest(unittest.TestCase):
    def test_prefer_folder_keeps_copy_inside_it(self):
        with tempfile.TemporaryDirectory() as root:
            master = _write(root, "Master/pic.jpg", b"x")
            other = _write(root, "Downloads/pic.jpg", b"x")
            os.utime(other, (9_999_999_999, 9_999_999_999))  # newer, but outside the master folder
            self.assertEqual(actions.keeper([master, other], "prefer folder", os.path.join(root, "Master")), master)

    def test_prefer_folder_without_match_falls_back_to_newest(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "x/a", b"x")
            b = _write(root, "y/b", b"x")
            os.utime(a, (1, 1))
            os.utime(b, (2, 2))
            self.assertEqual(actions.keeper([a, b], "prefer folder", os.path.join(root, "Master")), b)


class IgnoreAndActionsTest(unittest.TestCase):
    def test_ignored_groups_are_removed_and_persist(self):
        with tempfile.TemporaryDirectory() as root:
            groups = [["/a/1", "/a/2"], ["/b/1", "/b/2"]]
            path = os.path.join(root, "ignored.json")
            actions.save_ignored(path, {frozenset(groups[0])})
            ignored = actions.load_ignored(path)
            self.assertEqual(actions.without_ignored(groups, ignored), [groups[1]])

    def test_custom_action_gets_path_without_shell(self):
        with tempfile.TemporaryDirectory() as root:
            p = _write(root, "my file.txt", b"hi")
            code, out = actions.run_action("echo {path}", p)
            self.assertEqual(code, 0)
            self.assertEqual(out, p)


class TrashRestoreTest(unittest.TestCase):
    def test_trashed_file_comes_back_to_its_place(self):
        # Build a trash folder the way the desktop does, inside a temp dir: never the real trash.
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"XDG_DATA_HOME": root}):
            original = os.path.join(root, "docs", "report.txt")
            trash_dir = os.path.join(root, "Trash")
            os.makedirs(os.path.join(trash_dir, "files"))
            os.makedirs(os.path.join(trash_dir, "info"))
            with open(os.path.join(trash_dir, "files", "report.txt"), "wb") as f:
                f.write(b"important")
            with open(os.path.join(trash_dir, "info", "report.txt.trashinfo"), "w") as f:
                f.write(f"[Trash Info]\nPath={original}\nDeletionDate=2026-10-19T10:00:00\n")
            self.assertTrue(trash.restore(original, mounts=[]))
            with open(original, "rb") as f:
                self.assertEqual(f.read(), b"important")
            self.assertFalse(os.path.exists(os.path.join(trash_dir, "info", "report.txt.trashinfo")))

    def test_nothing_to_restore_returns_false(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(os.environ, {"XDG_DATA_HOME": root}):
            self.assertFalse(trash.restore(os.path.join(root, "never-trashed.txt"), mounts=[]))


class AnalysisTest(unittest.TestCase):
    def test_space_by_type_and_folder(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "one/x.jpg", b"12345")
            b = _write(root, "two/x.jpg", b"12345")
            c = _write(root, "one/y.txt", b"1234567890")
            d = _write(root, "one/z.txt", b"1234567890")
            groups = [[a, b], [c, d]]
            self.assertEqual(analysis.space_by_type(groups), [(".txt", 10), (".jpg", 5)])
            self.assertEqual(analysis.reclaimable_by_folder(groups, root), [("one", 10), ("two", 5)])

    def test_treemap_fills_the_area_in_proportion(self):
        rects = analysis.treemap([("a", 3), ("b", 1)], 0, 0, 400, 200)
        self.assertAlmostEqual(sum(w * h for _l, _v, _x, _y, w, h in rects), 400 * 200, places=6)
        areas = {label: w * h for label, _v, _x, _y, w, h in rects}
        self.assertAlmostEqual(areas["a"] / areas["b"], 3.0, places=6)


class DrillDownTest(unittest.TestCase):
    def test_subfolder_view_counts_only_copies_inside_it(self):
        with tempfile.TemporaryDirectory() as root:
            keep = _write(root, "photos/2020/a.jpg", b"12345")
            here = _write(root, "photos/2020/b.jpg", b"12345")
            deeper = _write(root, "photos/2020/trips/c.jpg", b"12345")
            elsewhere = _write(root, "photos/2021/d.jpg", b"12345")
            groups = [[keep, here, deeper, elsewhere]]
            base = os.path.join(root, "photos", "2020")
            self.assertEqual(analysis.reclaimable_by_folder(groups, base),
                             [("(this folder)", 5), ("trips", 5)])

    def test_is_file_tells_files_from_group_and_month_rows(self):
        from gui import _is_file
        self.assertTrue(_is_file("/home/me/pic.jpg"))
        self.assertFalse(_is_file("g3"))
        self.assertFalse(_is_file("m:2024-05"))


class ScheduleTest(unittest.TestCase):
    def test_units_call_cli_and_repeat_on_schedule(self):
        service, timer = schedule.unit_text("/data/My Photos", 12, "/usr/bin/dupefinder-cli", "/r/latest.html")
        self.assertIn('ExecStart="/usr/bin/dupefinder-cli" "/data/My Photos" --json --notify', service)
        self.assertIn("OnUnitActiveSec=12h", timer)
        self.assertIn("WantedBy=timers.target", timer)

    def test_percent_signs_are_escaped_for_systemd(self):
        service, _timer = schedule.unit_text("/data/100%", 1, "/bin/cli", "/r.html")
        self.assertIn("/data/100%%", service)

    def test_write_units_creates_both_files(self):
        with tempfile.TemporaryDirectory() as root:
            paths = schedule.write_units("/data", 6, "/bin/cli", "/r.html", out_dir=root)
            self.assertEqual(sorted(os.path.basename(p) for p in paths), sorted([schedule.SERVICE, schedule.TIMER]))
            for p in paths:
                self.assertTrue(os.path.getsize(p) > 0)


class ExifTest(unittest.TestCase):
    def test_picture_without_exif_gives_empty_details(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as root:
            p = os.path.join(root, "plain.png")
            Image.new("RGB", (8, 8)).save(p)
            self.assertEqual(exif_summary(p), ("", ""))


class TranslationTest(unittest.TestCase):
    def test_strings_fall_back_to_english_without_a_translation(self):
        self.assertEqual(_("Scan"), "Scan")


# ----- audio similarity -----------------------------------------------------

def _melody(seed, notes=40, sr=44100):
    rng = random.Random(seed)
    freqs = [440 * 2 ** (rng.randrange(0, 24) / 12) for _ in range(notes)]
    samples = []
    for f in freqs:
        samples += [int(8000 * math.sin(2 * math.pi * f * i / sr)) for i in range(int(sr * 0.5))]
    return samples, sr


class AudioSimilarityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            import imageio_ffmpeg
            cls.exe = imageio_ffmpeg.get_ffmpeg_exe()
        except ImportError:
            raise unittest.SkipTest("needs imageio-ffmpeg to encode test audio")
        cls.bin_dir = tempfile.mkdtemp()
        os.symlink(cls.exe, os.path.join(cls.bin_dir, "ffmpeg"))

    def test_re_encoded_copies_match_and_other_tunes_do_not(self):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
                os.environ, {"PATH": self.bin_dir + os.pathsep + os.environ["PATH"]}):
            truth = []
            for seed in range(8):
                samples, sr = _melody(seed)
                wav = os.path.join(root, f"tune{seed}.wav")
                with wave.open(wav, "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(sr)
                    w.writeframes(b"".join(struct.pack("<h", v) for v in samples))
                mp3 = os.path.join(root, f"tune{seed}.mp3")
                m4a = os.path.join(root, f"tune{seed}.m4a")
                subprocess.run([self.exe, "-v", "error", "-y", "-i", wav, "-b:a", "128k", mp3], check=True)
                subprocess.run([self.exe, "-v", "error", "-y", "-i", wav, "-b:a", "96k", m4a], check=True)
                truth.append([wav, mp3, m4a])
            res = find_duplicates(root, Options(mode="audio", threshold=75))
            self.assertEqual(res.errors, [])
            truth_pairs = _pairs(truth)
            found = _pairs(res.groups)
            true_pos = len(truth_pairs & found)
            precision, recall = true_pos / len(found), true_pos / len(truth_pairs)
            print(f"\naudio: precision={precision:.3f} recall={recall:.3f} pairs={len(truth_pairs)}")
            self.assertGreaterEqual(precision, 0.9)
            self.assertGreaterEqual(recall, 0.9)


if __name__ == "__main__":
    unittest.main()
