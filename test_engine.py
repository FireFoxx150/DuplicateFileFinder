import itertools
import os
import random
import tempfile
import threading
import unittest

from PIL import Image

from engine import Options, compare_folders, find_duplicates


def _pairs(sets):
    return {frozenset(p) for s in sets for p in itertools.combinations(s, 2)}


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


def _build_tree(root):
    """Write a tree with known duplicate sets, plus near-misses that must NOT match."""
    rng = random.Random(42)
    truth = []
    for i in range(30):
        data = rng.randbytes(rng.randint(1, 300_000))
        truth.append([_write(root, f"d{i % 5}/s{j}/f{i}_{j}.bin", data) for j in range(rng.randint(2, 4))])

    # Same size, identical first 4 KB, differ only in the last byte.
    base = rng.randbytes(20_000)
    near = bytearray(base)
    near[-1] ^= 1
    _write(root, "near/a.bin", base)
    _write(root, "near/b.bin", bytes(near))

    # Same size, differ at byte 0.
    head = bytearray(base)
    head[0] ^= 1
    _write(root, "near/c.bin", bytes(head))

    for i in range(20):
        _write(root, f"uniq/u{i}.bin", rng.randbytes(rng.randint(1, 50_000)))

    _write(root, "empty/a", b"")
    _write(root, "empty/b", b"")
    return truth


class ExactDuplicatesTest(unittest.TestCase):
    def test_precision_and_recall_at_least_90_percent(self):
        with tempfile.TemporaryDirectory() as root:
            truth = _pairs(_build_tree(root))
            res = find_duplicates(root)
            found = _pairs(res.groups)

            true_pos = len(truth & found)
            precision = true_pos / len(found)
            recall = true_pos / len(truth)
            print(f"\nexact: precision={precision:.3f} recall={recall:.3f} pairs={len(truth)}")

            self.assertEqual(res.errors, [])
            self.assertGreaterEqual(precision, 0.9)
            self.assertGreaterEqual(recall, 0.9)

    def test_near_misses_are_not_grouped(self):
        with tempfile.TemporaryDirectory() as root:
            _build_tree(root)
            for paths in find_duplicates(root).groups:
                self.assertFalse(any("near" in p for p in paths), paths)

    def test_filters_skip_hidden_excluded_and_other_extensions(self):
        with tempfile.TemporaryDirectory() as root:
            data = b"same bytes"
            _write(root, "keep/a.txt", data)
            _write(root, "keep/b.txt", data)
            _write(root, "skip/c.txt", data)
            _write(root, "keep/.hidden.txt", data)
            _write(root, "keep/d.log", data)
            opts = Options(exclude_patterns=frozenset({"skip"}), extensions=frozenset({".txt"}))
            groups = find_duplicates(root, opts).groups
            self.assertEqual([sorted(os.path.basename(p) for p in g) for g in groups], [["a.txt", "b.txt"]])

            with_hidden = find_duplicates(root, Options(include_hidden=True, extensions=frozenset({".txt"}))).groups
            self.assertIn(".hidden.txt", [os.path.basename(p) for g in with_hidden for p in g])

    def test_min_size_skips_small_files(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a", b"x" * 10)
            _write(root, "b", b"x" * 10)
            self.assertEqual(find_duplicates(root, Options(min_size=11)).groups, [])
            self.assertEqual(len(find_duplicates(root, Options(min_size=10)).groups), 1)

    def test_hard_links_count_once(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a.txt", b"data")
            os.link(a, os.path.join(root, "b.txt"))
            self.assertEqual(find_duplicates(root).groups, [])

    def test_cancel_returns_cancelled(self):
        with tempfile.TemporaryDirectory() as root:
            _build_tree(root)
            cancel = threading.Event()
            cancel.set()
            self.assertTrue(find_duplicates(root, cancel=cancel).cancelled)

    def test_progress_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            _build_tree(root)
            calls = []
            find_duplicates(root, progress=lambda label, done, total: calls.append((done, total)))
            self.assertTrue(calls)
            self.assertLessEqual(calls[-1][0], calls[-1][1])

    def test_hash_cache_gives_same_groups(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as cache_dir:
            _build_tree(root)
            cache = os.path.join(cache_dir, "hashes.db")
            first = find_duplicates(root, cache_path=cache).groups
            second = find_duplicates(root, cache_path=cache).groups
            self.assertEqual(first, second)
            self.assertEqual(first, find_duplicates(root).groups)


class SimilarImagesTest(unittest.TestCase):
    def test_resized_and_recompressed_copies_match_distinct_images_do_not(self):
        rng = random.Random(7)
        with tempfile.TemporaryDirectory() as root:
            truth = []
            for i in range(20):
                base = Image.new("L", (128, 128))
                base.putdata([rng.randrange(256) for _ in range(128 * 128)])
                original = os.path.join(root, f"img{i}.png")
                base.save(original)
                copy = os.path.join(root, f"copy{i}.jpg")
                base.resize((64, 64)).save(copy, quality=50)
                truth.append([original, copy])

            res = find_duplicates(root, Options(mode="images", threshold=16))
            truth_pairs = _pairs(truth)
            found = _pairs(res.groups)
            true_pos = len(truth_pairs & found)
            precision = true_pos / len(found)
            recall = true_pos / len(truth_pairs)
            print(f"\nsimilar: precision={precision:.3f} recall={recall:.3f} pairs={len(truth_pairs)}")

            self.assertGreaterEqual(precision, 0.9)
            self.assertGreaterEqual(recall, 0.9)


class CompareFoldersTest(unittest.TestCase):
    def test_shared_copies_and_unmatched_files(self):
        with tempfile.TemporaryDirectory() as root:
            a, b = os.path.join(root, "A"), os.path.join(root, "B")
            _write(a, "shared.txt", b"hello")
            _write(b, "elsewhere/copy.txt", b"hello")
            _write(a, "only_a.txt", b"unique to A")
            res = compare_folders(a, b)

            self.assertEqual(len(res.groups), 1)
            self.assertTrue(any(p.startswith(b) for p in res.groups[0]))
            self.assertEqual([os.path.basename(p) for p in res.unmatched], ["only_a.txt"])


if __name__ == "__main__":
    unittest.main()
