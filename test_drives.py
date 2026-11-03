import os
import tempfile
import unittest
from collections import namedtuple

from drives import list_drives

Usage = namedtuple("Usage", "total used free")

MOUNTS = """\
/dev/nvme0n1p2 / ext4 rw,relatime 0 0
proc /proc proc rw 0 0
tmpfs /run tmpfs rw 0 0
/dev/sdb1 /mnt/backup\\040disk ntfs3 rw 0 0
/dev/sdc1 /mnt/photos exfat rw 0 0
/dev/sdc1 /mnt/photos exfat rw 0 0
/dev/sdd1 /mnt/gone ext4 rw 0 0
server:/share /mnt/nas nfs rw 0 0
"""


def fake_usage(path):
    if path == "/mnt/gone":
        raise OSError("stale mount")
    return Usage(total=100, used=40, free=60)


class ListDrivesTest(unittest.TestCase):
    def _list(self, text):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mounts")
            with open(path, "w") as f:
                f.write(text)
            return list_drives(path, usage=fake_usage)

    def test_keeps_real_local_filesystems_only(self):
        mounts = [(d.mountpoint, d.fstype) for d in self._list(MOUNTS)]
        self.assertEqual(mounts, [("/", "ext4"), ("/mnt/backup disk", "ntfs3"), ("/mnt/photos", "exfat")])

    def test_skips_mounts_that_cannot_be_read(self):
        self.assertNotIn("/mnt/gone", [d.mountpoint for d in self._list(MOUNTS)])

    def test_reports_free_and_total_space(self):
        d = self._list("/dev/sdb1 /mnt/x ext4 rw 0 0\n")[0]
        self.assertEqual((d.total, d.free), (100, 60))

    def test_volume_label_is_used_as_the_drive_name(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mounts")
            with open(path, "w") as f:
                f.write("/dev/sdb1 /mnt/17106d83-uuid ext4 rw 0 0\n/dev/sdc1 /mnt/other ext4 rw 0 0\n")
            found = {x.mountpoint: x.name for x in list_drives(path, usage=fake_usage,
                                                              labels={"/dev/sdb1": "DataDrive8tb"})}
        self.assertEqual(found, {"/mnt/17106d83-uuid": "DataDrive8tb", "/mnt/other": ""})

    def test_missing_proc_mounts_gives_empty_list(self):
        self.assertEqual(list_drives("/nonexistent/mounts"), [])


if __name__ == "__main__":
    unittest.main()
