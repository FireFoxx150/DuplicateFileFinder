"""Live file watching on Linux, using inotify, so nothing has to poll the folder."""
import ctypes
import ctypes.util
import os
import select
import struct

IN_MODIFY = 0x2
IN_CLOSE_WRITE = 0x8
IN_MOVED_FROM = 0x40
IN_MOVED_TO = 0x80
IN_CREATE = 0x100
IN_DELETE = 0x200
IN_ISDIR = 0x40000000
EVENTS = IN_CLOSE_WRITE | IN_MOVED_TO | IN_CREATE | IN_DELETE | IN_MOVED_FROM
_HEADER = struct.Struct("iIII")  # wd, mask, cookie, name length

_libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)


def _folders(root):
    out = []
    for dirpath, _dirnames, _files in os.walk(root):
        out.append(dirpath)
    return out


def watch(root, callback, stop_event, poll_seconds=1.0):
    """Call callback(path, mask) for each file created, written, moved or deleted under root.

    Runs until stop_event is set. Folders created later are watched too.
    """
    fd = _libc.inotify_init1(os.O_NONBLOCK)
    if fd < 0:
        raise OSError(ctypes.get_errno(), "inotify is not available here")
    watched = {}

    def add(folder):
        wd = _libc.inotify_add_watch(fd, folder.encode(), EVENTS)
        if wd >= 0:
            watched[wd] = folder

    try:
        for folder in _folders(root):
            add(folder)
        while not stop_event.is_set():
            ready, _w, _x = select.select([fd], [], [], poll_seconds)
            if not ready:
                continue
            data = os.read(fd, 65536)
            offset = 0
            while offset < len(data):
                wd, mask, _cookie, length = _HEADER.unpack_from(data, offset)
                name = data[offset + _HEADER.size: offset + _HEADER.size + length].rstrip(b"\0")
                offset += _HEADER.size + length
                folder = watched.get(wd)
                if folder is None:
                    continue
                path = os.path.join(folder, name.decode(errors="replace"))
                if mask & IN_ISDIR:
                    if mask & IN_CREATE:
                        add(path)
                    continue
                callback(path, mask)
    finally:
        os.close(fd)
