import importlib.util
import io
import contextlib
import unittest
from types import SimpleNamespace

from minimal import config
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import Registry
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HelpOverlayTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / "extensions" / "help_overlay.py")

    def test_commands_sorted_aligned_and_keys_first(self):
        text = self.module.format_help({"zeta": "last", "alpha": "first one", "mid": ""})
        self.assertLess(text.index("Keys"), text.index("Commands"))
        rows = text[text.index("Commands") :].splitlines()[1:]
        self.assertEqual([row.split()[0] for row in rows], ["/alpha", "/mid", "/zeta"])
        self.assertEqual(rows[0].index("first"), rows[2].index("last"))
        self.assertEqual(rows[1], "  /mid")

    def test_control_characters_and_long_descriptions_are_neutralised(self):
        text = self.module.format_help({"x\x1b[31m": "evil\x1b]0;t\x07\nline2", "y": "w" * 500})
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)
        self.assertIn("evil ]0;t line2", text)
        self.assertTrue(all(len(line) < 120 for line in text.splitlines()))
        self.assertIn("…", text)

    def test_filter_drops_empty_sections_and_reports_no_match(self):
        commands = {"model": "pick a model", "exit": "quit"}
        text = self.module.format_help(commands, query="MODEL")
        self.assertIn("/model", text)
        self.assertNotIn("/exit", text)
        self.assertNotIn("Keys", text)
        self.assertIn("Nothing matches 'zzz'", self.module.format_help(commands, query="zzz"))

    def test_both_commands_print_the_listing(self):
        registry = Registry()
        _, errors = load_extensions(
            registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "extensions" / "help_overlay.py"]
        )
        self.assertEqual(errors, [])
        ctx = SimpleNamespace(commands=lambda: {"fold": "summarise"})
        for name in ("keys", "shortcuts"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                registry.commands[name].run("fold", ctx)
            self.assertIn("/fold", out.getvalue())


class ErrorsPanelTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.module = load_module(MARKETPLACE / "widgets" / "errors_panel.py")

    def test_ring_buffer_drops_oldest_and_merges_repeats(self):
        entries = []
        for source, message in [("bash", "a"), ("bash", "a"), ("read", "b"), ("edit", "c"), ("bash", "d")]:
            self.module.record(entries, source, message, 3)
        self.assertEqual([(e["source"], e["count"]) for e in entries], [("read", 1), ("edit", 1), ("bash", 1)])
        self.module.record(entries, "bash", "d", 3)
        self.assertEqual(entries[-1]["count"], 2)

    def test_rows_newest_first_with_count_and_clipping(self):
        entries = []
        self.module.record(entries, "read", "old", 5)
        for _ in range(3):
            self.module.record(entries, "bash", "x" * 100, 5)
        lines = self.module.format_rows(entries, 5, 30)
        self.assertEqual(lines[0][1][0].strip(), "4")
        newest = lines[1]
        self.assertEqual(newest[0][0], "bash")
        self.assertEqual(newest[1][0], " ×3")
        self.assertLessEqual(sum(len(p if isinstance(p, str) else p[0]) for p in newest), 30)
        self.assertTrue(newest[2].endswith("…"))
        self.assertEqual(lines[2][0][0], "read")

    def test_limit_shows_only_latest_rows(self):
        entries = []
        for i in range(4):
            self.module.record(entries, f"t{i}", "m", 10)
        lines = self.module.format_rows(entries, 2, 30)
        self.assertEqual([row[0][0] for row in lines[1:]], ["t3", "t2"])

    def test_hidden_when_empty(self):
        self.assertIsNone(self.module.format_rows([], 5, 30))

    def test_control_characters_become_spaces(self):
        entries = []
        self.module.record(entries, "bash", "boom\x1b[2J\nnext\x00", 5)
        self.assertEqual(entries[0]["message"], "boom [2J next")

    def test_events_and_clear_command(self):
        registry = Registry()
        _, errors = load_extensions(
            registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "widgets" / "errors_panel.py"]
        )
        self.assertEqual(errors, [])
        ctx = SimpleNamespace()
        for handler in registry._handlers["tool_result"]:
            handler({"name": "bash", "input": {}, "output": "bad", "is_error": True, "ctx": ctx})
            handler({"name": "read", "input": {}, "output": "fine", "is_error": False, "ctx": ctx})
        for handler in registry._handlers["provider_error"]:
            handler(
                {"ctx": ctx, "error": RuntimeError("429"), "attempt": 1, "provider": "p", "model": "m", "nested": False}
            )
        panel = next(iter(registry.panels.values()))
        lines = panel.render(ctx)
        self.assertEqual([row[0][0] for row in lines[1:]], ["provider", "bash"])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            registry.commands["errors"].run("clear", ctx)
        self.assertIsNone(panel.render(ctx))


if __name__ == "__main__":
    unittest.main()
