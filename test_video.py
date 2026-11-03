import os
import subprocess
import tempfile
import unittest
from unittest import mock

import previews

# Encoder arguments for the containers ffmpeg does not pick a codec for by default.
EXTRA_ARGS = {
    ".mpg": ["-f", "mpeg", "-c:v", "mpeg2video"],
    ".mpeg": ["-f", "mpeg", "-c:v", "mpeg2video"],
    ".3gp": ["-c:v", "mpeg4", "-f", "3gp"],
    ".divx": ["-c:v", "mpeg4", "-f", "avi"],
}


# Listed in previews.VIDEO_EXTS but not checked here: the bundled ffmpeg segfaults on MPEG-TS
# input, and it cannot write these containers, so no sample clip can be made.
UNTESTED = {".ts", ".mts", ".m2ts", ".3g2", ".dv", ".mxf", ".rm", ".rmvb"}


def _ffmpeg_exe():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return None


class VideoPreviewTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        exe = _ffmpeg_exe()
        if not exe:
            raise unittest.SkipTest("needs imageio-ffmpeg to generate test clips")
        cls.exe = exe
        cls.bin_dir = tempfile.mkdtemp()
        os.symlink(exe, os.path.join(cls.bin_dir, "ffmpeg"))

    def _env(self):
        return mock.patch.dict(os.environ, {"PATH": self.bin_dir + os.pathsep + os.environ["PATH"]})

    def _clip(self, root, name, duration="2"):
        path = os.path.join(root, name)
        ext = os.path.splitext(name)[1]
        cmd = [self.exe, "-v", "error", "-y", "-f", "lavfi",
               "-i", f"testsrc=duration={duration}:size=160x120:rate=10",
               "-pix_fmt", "yuv420p", *EXTRA_ARGS.get(ext, []), path]
        subprocess.run(cmd, check=True, capture_output=True)
        return path

    def test_every_listed_format_gives_a_thumbnail(self):
        with tempfile.TemporaryDirectory() as root, self._env():
            for ext in sorted(previews.VIDEO_EXTS - UNTESTED):
                with self.subTest(ext=ext):
                    path = self._clip(root, "clip" + ext)
                    kind, payload = previews.preview(path)
                    self.assertEqual(kind, "image", payload)
                    self.assertTrue(payload.startswith(b"\x89PNG"))

    def test_clip_shorter_than_seek_time_still_gives_a_thumbnail(self):
        with tempfile.TemporaryDirectory() as root, self._env():
            path = self._clip(root, "short.mp4", duration="0.5")
            self.assertEqual(previews.preview(path)[0], "image")

    def test_without_ffmpeg_gives_install_hint(self):
        with tempfile.TemporaryDirectory() as root:
            path = os.path.join(root, "x.mp4")
            with open(path, "wb") as f:
                f.write(b"not a video")
            with mock.patch.dict(os.environ, {"PATH": root}):
                self.assertEqual(previews.preview(path), ("info", "Install ffmpeg for video thumbnails."))


if __name__ == "__main__":
    unittest.main()
