"""Regression tests for the security-audit fixes (run 1)."""
import json
import os
import shutil
import ssl
import tempfile
import types
import unittest
from unittest import mock

import actions
import cli
import engine
import schedule


class HardlinkFixTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def path(self, name):
        return os.path.join(self.dir, name)

    def test_copies_with_different_bytes_are_left_alone(self):
        a, b = self.path("a.txt"), self.path("b.txt")
        open(a, "w").write("kept copy")
        open(b, "w").write("unique content")
        linked, failed = actions.hardlink_copies([[a, b]], {b})
        self.assertEqual(linked, [])
        self.assertEqual(open(b).read(), "unique content")
        self.assertEqual(len(failed), 1)

    def test_pre_existing_link_name_is_not_deleted(self):
        a, b = self.path("a.txt"), self.path("b.txt")
        open(a, "w").write("same")
        open(b, "w").write("same")
        victim = b + ".dupefinder-link"
        open(victim, "w").write("the user's own file")
        actions.hardlink_copies([[a, b]], {b})
        self.assertEqual(open(victim).read(), "the user's own file")

    def test_identical_copies_are_still_linked(self):
        a, b = self.path("a.txt"), self.path("b.txt")
        open(a, "w").write("same")
        open(b, "w").write("same")
        linked, failed = actions.hardlink_copies([[a, b]], {b})
        self.assertEqual(linked, [b])
        self.assertEqual(os.stat(a).st_ino, os.stat(b).st_ino)
        self.assertEqual(failed, [])


class UndoFixTest(unittest.TestCase):
    def test_undo_does_not_overwrite_a_newer_file(self):
        d = tempfile.mkdtemp()
        try:
            original, moved = os.path.join(d, "x.txt"), os.path.join(d, "moved.txt")
            open(moved, "w").write("old content")
            open(original, "w").write("newer content")
            undo = os.path.join(d, "undo.json")
            json.dump([{"from": original, "to": moved}], open(undo, "w"))
            restored, failed = actions.undo_moves(undo)
            self.assertEqual(restored, [])
            self.assertEqual(open(original).read(), "newer content")
            self.assertTrue(os.path.exists(moved))
            self.assertEqual(len(failed), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ScheduleFixTest(unittest.TestCase):
    def test_folder_with_line_break_is_refused(self):
        with self.assertRaises(ValueError):
            schedule.unit_text("/data\n[Service]\nExecStart=/bin/true", 24, "/usr/bin/dupefinder-cli", "/r.html")

    def test_normal_folder_still_builds_units(self):
        service, timer = schedule.unit_text("/data/photos", 24, "/usr/bin/dupefinder-cli", "/r.html")
        self.assertIn("ExecStart=", service)
        self.assertIn("OnUnitActiveSec=24h", timer)


class SmtpFixTest(unittest.TestCase):
    def test_starttls_requires_a_verified_certificate(self):
        args = types.SimpleNamespace(folder="/data", mail_from=None, mail_to="a@example.invalid",
                                     smtp_host="mail.example.invalid", smtp_port=587, smtp_tls=True)
        with mock.patch.object(cli.smtplib, "SMTP") as smtp_cls:
            smtp = smtp_cls.return_value.__enter__.return_value
            cli.send_report([], args)
        context = smtp.starttls.call_args.kwargs["context"]
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)


class FfmpegWhitelistTest(unittest.TestCase):
    def test_video_frames_limit_ffmpeg_inputs_to_plain_files(self):
        with mock.patch.object(engine.shutil, "which", return_value="/usr/bin/ffmpeg"), \
             mock.patch.object(engine.subprocess, "run") as run:
            run.return_value = types.SimpleNamespace(returncode=1, stdout=b"")
            with self.assertRaises(OSError):
                engine._video_frames("/data/clip.mp4")
        argv = run.call_args.args[0]
        i = argv.index("-protocol_whitelist")
        self.assertEqual(argv[i + 1], "file")
        self.assertLess(i, argv.index("-i"))


if __name__ == "__main__":
    unittest.main()
