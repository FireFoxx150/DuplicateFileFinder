"""List mounted drives on Linux so the app can offer them as scan roots."""
import os
import re
import shutil
from dataclasses import dataclass

# Filesystems that hold user files. Skips proc, sysfs, tmpfs, overlay and similar.
REAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "zfs", "jfs", "reiserfs", "vfat",
           "exfat", "ntfs", "ntfs3", "fuseblk", "hfsplus", "apfs", "udf", "iso9660"}


@dataclass
class Drive:
    mountpoint: str
    fstype: str
    total: int
    free: int
    name: str = ""  # the volume label, empty when the drive has none


def _unescape(s):
    # /proc/mounts writes spaces and tabs as octal escapes, e.g. \040
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def _labels(by_label="/dev/disk/by-label"):
    """Map each device's real path to its volume label, from the links udev keeps for labelled drives."""
    try:
        names = os.listdir(by_label)
    except OSError:
        return {}
    # udev writes spaces in labels as \x20 in the link names
    return {os.path.realpath(os.path.join(by_label, n)): re.sub(
        r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), n) for n in names}


def list_drives(mounts="/proc/mounts", usage=shutil.disk_usage, labels=None):
    try:
        with open(mounts) as f:
            lines = f.readlines()
    except OSError:
        return []  # not Linux, or no /proc
    labels = _labels() if labels is None else labels
    drives, seen = [], set()
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        device, mountpoint, fstype = parts[0], _unescape(parts[1]), parts[2]
        if not device.startswith("/dev/") or fstype not in REAL_FS or mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            u = usage(mountpoint)  # a stale or unreadable mount is skipped, not fatal
        except OSError:
            continue
        drives.append(Drive(mountpoint, fstype, u.total, u.free, labels.get(os.path.realpath(device), "")))
    return drives
