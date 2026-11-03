"""Find duplicate files: exact copies, similar images/documents/videos, or copies across two folders.

Exact matching narrows files in stages so most are never read in full:
size -> BLAKE2b of first 4 KB -> BLAKE2b of whole file (optionally cached).
Equal full hashes mean identical bytes, so exact groups have no false positives.
"""
import fnmatch
import filecmp
import hashlib
import os
import shutil
import sqlite3
import statistics
import stat
import subprocess
import threading
import time
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

import numpy as np
from PIL import Image

import audio

PARTIAL_BYTES = 4096
CHUNK = 1 << 20
WORKERS = os.cpu_count() or 4  # files read and hashed at once
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
# Containers ffmpeg can decode, which covers what VLC plays for files on disk.
VIDEO_EXTS = {".mp4", ".m4v", ".mkv", ".mov", ".avi", ".webm", ".flv", ".f4v", ".wmv", ".asf",
              ".mpg", ".mpeg", ".m2v", ".ts", ".mts", ".m2ts", ".vob", ".3gp", ".3g2", ".ogv",
              ".divx", ".mxf", ".rm", ".rmvb", ".dv", ".nut"}
TEXT_EXTS = {".txt", ".md", ".rst", ".csv", ".log", ".json", ".xml", ".html", ".htm", ".py", ".c",
             ".h", ".cpp", ".java", ".js", ".ts", ".go", ".rs", ".sh", ".yaml", ".yml", ".ini",
             ".cfg", ".tex"}
AUDIO_EXTS = {".mp3", ".flac", ".ogg", ".m4a", ".wav", ".opus", ".aac", ".wma"}
MODE_EXTS = {"images": IMAGE_EXTS, "documents": TEXT_EXTS, "videos": VIDEO_EXTS, "audio": AUDIO_EXTS}
SKIP_PRESET = ["node_modules", "__pycache__", ".cache", ".git", ".venv", "build", "dist", "*.tmp"]
SHINGLE = 5          # words per shingle when comparing documents
TEXT_BYTES = 200_000  # how much of each document to read
VIDEO_FRAMES = 40    # frames sampled (one every two seconds, up to 80 s) when comparing videos
VIDEO_CROPS = (1.0, 0.95, 0.9, 0.85, 0.8)  # centre crops of each frame, as a share of its size
VIDEO_FRAME_CORR = 0.96  # two frames count as the same when their pixels correlate at least this much


class Cancelled(Exception):
    pass


@dataclass
class Options:
    min_size: int = 1
    include_hidden: bool = False
    exclude_patterns: frozenset = frozenset()  # fnmatch globs for file and folder names
    max_depth: int | None = None  # subfolder levels to descend; None = all
    extensions: frozenset | None = None  # lowercase with dot, e.g. {".jpg"}; None = all
    mode: str = "exact"  # exact | images | documents | videos
    # images: largest Hamming distance (0-256) between 256-bit hashes; videos: 0-64
    # documents: smallest word-shingle similarity in percent (0-100)
    threshold: float = 5
    verify_bytes: bool = False  # compare file bytes after hashing, for an extra safety check
    max_size: int | None = None  # skip files larger than this many bytes
    modified_after: float | None = None  # epoch seconds; skip files older than this
    modified_before: float | None = None  # epoch seconds; skip files newer than this
    respect_dupeignore: bool = True  # honour .dupeignore files (one glob per line) in each folder


@dataclass
class Result:
    groups: list = field(default_factory=list)  # each group is a list of paths
    unmatched: list = field(default_factory=list)  # compare mode: files with no copy in B
    name_clashes: list = field(default_factory=list)  # same file name, different content
    errors: list = field(default_factory=list)
    cancelled: bool = False


class HashCache:
    """Full-file hashes keyed by path, size and mtime, so unchanged files are not re-read."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        # Hashing runs on several threads, so the connection is shared and every use is locked.
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.lock = threading.Lock()
        self.db.execute("CREATE TABLE IF NOT EXISTS hashes (path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER, sha TEXT)")

    def get(self, path, size, mtime):
        with self.lock:
            row = self.db.execute("SELECT sha FROM hashes WHERE path=? AND size=? AND mtime=?",
                                  (path, size, mtime)).fetchone()
        return row[0] if row else None

    def put(self, path, size, mtime, sha):
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO hashes VALUES (?,?,?,?)", (path, size, mtime, sha))

    def close(self):
        self.db.commit()
        self.db.close()


class _Control:
    """Cancel and pause shared by every stage. While paused, work waits instead of moving on."""

    def __init__(self, cancel, pause):
        self.cancel, self.pause = cancel, pause

    def check(self):
        while self.pause and self.pause.is_set() and not (self.cancel and self.cancel.is_set()):
            time.sleep(0.1)
        if self.cancel and self.cancel.is_set():
            raise Cancelled()


class _Work:
    def __init__(self, total, progress, ctl):
        self.total, self.done, self.progress, self.ctl = total, 0, progress, ctl

    def tick(self, label):
        self.ctl.check()
        self.done += 1
        if self.progress:
            self.progress(label, self.done, self.total)


def _digest(path, partial):
    h = hashlib.blake2b()
    with open(path, "rb") as f:
        if partial:
            h.update(f.read(PARTIAL_BYTES))
        else:
            for chunk in iter(lambda: f.read(CHUNK), b""):
                h.update(chunk)
    return h.hexdigest()


def _full_hash(path, mtime, size, cache):
    if cache:
        hit = cache.get(path, size, mtime)
        if hit:
            return hit
    sha = _digest(path, False)
    if cache:
        cache.put(path, size, mtime, sha)
    return sha


def _bits(px, width=9, height=8):
    """Difference hash from grayscale bytes: each bit says whether a pixel is brighter than its right neighbour."""
    bits = 0
    for row in range(height):
        for col in range(width - 1):
            bits = (bits << 1) | (px[row * width + col] > px[row * width + col + 1])
    return bits


def _dhash(path):
    """Hashes of an image rotated 0/90/180/270 degrees, each also centre-cropped to 100/95/90/85/80%.

    256-bit hashes on a 17x16 grid. A copy that was rotated, cropped or recompressed matches one of these,
    so the best match over all combinations is kept (see _is_similar).
    """
    out = []
    with Image.open(path) as im0:
        im = im0.convert("L")
        for rot in (0, 90, 180, 270):
            r = im.rotate(rot, expand=True) if rot else im
            w, h = r.size
            for keep in (1.0, 0.95, 0.9, 0.85, 0.8):
                dx, dy = int(w * (1 - keep) / 2), int(h * (1 - keep) / 2)
                c = r.crop((dx, dy, w - dx, h - dy)) if keep < 1 else r
                out.append(_bits(c.resize((17, 16), Image.LANCZOS).tobytes(), 17, 16))
    return out


def _video_frames(path):
    exe = shutil.which("ffmpeg")
    if not exe:
        raise OSError("ffmpeg is needed for video similarity")
    proc = subprocess.run(
        [exe, "-v", "error", "-t", "90", "-i", path, "-vf", "fps=1/2,scale=68:64,format=gray",
         "-frames:v", str(VIDEO_FRAMES), "-f", "rawvideo", "-"],
        capture_output=True, timeout=200,
    )
    if proc.returncode != 0:
        raise OSError("ffmpeg could not read this video")
    raw, frames = proc.stdout, []
    for i in range(0, len(raw) - 68 * 64 + 1, 68 * 64):
        px = raw[i:i + 68 * 64]
        if statistics.pstdev(px) >= 10:  # skip black, blank and title frames: they match across unrelated videos
            frames.append(_crop_vectors(px))
    return np.array(frames, dtype=np.float32).reshape(-1, len(VIDEO_CROPS), 32 * 32)  # empty when no usable frame


def _crop_vectors(px):
    """Each frame's centre crops (100% down to 80%), as mean-free unit vectors, so crops still correlate."""
    im = Image.frombytes("L", (68, 64), px)
    out = []
    for keep in VIDEO_CROPS:
        dx, dy = int(68 * (1 - keep) / 2), int(64 * (1 - keep) / 2)
        c = im.crop((dx, dy, 68 - dx, 64 - dy)) if keep < 1 else im
        v = np.asarray(c.resize((32, 32), Image.BILINEAR), dtype=np.float32).ravel()
        v -= v.mean()
        n = np.linalg.norm(v)
        out.append(v / n if n else v)
    return np.array(out)


def _shingles(path):
    with open(path, "rb") as f:
        words = f.read(TEXT_BYTES).decode("utf-8", errors="ignore").lower().split()
    return frozenset(zlib.crc32(" ".join(words[i:i + SHINGLE]).encode())
                     for i in range(max(0, len(words) - SHINGLE + 1)))


def _signature(path, mode):
    if mode == "images":
        return _dhash(path)
    if mode == "documents":
        return _shingles(path)
    if mode == "audio":
        return audio.fingerprint(path)
    return _video_frames(path)


def _is_empty(sig):
    return sig is None or (hasattr(sig, "__len__") and len(sig) == 0)


def _popcount(x):
    return bin(x).count("1")


def _is_similar(mode, threshold):
    if mode == "images":  # shortcut: compares every pair of hashes, so slow above a few thousand images
        return lambda a, b: min(_popcount(x ^ y) for x in a for y in b) <= threshold
    if mode == "documents":
        return lambda a, b: bool(a | b) and 100 * len(a & b) / len(a | b) >= threshold
    if mode == "audio":
        return lambda a, b: audio.agreement(a, b) >= threshold
    # videos: share of the frames both clips have in common (in percent) that correlate with a crop of the other
    return lambda a, b: _frames_match(a, b) >= threshold


def _frames_match(a, b):
    n = min(len(a), len(b))
    if n == 0:
        return 0.0
    ab = np.einsum("kd,ked->ke", a[:n, 0], b[:n]).max(axis=1)  # a's full frame against b's crops
    ba = np.einsum("ked,kd->ke", a[:n], b[:n, 0]).max(axis=1)  # and the other way round
    return 100.0 * float(np.mean(np.maximum(ab, ba) >= VIDEO_FRAME_CORR))


def _dupeignore_patterns(folder):
    """Glob patterns from a .dupeignore file in folder: one per line, blank lines and # comments skipped."""
    try:
        with open(os.path.join(folder, ".dupeignore")) as f:
            return [line.strip() for line in f if line.strip() and not line.startswith("#")]
    except OSError:
        return []


def _keep_name(name, opts):
    if not opts.include_hidden and name.startswith("."):
        return False
    return not any(fnmatch.fnmatch(name, pat) for pat in opts.exclude_patterns)


def _wanted_exts(opts):
    base = MODE_EXTS.get(opts.mode)
    if base is None:
        return opts.extensions
    return base if opts.extensions is None else opts.extensions & base


def _collect(root, opts, errors, ctl):
    """Map file size -> [(path, mtime_ns)] for every regular file that passes the filters."""
    by_size = defaultdict(list)
    seen = set()
    wanted = _wanted_exts(opts)
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: errors.append(str(e))):
        ctl.check()
        rel = os.path.relpath(dirpath, root)
        depth = 0 if rel == "." else rel.count(os.sep) + 1
        dirnames[:] = [d for d in dirnames if _keep_name(d, opts)]
        if opts.max_depth is not None and depth >= opts.max_depth:
            dirnames[:] = []
        local = _dupeignore_patterns(dirpath) if opts.respect_dupeignore else []
        dirnames[:] = [d for d in dirnames if not any(fnmatch.fnmatch(d, pat) for pat in local)]
        for name in filenames:
            if not _keep_name(name, opts) or any(fnmatch.fnmatch(name, pat) for pat in local):
                continue
            if wanted is not None and os.path.splitext(name)[1].lower() not in wanted:
                continue
            path = os.path.join(dirpath, name)
            try:
                st = os.lstat(path)
            except OSError as e:
                errors.append(f"{path}: {e}")
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size < opts.min_size:
                continue
            if opts.max_size is not None and st.st_size > opts.max_size:
                continue
            if opts.modified_after is not None and st.st_mtime < opts.modified_after:
                continue
            if opts.modified_before is not None and st.st_mtime > opts.modified_before:
                continue
            key = (st.st_dev, st.st_ino)  # hard links share data; count once per folder
            if key in seen:
                continue
            seen.add(key)
            by_size[st.st_size].append((path, st.st_mtime_ns))
    return by_size


def _bucket(items, key, work, errors):
    """Group items by key(path, mtime), reading several files at once on multi-core machines."""
    buckets = defaultdict(list)
    pool = ThreadPoolExecutor(max_workers=WORKERS)
    try:
        futures = {pool.submit(key, *item): item for item in items}
        for fut in as_completed(futures):
            work.tick("Hashing files")
            try:
                buckets[fut.result()].append(futures[fut])
            except OSError as e:
                errors.append(f"{futures[fut][0]}: {e}")
    except Cancelled:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    finally:
        pool.shutdown(wait=True)
    return [b for b in buckets.values() if len(b) > 1]


def _exact_groups(by_size, work, errors, cache, verify_bytes=False):
    groups = []
    for size, items in by_size.items():
        if len(items) < 2:
            continue
        for cand in _bucket(items, lambda p, m: _digest(p, True), work, errors):
            for dup in _bucket(cand, lambda p, m, s=size: _full_hash(p, m, s, cache), work, errors):
                paths = sorted(p for p, _ in dup)
                if verify_bytes:
                    groups += _byte_identical(paths, errors)
                else:
                    groups.append(paths)
    return groups


def _byte_identical(paths, errors):
    """Split paths into groups whose bytes really match, guarding against any hash collision."""
    groups = []
    for path in paths:
        for g in groups:
            try:
                if filecmp.cmp(g[0], path, shallow=False):
                    g.append(path)
                    break
            except OSError as e:
                errors.append(f"{path}: {e}")
                break
        else:
            groups.append([path])
    return [g for g in groups if len(g) > 1]


def _work_total(by_size):
    # Two hashing passes over every file that shares a size with another file.
    return 2 * sum(len(items) for items in by_size.values() if len(items) > 1)


def _name_clashes(items, groups):
    """Files with the same name that are not all copies of one another."""
    group_of = {p: i for i, g in enumerate(groups) for p in g}
    by_name = defaultdict(list)
    for path, _ in items:
        by_name[os.path.basename(path)].append(path)
    clashes = []
    for paths in by_name.values():
        if len(paths) < 2:
            continue
        ids = {group_of.get(p) for p in paths}
        if len(ids) == 1 and None not in ids:
            continue  # all one duplicate group, so the same content
        clashes.append(sorted(paths))
    return clashes


def _similar_groups(items, opts, work, errors):
    def read(item):
        try:
            return item[0], _signature(item[0], opts.mode), None
        except Exception as e:  # unreadable or corrupt files should not stop the scan
            return item[0], None, e

    sigs = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for path, sig, err in pool.map(read, items):  # map keeps the input order
            work.tick("Reading files")
            if err is not None:
                errors.append(f"{path}: {err}")
            elif not _is_empty(sig):
                sigs.append((path, sig))
    if opts.mode == "images":
        return _image_groups(sigs, opts.threshold, work)
    # A file joins a group only if it matches every member, so unrelated files never end up together
    # through a chain of matches.
    similar = _is_similar(opts.mode, opts.threshold)
    groups = []
    for path, sig in sigs:
        work.tick("Comparing files")
        for g in groups:
            if all(similar(sig, other) for _p, other in g):
                g.append((path, sig))
                break
        else:
            groups.append([(path, sig)])
    return [sorted(p for p, _ in g) for g in groups if len(g) > 1]


def _array_module():
    """cupy when a CUDA GPU can be used, otherwise numpy. Set DUPEFINDER_GPU=0 to always use the CPU."""
    if os.environ.get("DUPEFINDER_GPU") == "0":
        return np
    try:
        import cupy
        if cupy.cuda.runtime.getDeviceCount() > 0:
            probe = cupy.ones((2, 2), dtype=cupy.float32)
            (probe @ probe).get()  # a real multiply, so missing CUDA libraries are caught here, not mid-scan
            return cupy
    except Exception:  # cupy is optional; no package, no driver or no CUDA libraries means the CPU
        pass
    return np


def _image_groups(sigs, threshold, work):
    """Compare every image's 20 hashes against all others with one matrix product per image.

    Each 256-bit hash becomes 256 values of +1 or -1; the dot product of two such vectors is
    256 - 2 * Hamming distance, so the distances for a whole row come out of a single BLAS call.
    """
    n = len(sigs)
    if n < 2:
        return []
    raw = b"".join(h.to_bytes(32, "big") for _p, hashes in sigs for h in hashes)
    bits = np.unpackbits(np.frombuffer(raw, dtype=np.uint8)).astype(np.float32)
    xp = _array_module()
    signs = xp.asarray(1.0 - 2.0 * bits).reshape(n * 20, 256)
    similar = np.zeros((n, n), dtype=bool)
    for i in range(n):
        work.tick("Comparing images")
        dist = (256.0 - signs[i * 20:(i + 1) * 20] @ signs.T) / 2  # (20, 20 * n)
        best = dist.reshape(20, n, 20).min(axis=(0, 2))
        similar[i] = (best.get() if xp is not np else best) <= threshold
    groups = []
    for i in range(n):  # a file joins a group only if it matches every member
        for g in groups:
            if similar[i, g].all():
                g.append(i)
                break
        else:
            groups.append([i])
    return [sorted(sigs[i][0] for i in g) for g in groups if len(g) > 1]


def _all(by_size):
    return [item for items in by_size.values() for item in items]


def find_duplicates(root, opts=None, cancel=None, progress=None, cache_path=None, pause=None):
    """Group duplicates under root. progress(label, done, total) is called as work proceeds.

    cancel and pause are threading.Event objects. Setting pause halts work until it is cleared.
    """
    opts = opts or Options()
    ctl = _Control(cancel, pause)
    result = Result()
    cache = HashCache(cache_path) if cache_path and opts.mode == "exact" else None
    try:
        by_size = _collect(root, opts, result.errors, ctl)
        if opts.mode == "exact":
            work = _Work(_work_total(by_size), progress, ctl)
            result.groups = _exact_groups(by_size, work, result.errors, cache, opts.verify_bytes)
            result.name_clashes = _name_clashes(_all(by_size), result.groups)
        else:
            items = _all(by_size)
            work = _Work(2 * len(items), progress, ctl)
            result.groups = _similar_groups(items, opts, work, result.errors)
    except Cancelled:
        result.cancelled = True
    finally:
        if cache:
            cache.close()
    result.groups.sort(key=lambda g: (-os.path.getsize(g[0]), g[0]))
    return result


def compare_folders(a, b, opts=None, cancel=None, progress=None, cache_path=None, pause=None):
    """Copies that appear in both folders, plus files in a with no identical copy in b."""
    opts = opts or Options()
    ctl = _Control(cancel, pause)
    result = Result()
    cache = HashCache(cache_path) if cache_path else None
    try:
        by_a = _collect(a, opts, result.errors, ctl)
        by_b = _collect(b, opts, result.errors, ctl)
        merged = defaultdict(dict)  # size -> {path: mtime}, so a path appears once
        for by in (by_a, by_b):
            for size, items in by.items():
                merged[size].update(dict(items))
        merged = {s: list(d.items()) for s, d in merged.items()}
        work = _Work(_work_total(merged), progress, ctl)
        groups = _exact_groups(merged, work, result.errors, cache)
    except Cancelled:
        result.cancelled = True
        return result
    finally:
        if cache:
            cache.close()

    a_abs, b_abs = os.path.abspath(a) + os.sep, os.path.abspath(b) + os.sep
    matched_a = set()
    for g in groups:
        in_a = [p for p in g if os.path.abspath(p).startswith(a_abs)]
        in_b = [p for p in g if os.path.abspath(p).startswith(b_abs)]
        if in_a and in_b:
            result.groups.append(g)
            matched_a.update(in_a)
    result.unmatched = sorted(p for p, _ in _all(by_a) if p not in matched_a)
    result.groups.sort(key=lambda g: (-os.path.getsize(g[0]), g[0]))
    return result
