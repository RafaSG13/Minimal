import unittest
from types import SimpleNamespace

from minimal import config
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry

from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest


class DiffPanelTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.registry = Registry()
        _, errors = load_extensions(
            self.registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "widgets" / "diff_panel.py"]
        )
        self.assertEqual(errors, [])
        self.ctx = SimpleNamespace(cwd=str(self.tmp))
        self.handlers = {name: self.registry._handlers[name][0] for name in ("turn_start", "tool_call", "tool_result")}
        self.turn = self.handlers["tool_result"].__self__  # the extension's Turn: its worker thread is flushed below
        self.addCleanup(self.turn.dispose)

    def call(self, name, path, change=None):
        """Fire tool_call, apply ``change(path)`` to the disk, fire tool_result."""
        event = {"name": name, "input": {"path": path}, "ctx": self.ctx}
        self.handlers["tool_call"](event)
        if change:
            change(self.tmp / path)
        self.handlers["tool_result"]({**event, "output": "ok", "is_error": False})
        self.turn.flush()  # the diffing runs on a worker thread

    def lines(self):
        panel = self.registry.panels["diff_panel"]
        return panel.render(self.ctx)

    @staticmethod
    def text(lines):
        return [
            "".join(p if isinstance(p, str) else p[0] for p in (row if isinstance(row, list) else [row]))
            for row in lines
        ]

    def test_edit_shows_styled_hunk_and_counts(self):
        (self.tmp / "a.txt").write_text("one\ntwo\nthree\n")
        self.call("edit", "a.txt", lambda p: p.write_text("one\n2\nthree\nfour\n"))
        lines = self.lines()
        self.assertEqual(self.text(lines)[0], "a.txt  +2 -1")
        styles = {x[0]: x[1] for x in lines[1:] if isinstance(x, tuple)}
        self.assertEqual(styles["-two"], "error")
        self.assertEqual(styles["+2"], "success")
        self.assertEqual(styles[" one"], "dim")

    def test_new_file_and_deleted_file(self):
        self.call("write", "n.txt", lambda p: p.write_text("x\ny\n"))
        (self.tmp / "d.txt").write_text("gone\n")
        self.call("write", "d.txt", lambda p: p.unlink())
        text = self.text(self.lines())
        self.assertIn("n.txt (new)  +2 -0", text)
        self.assertIn("d.txt (deleted)  +0 -1", text)

    def test_unchanged_and_failed_calls_leave_nothing(self):
        (self.tmp / "a.txt").write_text("same\n")
        self.call("edit", "a.txt")
        self.call("write", "never.txt")  # blocked call: file still missing
        self.assertIsNone(self.lines())

    def test_binary_oversize_and_unreadable_are_listed_not_diffed(self):
        (self.tmp / "b.bin").write_bytes(b"\x00\x01")
        self.call("write", "b.bin", lambda p: p.write_bytes(b"\x00\x02"))
        (self.tmp / "big.txt").write_text("x" * 300_000)
        self.call("edit", "big.txt", lambda p: p.write_text("y" * 300_000))
        (self.tmp / "dir").mkdir()
        self.call("write", "dir")
        text = self.text(self.lines())
        self.assertEqual(
            text,
            ["b.bin (binary file, not diffed)", "big.txt (file too large, not diffed)", "dir (unreadable, not diffed)"],
        )

    def test_non_utf8_is_tolerated(self):
        (self.tmp / "l.txt").write_bytes(b"caf\xe9\n")
        self.call("edit", "l.txt", lambda p: p.write_bytes(b"caf\xe9!\n"))
        self.assertEqual(self.text(self.lines())[0], "l.txt  +1 -1")

    def test_two_edits_in_a_turn_diff_against_the_first_snapshot(self):
        (self.tmp / "a.txt").write_text("1\n")
        self.call("edit", "a.txt", lambda p: p.write_text("2\n"))
        self.call("edit", "a.txt", lambda p: p.write_text("3\n"))
        self.assertIn("-1", self.text(self.lines()))
        self.assertIn("+3", self.text(self.lines()))
        self.call("edit", "a.txt", lambda p: p.write_text("1\n"))
        self.assertIsNone(self.lines())

    def test_turn_start_resets_and_other_tools_are_ignored(self):
        self.call("write", "n.txt", lambda p: p.write_text("x\n"))
        self.handlers["turn_start"]({"ctx": self.ctx, "text": "hi"})
        self.assertIsNone(self.lines())
        self.call("bash", "n.txt", lambda p: p.write_text("y\n"))
        self.assertIsNone(self.lines())

    def test_control_characters_are_escaped(self):
        self.call("write", "e.txt", lambda p: p.write_text("\x1b[31mred\x07\u2028\n"))
        shown = self.text(self.lines())[1:]
        self.assertTrue(all("\x1b" not in s and "\x07" not in s and "\u2028" not in s for s in shown))
        self.assertIn("+\\x1b[31mred\\x07\\u2028", shown)

    def test_caps_hunks_and_lines(self):
        old = "".join(f"line{i}\n" for i in range(100))
        new = old.replace("line10\n", "A\n").replace("line50\n", "B\n").replace("line90\n", "C\n")
        (self.tmp / "m.txt").write_text(old)
        self.call("edit", "m.txt", lambda p: p.write_text(new))
        panel = self.registry.panels["diff_panel"]
        import importlib.util

        spec = importlib.util.spec_from_file_location("dp", MARKETPLACE / "widgets" / "diff_panel.py")
        dp = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dp)
        change = dp.make_change(("text", old), ("text", new), 2)
        self.assertEqual(len(change["hunks"]), 3)
        last = self.text(dp.panel_lines([("m.txt", change)], 1, 50))
        self.assertIn("+C", last)
        self.assertNotIn("+A", last)
        self.assertEqual(last[-1], "… 2 earlier hunks, /diff shows all")
        short = self.text(dp.panel_lines([("m.txt", change)], 3, 4))
        self.assertEqual(len(short), 5)
        self.assertTrue(short[-1].startswith("… "))
        self.assertIsNotNone(panel)

    def test_diff_command_prints_everything(self):
        import contextlib
        import io

        (self.tmp / "a.txt").write_text("one\n")
        self.call("edit", "a.txt", lambda p: p.write_text("two\n"))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.registry.commands["diff"].run("", self.ctx)
        self.assertEqual(out.getvalue().splitlines()[:5], ["--- a/a.txt", "+++ b/a.txt", "@@ -1 +1 @@", "-one", "+two"])
        self.handlers["turn_start"]({"ctx": self.ctx, "text": ""})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.registry.commands["diff"].run("", self.ctx)
        self.assertIn("No files", out.getvalue())


if __name__ == "__main__":
    unittest.main()
