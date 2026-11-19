"""Customising the terminal UI and extensions without code: keybindings, areas, order, ``ui.*`` settings, ``/ui``,
and extension ``__depends__``/``__conflicts__``."""

import asyncio
import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from textual.color import Color
from textual.widgets import Static

from minimal import config
from minimal.core import layout
from minimal.core.app import App
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.extensions import loader, marketplace
from minimal.extensions.metadata import Metadata, read_metadata
from minimal.extensions.registry import Registry, normalize_key
from minimal.ui.commands import register_builtin_commands
from minimal.ui.tui.app import MinimalTui


def header(name: str, extra: str = "") -> str:
    return f'"""{name}."""\n__version__ = "1.0"\n__api__ = "2"\n{extra}'


class CustomisationTest(unittest.TestCase):
    """A temp ``~/.minimal`` and working folder; ``write_settings`` and ``write_ext`` fill them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name).resolve()
        self.home = self.tmp / "home"
        self.cwd = self.tmp / "work"
        self.cwd.mkdir()
        self.global_ext = self.home / "extensions"
        for patcher in (
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.global_ext),
            mock.patch.object(config, "HOME_DIR", self.home),
            mock.patch.object(config, "SETTINGS_FILE", self.home / "settings.json"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def write_settings(self, data: dict, scope: str = "global") -> None:
        path = config.settings_files(str(self.cwd))[scope]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def write_ext(self, name: str, body: str = "", extra: str = "") -> Path:
        self.global_ext.mkdir(parents=True, exist_ok=True)
        path = self.global_ext / f"{name}.py"
        path.write_text(header(name, extra) + textwrap.dedent(body))
        return path

    def settings(self) -> config.Settings:
        layers, _ = config.read_layers(str(self.cwd))
        return config.Settings(tuple(layers))

    def load(self) -> tuple[Registry, list[Path], list[str]]:
        registry = Registry()
        loaded, errors = loader.load_extensions(registry, str(self.cwd), self.settings())
        return registry, loaded, errors


class KeybindingRegistryTest(CustomisationTest):
    def test_keys_are_validated_and_reserved_ones_refused(self):
        registry = Registry()
        for good in ("ctrl+o", "alt+x", "f5", "Ctrl+Shift+O", "ctrl+alt+left", "insert"):
            registry.register_keybinding(good, lambda ctx: None)
        self.assertIn("ctrl+shift+o", registry.keybindings)
        for reserved in ("escape", "ctrl+c", "ctrl+p", "ctrl+q", "enter", "tab", "shift+tab", "up", "down", "ctrl+b", "ctrl+e"):
            with self.assertRaisesRegex(ValueError, "reserved"):
                registry.register_keybinding(reserved, lambda ctx: None)
        for bad in ("", "ctrl+", "ctr+o", "ctrl+foo", "a", "shift+a", "space", "ctrl+f25", 5):
            with self.assertRaises(ValueError, msg=bad):
                normalize_key(bad)
        with self.assertRaisesRegex(ValueError, "run is required"):
            registry.register_keybinding("ctrl+t", None)  # type: ignore[arg-type]

    def test_a_later_binding_of_the_same_key_replaces_and_the_extension_reports_it(self):
        self.write_ext("first", 'def register(minimal):\n    minimal.register_keybinding("ctrl+o", lambda ctx: None, "one")\n')
        self.write_ext("second", 'def register(minimal):\n    minimal.register_keybinding("ctrl+o", lambda ctx: None, "two")\n')
        registry, loaded, errors = self.load()
        self.assertEqual([p.name for p in loaded], ["first.py", "second.py"])
        self.assertEqual(registry.keybindings["ctrl+o"].description, "two")
        self.assertEqual(registry.keybindings["ctrl+o"].id, "second:two")
        self.assertEqual(len(errors), 1)
        self.assertIn("second: key ctrl+o was bound by first's binding 'one'; replaced", errors[0])

    def test_a_reserved_key_is_a_load_error_and_the_decorator_names_from_the_docstring(self):
        self.write_ext(
            "bad", 'def register(minimal):\n    minimal.register_keybinding("escape", lambda ctx: None)\n'
        )
        self.write_ext(
            "good",
            '''
            def register(minimal):
                @minimal.keybinding("ctrl+o", name="mark")
                def mark(ctx):
                    """Mark the spot."""
            ''',
        )
        registry, loaded, errors = self.load()
        self.assertEqual([p.name for p in loaded], ["good.py"])
        self.assertTrue(any("bad.py" in e and "escape is reserved" in e for e in errors), errors)
        spec = registry.keybindings["ctrl+o"]
        self.assertEqual((spec.description, spec.id), ("Mark the spot.", "good:mark"))

    def test_two_keys_of_one_extension_cannot_share_a_name(self):
        registry = Registry()
        registry.register_keybinding("ctrl+o", lambda ctx: None, "same", ext="x")
        with self.assertRaisesRegex(ValueError, "already names ctrl\\+o"):
            registry.register_keybinding("ctrl+t", lambda ctx: None, "same", ext="x")
        registry.register_keybinding("ctrl+t", lambda ctx: None, "same", name="other", ext="x")


class KeyMapTest(CustomisationTest):
    def registry(self) -> Registry:
        registry = Registry()
        registry.register_keybinding("ctrl+o", lambda ctx: None, "Open", ext="e")
        registry.register_keybinding("ctrl+g", lambda ctx: None, "Go", ext="e")
        return registry

    def keys(self, user: dict) -> tuple[dict, list[str]]:
        self.write_settings({"ui": {"keys": user}})
        bound, problems = layout.resolve_keys(self.registry(), self.settings())
        return {b.id: b.key for b in bound}, problems

    def test_defaults(self):
        keys, problems = self.keys({})
        self.assertEqual(
            keys, {"interrupt": "escape", "toggle_sidebar": "ctrl+b", "cycle_mode": "shift+tab", "e:Open": "ctrl+o", "e:Go": "ctrl+g"}
        )
        self.assertEqual(problems, [])

    def test_rebinding_actions_and_extension_keys_by_name(self):
        keys, problems = self.keys({"toggle_sidebar": "ctrl+t", "e:Open": "alt+o", "e:Go": ""})
        self.assertEqual((keys["toggle_sidebar"], keys["e:Open"], keys["e:Go"]), ("ctrl+t", "alt+o", None))
        self.assertEqual(problems, [])

    def test_bad_requests_are_problems_and_keep_the_default(self):
        keys, problems = self.keys(
            {"interrupt": "ctrl+e", "nope": "ctrl+t", "e:Open": "ctrl+g", "cycle_mode": "shift+tab", "toggle_sidebar": "x"}
        )
        self.assertEqual((keys["interrupt"], keys["e:Open"], keys["cycle_mode"]), ("escape", "ctrl+o", "shift+tab"))
        self.assertEqual(keys["toggle_sidebar"], "ctrl+b")
        text = "\n".join(problems)
        self.assertIn("ui.keys[interrupt]: ctrl+e is reserved", text)
        self.assertIn("no key action or extension keybinding called 'nope'", text)
        self.assertIn("e:Open: key ctrl+g is already used by e:Go", text)
        self.assertIn("ui.keys[toggle_sidebar]: 'x' would steal typing", text)


class ConfigUiTest(CustomisationTest):
    def test_ui_settings_are_validated(self):
        self.write_settings(
            {
                "ui": {
                    "sidebar": "middle",
                    "sidebar_width": 10,
                    "density": "compact",
                    "prompt_glyph": "toolong",
                    "widgets": {"a": {"area": "nowhere"}, "b": {"order": "1"}, "c": {"enabled": False, "area": "left", "order": 3}},
                    "status": {"s": {"side": "right"}, "t": {"side": "top"}},
                    "keys": {"x": 1},
                    "css": "a.tcss",
                }
            }
        )
        layers, errors = config.read_layers(str(self.cwd))
        self.assertEqual(sorted(k for k in layers[0].data if k.startswith("ui.")), ["ui.css", "ui.density"])
        text = "\n".join(errors)
        for key in ("sidebar", "sidebar_width", "prompt_glyph", "widgets", "status", "keys"):
            self.assertIn(f"ui.{key} must be", text)
        self.assertEqual(len(errors), 6)

    def test_good_values_and_global_only_css(self):
        good = {"sidebar": "left", "sidebar_width": 40, "prompt_glyph": ">", "placeholder": "say", "widgets": {"w": {"order": -1}}}
        self.write_settings({"ui": {**good, "css": "x.tcss"}})
        self.write_settings({"ui": {**good, "sidebar": "hidden", "css": "evil.tcss", "widgets": {"w": {"area": "top"}}}}, "project")
        layers, errors = config.read_layers(str(self.cwd))
        self.assertEqual(errors, [f"{self.cwd / '.minimal' / 'settings.json'}: ui.css is only read from ~/.minimal/settings.json"])
        settings = config.Settings(tuple(layers))
        self.assertEqual((settings.ui_sidebar, settings.ui_sidebar_width), ("hidden", 40))
        self.assertEqual(settings.ui_css, self.home / "x.tcss")
        self.assertEqual(settings.ui_map("ui.widgets"), {"w": {"order": -1, "area": "top"}})  # merged field by field

    def test_update_global_sets_and_removes_keys_inside_a_group(self):
        self.write_settings({"theme": "x", "ui": {"density": "compact"}})
        config.update_global({"ui.sidebar": "left", "ui.density": None})
        self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {"theme": "x", "ui": {"sidebar": "left"}})
        config.update_global({"ui.sidebar": None, "ui.widgets": None})
        self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {"theme": "x"})
        config.SETTINGS_FILE.write_text(json.dumps({"ui": 5}))
        with self.assertRaisesRegex(ValueError, "ui is not a JSON object"):
            config.update_global({"ui.sidebar": "left"})


class DependsTest(CustomisationTest):
    def order(self, errors_only: bool = False):
        registry, loaded, errors = self.load()
        return [p.stem for p in loaded], errors

    def test_dependencies_load_first(self):
        self.write_ext("a_app", "def register(minimal):\n    pass\n", '__depends__ = ["z_lib"]\n')
        self.write_ext("z_lib", "def register(minimal):\n    pass\n")
        self.write_ext("m_other", "def register(minimal):\n    pass\n")
        loaded, errors = self.order()
        self.assertEqual(loaded, ["z_lib", "a_app", "m_other"])
        self.assertEqual(errors, [])

    def test_missing_failed_and_skipped_dependencies_are_precise_errors(self):
        self.write_ext("needs_missing", "def register(minimal):\n    pass\n", '__depends__ = ["ghost"]\n')
        self.write_ext("broken", "def register(minimal):\n    raise RuntimeError('boom')\n")
        self.write_ext("needs_broken", "def register(minimal):\n    pass\n", '__depends__ = ["broken"]\n')
        self.write_ext("needs_needs", "def register(minimal):\n    pass\n", '__depends__ = ["needs_missing"]\n')
        loaded, errors = self.order()
        self.assertEqual(loaded, [])
        text = "\n".join(errors)
        self.assertIn("needs_missing.py: DependencyError: needs 'ghost': install it with /ext", text)
        self.assertIn("needs_broken.py: DependencyError: needs 'broken', which did not load", text)
        self.assertIn("needs_needs.py: DependencyError: needs 'needs_missing', which did not load", text)
        self.assertIn("broken.py: RuntimeError: boom", text)

    def test_the_first_loaded_of_two_conflicting_extensions_wins_either_way_round(self):
        self.write_ext("a_one", "def register(minimal):\n    pass\n")
        self.write_ext("b_two", "def register(minimal):\n    pass\n", '__conflicts__ = ["a_one"]\n')
        loaded, errors = self.order()
        self.assertEqual(loaded, ["a_one"])
        self.assertIn("b_two.py: ConflictError: conflicts with 'a_one', which is already loaded", errors[0])
        (self.global_ext / "a_one.py").write_text(header("a", '__conflicts__ = ["b_two"]\n') + "def register(minimal):\n    pass\n")
        (self.global_ext / "b_two.py").write_text(header("b") + "def register(minimal):\n    pass\n")
        loaded, errors = self.order()
        self.assertEqual(loaded, ["a_one"])
        self.assertIn("lists it in __conflicts__", errors[0])

    def test_a_cycle_loads_neither(self):
        self.write_ext("one", "def register(minimal):\n    pass\n", '__depends__ = ["two"]\n')
        self.write_ext("two", "def register(minimal):\n    pass\n", '__depends__ = ["one"]\n')
        loaded, errors = self.order()
        self.assertEqual(loaded, [])
        self.assertEqual(len(errors), 2)
        self.assertIn("dependency cycle one -> two -> one", errors[0])

    def test_a_bundled_extension_satisfies_a_dependency(self):
        bundled = {p.stem for p in loader.category_entries(loader.BUNDLED_DIR, "extensions")[0]}
        self.assertIn("marketplace_tools", bundled)
        self.write_ext("uses_tools", "def register(minimal):\n    pass\n", '__depends__ = ["marketplace_tools"]\n')
        loaded, errors = self.order()
        self.assertEqual((loaded, errors), (["uses_tools"], []))

    def test_malformed_lists_are_errors_and_check_rejects_self_references(self):
        with self.assertRaisesRegex(ValueError, r'__depends__ must be a list of extension ids.*got \[1\]'):
            read_metadata(b"__depends__ = [1]\n", "x.py")
        with self.assertRaisesRegex(ValueError, r"__conflicts__ must be a list of extension ids.*got 'a'"):
            read_metadata(b"__conflicts__ = 'a'\n", "x.py")
        meta = read_metadata(b'__depends__ = ["a", "tools/b"]\n__conflicts__ = ("c",)\n', "x.py")
        self.assertEqual((meta.depends, meta.conflicts), (("a", "tools/b"), ("c",)))
        self.write_ext("me", "def register(minimal):\n    pass\n", '__depends__ = ["me"]\n')
        self.write_ext("both", "def register(minimal):\n    pass\n", '__depends__ = ["x"]\n__conflicts__ = ["x"]\n')
        lines, ok = marketplace.check([self.global_ext / "me.py", self.global_ext / "both.py"])
        self.assertFalse(ok)
        self.assertIn("must not list the extension itself (me)", lines[0])
        self.assertIn("x is in both __depends__ and __conflicts__", lines[1])

    def test_metadata_keeps_working_without_the_new_lists(self):
        self.assertEqual(read_metadata(b'"""d."""\n', "x.py").depends, ())
        self.assertEqual(Metadata("", "", "", False).conflicts, ())


class LayoutTest(CustomisationTest):
    def registry(self) -> Registry:
        registry = Registry()
        registry.register_widget("late", lambda ctx: None, "top", order=5)
        registry.register_widget("early", lambda ctx: None, "top")
        registry.register_panel("panel", lambda ctx: None, "top")
        registry.register_widget("lefty", lambda ctx: None, "left")
        registry.register_status("clock", lambda ctx: "t", "right", order=-1)
        registry.register_status("git", lambda ctx: "mine", "left")
        return registry

    def names(self, settings: dict, area: str = "top") -> list[str]:
        self.write_settings(settings)
        return [p.name for p in layout.resolve(self.registry(), self.settings()).in_area(area)]

    def test_order_then_registration_across_widgets_and_panels(self):
        self.assertEqual(self.names({}), ["early", "panel", "late"])

    def test_user_overrides_reorder_move_and_hide(self):
        self.assertEqual(self.names({"ui": {"widgets": {"late": {"order": -1}}}}), ["late", "early", "panel"])
        self.assertEqual(self.names({"ui": {"widgets": {"early": {"area": "footer"}}}}), ["panel", "late"])
        self.assertEqual(self.names({"ui": {"widgets": {"early": {"enabled": False}}}}), ["panel", "late"])
        self.assertEqual(self.names({"ui": {"widgets": {"panel": {"area": "left", "order": -2}}}}, "left"), ["panel", "lefty"])
        self.assertEqual(self.names({"ui": {"sidebar": "hidden", "widgets": {"early": {"area": "sidebar"}}}}, "sidebar"), [])

    def test_unknown_names_are_problems(self):
        self.write_settings({"ui": {"widgets": {"ghost": {"enabled": False}}, "status": {"ghost2": {"order": 1}}}})
        problems = layout.resolve(self.registry(), self.settings()).problems
        self.assertEqual(len(problems), 2)
        self.assertIn("ui.widgets: no widget or panel called 'ghost' (known: early, late, lefty, panel)", problems[0])

    def test_status_segments_builtin_and_extension_take_part_and_an_extension_replaces_a_builtin(self):
        self.write_settings({"ui": {"status": {"path": {"side": "right", "order": -5}, "model": {"enabled": False}}}})
        result = layout.resolve(self.registry(), self.settings())
        left = result.segments("left")
        self.assertNotIn("model", [p.name for p in left])
        self.assertEqual([p.name for p in left][0], "mode")
        self.assertEqual([p.name for p in result.segments("right")], ["path", "clock", "context", "extensions", "session"])
        git = next(p for p in left if p.name == "git")
        self.assertEqual((git.source, git.spec is not None), ("extension", True))


class RegistryOrderTest(unittest.TestCase):
    def test_order_must_be_an_integer_and_areas_include_left_and_footer(self):
        registry = Registry()
        for call in (
            lambda: registry.register_widget("w", lambda ctx: None, order="1"),  # type: ignore[arg-type]
            lambda: registry.register_panel("p", lambda ctx: None, order=True),
            lambda: registry.register_status("s", lambda ctx: None, order=1.5),  # type: ignore[arg-type]
            lambda: registry.register_widget("w", lambda ctx: None, "middle"),
        ):
            with self.assertRaises(ValueError):
                call()
        registry.register_widget("w", lambda ctx: None, "footer")
        registry.register_panel("p", lambda ctx: None, "left", order=2)
        self.assertEqual((registry.widgets["w"].area, registry.panels["p"].order), ("footer", 2))


class TuiCustomisationTest(CustomisationTest, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        super().setUp()
        self.marks = self.tmp / "marks.txt"
        patcher = mock.patch.dict("os.environ", {"MARKS": str(self.marks)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.core = App(
            registry=Registry(),
            session=Session("t", self.tmp / "t.jsonl"),
            cwd=str(self.cwd),
            auto_approve=False,
            register_commands=register_builtin_commands,
        )
        self.core.provider_name, self.core.model = "fake", "m"

    MARKER = """
        import os
        from minimal.api import theme

        def register(minimal):
            @minimal.keybinding("ctrl+o", name="mark", description="Mark the spot", show=True)
            def mark(ctx):
                with open(os.environ["MARKS"], "a") as f:
                    f.write("mark\\n")

            @minimal.keybinding("f6", description="Breaks")
            def breaks(ctx):
                raise RuntimeError("nope")
        """

    def marks_count(self) -> int:
        return len(self.marks.read_text().split()) if self.marks.exists() else 0

    def start(self) -> MinimalTui:
        self.core.reload_extensions()
        return MinimalTui(Host(self.core))

    async def wait_until(self, pilot, condition):
        for _ in range(100):
            await pilot.pause(0.05)
            if condition():
                return
        self.fail("timed out")

    async def reload(self, pilot):
        await asyncio.to_thread(self.core.reload_extensions)
        await pilot.pause()

    async def run_line(self, pilot, line):
        app = pilot.app
        app.submit(line)
        await self.wait_until(pilot, lambda: not app.busy)
        await pilot.pause()

    def errors(self, app) -> str:
        return "\n".join(str(s.render()) for s in app.query(".error").results(Static))

    def output(self, app) -> str:
        return "\n".join(str(s.render()) for s in app.query(".output").results(Static))

    async def test_a_keybinding_runs_on_a_worker_survives_reload_and_reports_errors(self):
        self.write_ext("marker", self.MARKER)
        app = self.start()
        async with app.run_test() as pilot:
            await pilot.press("ctrl+o")
            await self.wait_until(pilot, lambda: self.marks_count() == 1 and not app.busy)
            await self.reload(pilot)
            await pilot.press("ctrl+o")
            await self.wait_until(pilot, lambda: self.marks_count() == 2 and not app.busy)
            self.assertEqual(sum(1 for _, b in app._bindings if b.id and b.id.startswith("ext-key:")), 2)
            self.assertEqual(sorted(app.host.keybindings), ["ctrl+o", "f6"])
            shown = [b for _, b in app._bindings if b.key == "ctrl+o"]
            self.assertEqual([(b.show, b.description) for b in shown], [(True, "Mark the spot")])
            await pilot.press("f6")
            await self.wait_until(pilot, lambda: "RuntimeError" in self.errors(app) or "nope" in self.errors(app))
            await self.wait_until(pilot, lambda: not app.busy)
            self.assertEqual(self.errors(app).count("nope"), 1)
            self.assertIn("[extension] key f6 (marker:Breaks): nope", self.errors(app))

    async def test_a_keybinding_does_not_fire_while_busy(self):
        self.write_ext("marker", self.MARKER)
        app = self.start()
        async with app.run_test() as pilot:
            app.set_busy(True)
            await pilot.press("ctrl+o")
            await pilot.pause(0.2)
            self.assertEqual(self.marks_count(), 0)
            app.set_busy(False)

    async def test_ui_keys_rebinds_builtin_actions_and_extension_keys_and_reports_bad_ones(self):
        self.write_ext("marker", self.MARKER)
        self.write_ext("sidebar_widget", 'def register(minimal):\n    minimal.register_panel("p", lambda ctx: "x")\n')
        self.write_settings({"ui": {"keys": {"toggle_sidebar": "ctrl+t", "marker:mark": "ctrl+g", "interrupt": "ctrl+e"}}})
        app = self.start()
        async with app.run_test() as pilot:
            self.assertEqual(app.query_one("#sidebar").has_class("-collapsed"), False)
            await pilot.press("ctrl+t")
            self.assertTrue(app.query_one("#sidebar").has_class("-collapsed"))
            await pilot.press("ctrl+b")  # no longer bound
            self.assertTrue(app.query_one("#sidebar").has_class("-collapsed"))
            await pilot.press("ctrl+o")
            await pilot.pause(0.2)
            self.assertEqual(self.marks_count(), 0)
            await pilot.press("ctrl+g")
            await self.wait_until(pilot, lambda: self.marks_count() == 1)
            self.assertIn("[ui] ui.keys[interrupt]: ctrl+e is reserved", self.errors(app))

    async def test_areas_left_and_footer_mount_and_the_sidebar_can_move(self):
        self.write_ext(
            "areas",
            '''
            from textual.widgets import Static

            def register(minimal):
                minimal.register_widget("lw", lambda ctx: Static("L", id="lw"), "left")
                minimal.register_panel("fp", lambda ctx: "F", "footer")
                minimal.register_panel("sp", lambda ctx: "S", "sidebar")
            ''',
        )
        app = self.start()
        async with app.run_test() as pilot:
            self.assertEqual(app.query_one("#lw").parent, app.query_one("#left"))
            self.assertTrue(app.query_one("#left").has_class("-has-widgets"))
            self.assertEqual(len(app.query_one("#footer").children), 1)
            self.assertTrue(app.query_one("#sidebar").has_class("-has-widgets"))
            order = [w.id for w in app.query_one("#body").children]
            self.assertEqual(order, ["left", "main", "sidebar"])
            main = app.query_one("#main")
            self.assertLess(main.children.index(app.query_one("#statusline")), main.children.index(app.query_one("#footer")))
            self.write_settings({"ui": {"sidebar": "left", "sidebar_width": 44}})
            await self.reload(pilot)
            self.assertEqual([w.id for w in app.query_one("#body").children], ["left", "sidebar", "main"])
            self.assertTrue(app.query_one("#sidebar").has_class("-left"))
            self.assertEqual(app.query_one("#sidebar").styles.width.value, 44)
            self.assertEqual(app.query_one("#left").styles.width.value, 44)
            self.write_settings({"ui": {"sidebar": "hidden"}})
            await self.reload(pilot)
            self.assertEqual(len(app.query_one("#sidebar").children), 0)
            self.assertFalse(app.query_one("#sidebar").display)
            self.write_settings({})
            await self.reload(pilot)
            self.assertEqual([w.id for w in app.query_one("#body").children], ["left", "main", "sidebar"])
            self.assertEqual(len(app.query_one("#sidebar").children), 1)

    async def test_order_and_ui_widgets_and_ui_status_overrides_apply_on_reload(self):
        self.write_ext(
            "stack",
            '''
            def register(minimal):
                for name, order in (("b", 0), ("a", 0), ("c", -1)):
                    minimal.register_panel(name, lambda ctx, n=name: "panel " + n, "bottom", order=order)
                minimal.register_status("one", lambda ctx: "ONE", "left", order=1)
                minimal.register_status("two", lambda ctx: "TWO", "left")
            ''',
        )
        app = self.start()
        async with app.run_test() as pilot:
            names = lambda area: [v.spec.name for v in app.query_one(f"#{area}").children]  # noqa: E731
            self.assertEqual(names("bottom"), ["c", "b", "a"])
            status = lambda: str(app.query_one("#status-left", Static).render())  # noqa: E731
            self.assertLess(status().index("TWO"), status().index("ONE"))
            self.write_settings(
                {
                    "ui": {
                        "widgets": {"c": {"order": 9}, "a": {"area": "top"}, "b": {"enabled": False}},
                        "status": {"two": {"order": 5}, "path": {"enabled": False}, "model": {"side": "right"}},
                    }
                }
            )
            await self.reload(pilot)
            self.assertEqual(names("bottom"), ["c"])
            self.assertEqual(names("top"), ["a"])
            self.assertEqual([n for n, _ in app.ext_widgets], ["a", "c"])
            self.assertLess(status().index("ONE"), status().index("TWO"))
            self.assertNotIn(str(self.cwd.name), status())
            self.assertNotIn("m·fake", status())
            self.assertIn("m", str(app.query_one("#status-right", Static).render()))
            self.assertIn("fake", str(app.query_one("#status-right", Static).render()))

    async def test_user_css_applies_on_reload_and_a_broken_file_is_reported_and_ignored(self):
        css = self.home / "user.tcss"
        css.parent.mkdir(parents=True, exist_ok=True)
        css.write_text("#statusline { background: #123456; }\n")
        self.write_settings({"ui": {"css": "user.tcss"}})
        app = self.start()
        async with app.run_test() as pilot:
            self.assertEqual(app.query_one("#statusline").styles.background, Color.parse("#123456"))
            css.write_text("#statusline { background: #654321; }\n")
            await self.reload(pilot)
            self.assertEqual(app.query_one("#statusline").styles.background, Color.parse("#654321"))
            css.write_text("#statusline { background: nonsense-colour; }\n")
            await self.reload(pilot)
            self.assertIn("[ui] ui.css:", self.errors(app))
            self.assertIn("user.tcss was not applied: line 1", self.errors(app))
            self.assertNotEqual(app.query_one("#statusline").styles.background, Color.parse("#654321"))
            self.assertIsNone(app.user_css)
            css.write_text("#statusline { background: #111111; }\n")
            await self.reload(pilot)
            self.assertEqual(app.query_one("#statusline").styles.background, Color.parse("#111111"))
            css.unlink()
            await self.reload(pilot)
            self.assertIn("cannot read", self.errors(app))
            self.assertIsNone(app.user_css)

    async def test_compact_density_removes_the_margins_between_blocks(self):
        app = self.start()
        async with app.run_test() as pilot:
            await app._add(Static("hi", classes="user"))
            block = app.query_one(".user")
            self.assertFalse(app.has_class("-compact"))
            self.assertEqual((block.styles.margin.top, block.styles.padding.top), (1, 1))
            self.write_settings({"ui": {"density": "compact"}})
            await self.reload(pilot)
            self.assertTrue(app.has_class("-compact"))
            self.assertEqual((block.styles.margin.top, block.styles.padding.top), (0, 0))

    async def test_prompt_glyph_and_placeholder(self):
        app = self.start()
        async with app.run_test() as pilot:
            default = app.query_one("#prompt").placeholder
            self.write_settings({"ui": {"prompt_glyph": "λ", "placeholder": "say it"}})
            await self.reload(pilot)
            self.assertEqual(str(app.query_one("#prompt-glyph", Static).render()), "λ")
            self.assertEqual(app.query_one("#prompt").placeholder, "say it")
            self.write_settings({})
            await self.reload(pilot)
            self.assertEqual(app.query_one("#prompt").placeholder, default)
            self.assertNotEqual(str(app.query_one("#prompt-glyph", Static).render()), "λ")

    async def test_ui_command_prints_the_layout_and_persists_changes_that_a_reload_applies(self):
        self.write_ext(
            "stack",
            '''
            def register(minimal):
                minimal.register_panel("tasks", lambda ctx: "t", "sidebar", order=3)
                minimal.register_panel("notes", lambda ctx: "n", "bottom")
                minimal.register_status("clock", lambda ctx: "CLOCK", "right")
                minimal.register_keybinding("ctrl+o", lambda ctx: None, "Open it")
            ''',
        )
        app = self.start()
        async with app.run_test() as pilot:
            await self.run_line(pilot, "/ui")
            report = self.output(app)
            self.assertIn("sidebar right (width default), density normal", report)
            self.assertRegex(report, r"sidebar\n\s+3  tasks  panel  \[extension\]")
            self.assertRegex(report, r"right\n\s+-?\d+  clock  \[extension\]")
            self.assertIn("ctrl+o", report)
            self.assertIn("stack:Open it", report)
            self.assertIn("[built-in]", report)

            await self.run_line(pilot, "/ui sidebar left")
            await self.run_line(pilot, "/ui density compact")
            await self.run_line(pilot, "/ui hide notes")
            await self.run_line(pilot, "/ui move tasks footer 7")
            await self.run_line(pilot, "/ui hide clock")
            saved = json.loads(config.SETTINGS_FILE.read_text())
            self.assertEqual(
                saved,
                {
                    "ui": {
                        "sidebar": "left",
                        "density": "compact",
                        "widgets": {"notes": {"enabled": False}, "tasks": {"area": "footer", "order": 7}},
                        "status": {"clock": {"enabled": False}},
                    }
                },
            )
            self.assertEqual(oct(config.SETTINGS_FILE.stat().st_mode & 0o777), "0o600")
            self.assertTrue(app.has_class("-compact"))
            self.assertTrue(app.query_one("#sidebar").has_class("-left"))
            self.assertEqual([n for n, _ in app.ext_widgets], ["tasks"])
            self.assertEqual(len(app.query_one("#footer").children), 1)
            self.assertNotIn("CLOCK", str(app.query_one("#status-right", Static).render()))

            await self.run_line(pilot, "/ui")
            report = self.output(app).split("sidebar left (width default), density compact")[-1]
            self.assertIn("7  tasks  panel  [extension, user: area order]", report)
            self.assertIn("notes  panel of bottom  hidden by you  [extension, user: enabled]", report)

            await self.run_line(pilot, "/ui show notes")
            await self.run_line(pilot, "/ui reset tasks")
            saved = json.loads(config.SETTINGS_FILE.read_text())
            self.assertEqual(saved["ui"]["status"], {"clock": {"enabled": False}})
            self.assertNotIn("widgets", saved["ui"])
            self.assertEqual(sorted(n for n, _ in app.ext_widgets), ["notes", "tasks"])

            await self.run_line(pilot, "/ui reset")
            self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {})
            self.assertFalse(app.has_class("-compact"))
            self.assertIn("CLOCK", str(app.query_one("#status-right", Static).render()))

    async def test_ui_command_rejects_unknown_names_and_values_without_saving(self):
        self.write_ext("stack", 'def register(minimal):\n    minimal.register_panel("tasks", lambda ctx: "t")\n')
        app = self.start()
        async with app.run_test() as pilot:
            await self.run_line(pilot, "/ui hide nothing")
            await self.run_line(pilot, "/ui move tasks middle")
            await self.run_line(pilot, "/ui sidebar up")
            await self.run_line(pilot, "/ui move tasks top x")
            await self.run_line(pilot, "/ui bogus")
            text = self.errors(app)
            self.assertIn("no widget, panel or status segment called 'nothing'; widgets and panels: tasks; status segments: context", text)
            self.assertIn("tasks goes in one of top, bottom, sidebar, left, footer, not 'middle'", text)
            self.assertIn("/ui sidebar: use one of right, left, hidden", text)
            self.assertIn("the order must be an integer", text)
            self.assertIn("usage: /ui [sidebar", text)
            self.assertFalse(config.SETTINGS_FILE.exists())

    async def test_ui_move_of_a_status_segment_uses_sides(self):
        self.write_ext("stack", 'def register(minimal):\n    minimal.register_status("clock", lambda ctx: "CLOCK")\n')
        app = self.start()
        async with app.run_test() as pilot:
            await self.run_line(pilot, "/ui move clock left -3")
            self.assertEqual(json.loads(config.SETTINGS_FILE.read_text()), {"ui": {"status": {"clock": {"side": "left", "order": -3}}}})
            self.assertIn("CLOCK", str(app.query_one("#status-left", Static).render()))
            await self.run_line(pilot, "/ui move clock footer")
            self.assertIn("clock goes in one of left, right, not 'footer'", self.errors(app))

    async def test_a_project_ui_setting_is_applied_and_reported_by_ui(self):
        self.write_settings({"ui": {"density": "compact"}}, "project")
        app = self.start()
        async with app.run_test() as pilot:
            self.assertTrue(app.has_class("-compact"))
            await self.run_line(pilot, "/ui density normal")
            self.assertTrue(app.has_class("-compact"))  # the project layer beats the global file
            self.assertIn("A project settings file also sets ui.*", self.output(app))


class ExampleExtensionTest(CustomisationTest):
    def test_the_example_extension_binds_a_key_and_orders_a_panel(self):
        example = Path(__file__).resolve().parent.parent / "extensions" / "example.py"
        registry = Registry()
        errors: list[str] = []
        loader.load_source(loader.read_extensions([example])[0], registry, config.Settings(), "cli", errors, str(self.cwd))
        self.assertEqual(errors, [])
        self.assertEqual([(s.key, s.id) for s in registry.keybindings.values()], [("ctrl+o", "example:stats")])
        self.assertEqual(registry.panels["session"].order, -10)


if __name__ == "__main__":
    unittest.main()
