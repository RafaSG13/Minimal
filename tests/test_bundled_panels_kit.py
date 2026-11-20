import importlib.util
import io
import contextlib
import unittest
from pathlib import Path
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
        self.assertEqual(lines[0], [("4 errors", "dim")])
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


def user_settings(ext_id, values):
    layer = config.Layer("global", Path("settings.json"), {"extensions.settings": {ext_id: values}})
    return config.Settings(layers=(layer,))


def call(id, name, **inp):
    return {"role": "assistant", "content": [{"type": "tool_use", "id": id, "name": name, "input": inp}]}


def result(id, is_error=False):
    return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": id, "content": "x", "is_error": is_error}]}


class PanelSettingsTest(BundledExtensionTest):
    def load(self, item, values=None):
        ext_id = f"widgets/{item}"
        registry = Registry()
        settings = user_settings(ext_id, values) if values is not None else config.Settings()
        _, errors = load_extensions(registry, str(self.tmp), settings, extra=[MARKETPLACE / "widgets" / f"{item}.py"])
        return registry, errors

    def panel(self, item, values=None):
        registry, errors = self.load(item, values)
        self.assertEqual(errors, [])
        return next(iter(registry.panels.values()))

    def ctx(self, messages):
        return SimpleNamespace(messages=messages, cwd=str(self.tmp))

    def text(self, lines):
        return [row if isinstance(row, str) else "".join(p if isinstance(p, str) else p[0] for p in row) for row in lines]

    def test_defaults_register_a_titled_collapsible_sidebar_panel(self):
        for item, title in [
            ("files_touched", "FILES"),
            ("tool_timeline", "TOOLS"),
            ("plan_board", "PLAN"),
            ("errors_panel", "ERRORS"),
            ("diff_panel", "DIFF"),
        ]:
            with self.subTest(item=item):
                panel = self.panel(item)
                self.assertEqual((panel.area, panel.title, panel.collapsible, panel.order), ("sidebar", title, True, 0))

    def test_title_area_order_settings_apply_and_enabled_false_registers_nothing(self):
        panel = self.panel("tool_timeline", {"title": "CALLS", "area": "left", "order": 5})
        self.assertEqual((panel.title, panel.area, panel.order), ("CALLS", "left", 5))
        self.assertIsNone(self.panel("errors_panel", {"title": ""}).title)
        registry, errors = self.load("plan_board", {"enabled": False})
        self.assertEqual((errors, registry.panels, "plan" in registry.tools), ([], {}, False))

    def test_bad_settings_are_reported_and_name_the_item(self):
        for item, values in [
            ("tool_timeline", {"turns": -1}),
            ("tool_timeline", {"styles": {"ok": "not a style!"}}),
            ("plan_board", {"glyphs": {"done": ""}}),
            ("files_touched", {"sort": "random"}),
            ("errors_panel", {"ignore": ["("]}),
            ("diff_panel", {"tools": []}),
        ]:
            with self.subTest(item=item, values=values):
                _, errors = self.load(item, values)
                self.assertEqual(len(errors), 1)
                self.assertIn(f"widgets/{item}", errors[0])

    def test_tool_timeline_settings_shape_the_marks_and_bars(self):
        messages = [{"role": "user", "content": "go"}]
        for i, name in enumerate(["bash", "bash", "read"]):
            messages += [call(f"c{i}", name), result(f"c{i}", is_error=(i == 2))]
        default = self.text(self.panel("tool_timeline").render(self.ctx(messages)))
        self.assertEqual(default[0], "3 calls · 1 errors")
        self.assertEqual(default[1][:4], "  1 ")
        custom = self.panel(
            "tool_timeline", {"glyphs": {"ok": "+", "error": "!"}, "top_tools": 0, "turns": 0}
        )
        self.assertEqual(self.text(custom.render(self.ctx(messages))), ["3 calls · 1 errors"])
        marked = self.panel("tool_timeline", {"glyphs": {"ok": "+", "error": "!"}, "top_tools": 0})
        self.assertEqual(self.text(marked.render(self.ctx(messages)))[1], "  1 ++!")

    def test_plan_board_settings(self):
        steps = [{"text": "a", "status": "done"}, {"text": "b", "status": "pending"}]
        messages = [call("p", "plan", steps=steps), result("p")]
        default = self.text(self.panel("plan_board").render(self.ctx(messages)))
        self.assertEqual(default[0], "1/2")
        self.assertEqual(len(default), 4)
        slim = self.panel("plan_board", {"bar_width": 0, "glyphs": {"done": "X"}})
        lines = self.text(slim.render(self.ctx(messages)))
        self.assertEqual(lines, ["1/2", "X a", lines[2]])
        self.assertTrue(lines[2].endswith(" b"))

    def test_files_touched_settings(self):
        messages = [
            call("r", "read", path="a.py"),
            result("r"),
            call("w", "write", path="b.py", content="x\ny\n"),
            result("w"),
        ]
        default = self.text(self.panel("files_touched").render(self.ctx(messages)))
        self.assertEqual(len(default), 3)
        self.assertTrue(default[0].startswith("2 · +2"))
        no_reads = self.text(self.panel("files_touched", {"show_reads": False}).render(self.ctx(messages)))
        self.assertEqual(len(no_reads), 2)
        one_row = self.text(self.panel("files_touched", {"rows": 1}).render(self.ctx(messages)))
        self.assertEqual(len(one_row), 2)

    def test_errors_panel_settings(self):
        ctx = SimpleNamespace()
        registry, errors = self.load("errors_panel", {"ignore": ["denied"], "include_provider": False, "newest_first": False})
        self.assertEqual(errors, [])
        for handler in registry._handlers["tool_result"]:
            handler({"name": "bash", "input": {}, "output": "Permission denied", "is_error": True, "ctx": ctx})
            handler({"name": "edit", "input": {}, "output": "boom", "is_error": True, "ctx": ctx})
            handler({"name": "read", "input": {}, "output": "oops", "is_error": True, "ctx": ctx})
        for handler in registry._handlers["provider_error"]:
            handler({"ctx": ctx, "error": RuntimeError("429"), "attempt": 1, "provider": "p", "model": "m", "nested": False})
        lines = self.text(next(iter(registry.panels.values())).render(ctx))
        self.assertEqual([row.split()[0] for row in lines[1:]], ["edit", "read"])

    def test_ledger_settings(self):
        big = "z" * 3000
        messages = [
            {"role": "user", "content": "go"},
            call("r", "bash", command="ls"),
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r", "content": big}]},
        ]
        ctx = SimpleNamespace(messages=messages, cwd=str(self.tmp), context_tokens=None)

        def build(values=None):
            registry = Registry()
            settings = user_settings("ledger", values) if values is not None else config.Settings()
            _, errors = load_extensions(registry, str(self.tmp), settings, extra=[MARKETPLACE / "extensions" / "ledger.py"])
            self.assertEqual(errors, [])
            return registry.panels["ledger"]

        panel = build()
        self.assertEqual((panel.title, panel.collapsible, panel.area), ("CONTEXT", True, "sidebar"))
        self.assertEqual(len(panel.render(ctx)), 3)
        self.assertEqual(len(build({"rows": 0}).render(ctx)), 2)
        self.assertEqual(build({"title": "", "order": 2}).title, None)
        registry = Registry()
        _, errors = load_extensions(
            registry, str(self.tmp), user_settings("ledger", {"rows": 999}), extra=[MARKETPLACE / "extensions" / "ledger.py"]
        )
        self.assertEqual(len(errors), 1)

    def test_live_activity_idle_settings(self):
        ctx = SimpleNamespace()
        self.assertEqual(self.text(self.panel("live_activity").render(ctx)), ["idle"])
        self.assertIsNone(self.panel("live_activity", {"idle": "hide"}).render(ctx))
        self.assertEqual(self.text(self.panel("live_activity", {"idle_text": "zzz"}).render(ctx)), ["zzz"])
        self.assertIsNone(self.panel("live_activity").title)
        titled = self.panel("live_activity", {"title": "NOW"})
        self.assertEqual((titled.title, titled.collapsible), ("NOW", True))

    def test_live_activity_counts_and_thinking_text(self):
        for values, expect_counts, thinking in [(None, True, "⋯ thinking"), ({"show_counts": False, "thinking_text": "hm"}, False, "hm")]:
            with self.subTest(values=values):
                panel = self.panel("live_activity", values)
                ctx = SimpleNamespace(invalidate=lambda: None)
                panel.live(ctx, {"kind": "start", "turn": True})
                try:
                    line = self.text(panel.render(ctx))[0]
                finally:
                    panel.live(ctx, {"kind": "end"})
                self.assertIn(thinking, line)
                self.assertEqual("calls" in line, expect_counts)


if __name__ == "__main__":
    unittest.main()
