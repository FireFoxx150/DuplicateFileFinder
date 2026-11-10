import contextlib
import io
import os
import tempfile
import time
import tkinter as tk
import unittest
from email import message_from_bytes
from unittest import mock

import actions
import cli
import gui


def _write(root, rel, data):
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    return path


class CleanupCapTest(unittest.TestCase):
    def test_apply_refuses_above_the_cap_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a.txt", b"same")
            _write(root, "b.txt", b"same")
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                code = cli.main([root, "--cleanup", "--apply", "--max-files", "0"])
            self.assertEqual(code, 2)
            self.assertIn("Nothing was changed", err.getvalue())
            self.assertEqual(sorted(os.listdir(root)), ["a.txt", "b.txt"])

    def test_dry_run_is_the_default(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "a.txt", b"same")
            _write(root, "b.txt", b"same")
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main([root, "--cleanup"]), 0)
            self.assertIn("Dry run", out.getvalue())
            self.assertEqual(len(os.listdir(root)), 2)


class ChainingTest(unittest.TestCase):
    def test_a_file_must_match_every_member_of_its_group(self):
        # a~b and b~c, but a is not similar to c: c must not join a's group through b
        import engine
        # 1 bit apart for a-b and b-c, 2 bits for a-c; images carry 20 hashes each
        same = [0.0] * 15  # same colour, so only the shape decides
        sigs = {"a.png": ([0b00] * 20, same), "b.png": ([0b01] * 20, same), "c.png": ([0b11] * 20, same)}
        with mock.patch.object(engine, "_signature", side_effect=lambda p, m: sigs[os.path.basename(p)]), \
                tempfile.TemporaryDirectory() as root:
            for name in sigs:
                _write(root, name, b"x")
            res = engine.find_duplicates(root, engine.Options(mode="images", threshold=1))
            names = [sorted(os.path.basename(p) for p in g) for g in res.groups]
            self.assertIn(names, [[["a.png", "b.png"]], [["b.png", "c.png"]]])  # either pair, never a with c


class GpuFallbackTest(unittest.TestCase):
    def test_gpu_can_be_switched_off(self):
        import engine
        import numpy as np
        with mock.patch.dict(os.environ, {"DUPEFINDER_GPU": "0"}):
            self.assertIs(engine._array_module(), np)


class HardlinkPlanTest(unittest.TestCase):
    def test_same_drive_copies_are_listed_with_their_keeper(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a.bin", b"data")
            b = _write(root, "b.bin", b"data")
            c = _write(root, "c.bin", b"data")
            pairs = actions.hardlink_plan([[a, b, c]])
            keepers = {k for _p, k in pairs}
            self.assertEqual(len(keepers), 1)
            self.assertEqual({p for p, _k in pairs} | keepers, {a, b, c})


class EmailReportTest(unittest.TestCase):
    def test_report_is_sent_as_an_html_attachment(self):
        with tempfile.TemporaryDirectory() as root:
            a = _write(root, "a.txt", b"same")
            b = _write(root, "b.txt", b"same")
            args = mock.Mock(folder=root, mail_from="me@example.org", mail_to="you@example.org",
                             smtp_host="mail.example.org", smtp_port=2525, smtp_tls=False)
            with mock.patch("smtplib.SMTP") as smtp:
                cli.send_report([[a, b]], args)
            smtp.assert_called_once_with("mail.example.org", 2525, timeout=30)
            sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
            self.assertEqual(sent["To"], "you@example.org")
            parts = [p for p in message_from_bytes(sent.as_bytes()).walk()
                     if p.get_filename() == "duplicates.html"]
            self.assertEqual(len(parts), 1)
            self.assertIn(b"Group 1", parts[0].get_payload(decode=True))


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "needs a display")
class GuiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.patches = [
            mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp, "XDG_STATE_HOME": self.tmp,
                                         "XDG_CACHE_HOME": self.tmp}),
            mock.patch.object(gui, "CACHE_PATH", os.path.join(self.tmp, "hashes.db")),
            mock.patch.object(gui, "IGNORE_PATH", os.path.join(self.tmp, "ignored.json")),
            mock.patch.object(gui, "UNDO_PATH", os.path.join(self.tmp, "undo.json")),
            mock.patch.object(gui, "TRASH_LOG", os.path.join(self.tmp, "trashed.json")),
            mock.patch.object(gui, "HISTORY_PATH", os.path.join(self.tmp, "history.jsonl")),
        ]
        for p in self.patches:
            p.start()
        try:
            self.root = gui.TkinterDnD.Tk() if gui.TkinterDnD else tk.Tk()  # the app runs on the DnD root
        except tk.TclError:
            self.skipTest("no display")

    def tearDown(self):
        self.root.destroy()
        for p in self.patches:
            p.stop()

    def _wait(self, app):
        deadline = time.time() + 20
        while app.scan_btn.instate(["disabled"]) and time.time() < deadline:
            self.root.update()
            time.sleep(0.05)

    def test_scan_finds_copies_and_select_extras_picks_one_per_group(self):
        folder = os.path.join(self.tmp, "files")
        _write(folder, "a.txt", b"same")
        _write(folder, "b.txt", b"same")
        app = gui.App(self.root, start_folder=folder)
        app.scan()
        self._wait(app)
        self.assertEqual(len(app.groups), 1)
        app.select_extras()
        self.root.update()
        self.assertEqual(len(app._selected_paths()), 1)

    def test_command_palette_and_history_open_without_error(self):
        app = gui.App(self.root)
        app.command_palette()
        app.history_window()
        self.root.update()
        self.assertTrue(app.p)

    def test_preview_changes_nothing(self):
        folder = os.path.join(self.tmp, "files")
        a = _write(folder, "a.txt", b"same")
        b = _write(folder, "b.txt", b"same")
        app = gui.App(self.root, start_folder=folder)
        app.scan()
        self._wait(app)
        app.tree.selection_set([p for g in app.tree.get_children() for p in app.tree.get_children(g)])
        self.root.update()
        app.preview_plan()
        self.assertTrue(os.path.exists(a) and os.path.exists(b))


if __name__ == "__main__":
    unittest.main()
