"""Summaries of duplicate groups: reclaimable space by file type and by folder, and a treemap layout."""
import os
from collections import defaultdict


def _wasted(group):
    return os.path.getsize(group[0]) * (len(group) - 1)


def space_by_type(groups):
    """Reclaimable bytes per file extension, largest first: list of (extension, bytes)."""
    totals = defaultdict(int)
    for g in groups:
        ext = os.path.splitext(g[0])[1].lower() or "(none)"
        totals[ext] += _wasted(g)
    return sorted(totals.items(), key=lambda kv: -kv[1])


def reclaimable_by_folder(groups, root):
    """Reclaimable bytes per first-level folder under root, largest first.

    The copy kept in each group is not counted; the other copies are attributed to the folder they sit in.
    Copies outside root are ignored, so the same call drills down when root is a subfolder.
    """
    root = os.path.normpath(root)
    totals = defaultdict(int)
    for g in groups:
        size = os.path.getsize(g[0])
        for path in g[1:]:
            folder = os.path.normpath(os.path.dirname(path))
            if folder != root and not folder.startswith(root + os.sep):
                continue
            rel = os.path.relpath(folder, root)
            top = "(this folder)" if rel == "." else rel.split(os.sep)[0]
            totals[top] += size
    return sorted(totals.items(), key=lambda kv: -kv[1])


def treemap(items, x, y, w, h):
    """Lay out (label, value) items as rectangles that fill (x, y, w, h), in proportion to value.

    Returns a list of (label, value, x, y, w, h). Uses the slice-and-dice method: rows alternate
    between horizontal and vertical splits, so each rectangle stays reasonably square.
    """
    items = [(label, v) for label, v in items if v > 0]
    total = sum(v for _, v in items)
    if total == 0 or w <= 0 or h <= 0:
        return []
    rects = []
    _slice(sorted(items, key=lambda kv: -kv[1]), total, x, y, w, h, rects, horizontal=w >= h)
    return rects


def _slice(items, total, x, y, w, h, out, horizontal):
    if not items:
        return
    if len(items) == 1:
        label, v = items[0]
        out.append((label, v, x, y, w, h))
        return
    first, rest = items[0], items[1:]
    share = first[1] / total
    if horizontal:
        fw = w * share
        out.append((first[0], first[1], x, y, fw, h))
        _slice(rest, total - first[1], x + fw, y, w - fw, h, out, not horizontal)
    else:
        fh = h * share
        out.append((first[0], first[1], x, y, w, fh))
        _slice(rest, total - first[1], x, y + fh, w, h - fh, out, not horizontal)
