import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import config
from minimal.core.host import Host
from minimal.core.session import Session
from minimal.ui.tui import theme as tui_theme
from minimal.ui.tui.app import MinimalTui
from minimal.extensions.loader import load_extensions
from minimal.extensions.registry import THEME_COLORS, Registry
from tests.test_bundled_extensions import MARKETPLACE, BundledExtensionTest


def load_module(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def rgb(color):
    return tuple(int(color[i : i + 2], 16) for i in (1, 3, 5))


def luminance(color):
    """WCAG relative luminance, written out here independently of the extension under test."""
    linear = [c / 255 for c in rgb(color)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in linear)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a, b):
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_COLORS = (
    "foreground",
    "accent",
    "secondary",
    "success",
    "warning",
    "error",
    "muted",
    "code",
    "model",
    "path",
    "git_clean",
    "git_dirty",
    "diff_added",
    "diff_removed",
)


def user_settings(ext_id, values):
    """Settings whose ``extensions.settings.<ext_id>`` is ``values``."""
    layer = config.Layer("global", Path("settings.json"), {"extensions.settings": {ext_id: values}})
    return config.Settings(layers=(layer,))


def load_one(tmp, path, settings=None):
    registry = Registry()
    _, errors = load_extensions(registry, str(tmp), settings or config.Settings(), extra=[path])
    return registry, errors


THEME_FILES = sorted(p for p in (MARKETPLACE / "themes").glob("*.py") if p.stem != "accessible_themes")


class BundledThemesTest(BundledExtensionTest):
    def test_there_are_eleven_single_palette_themes(self):
        self.assertEqual(len(THEME_FILES), 11)

    def test_each_registers_its_own_theme_with_every_colour_and_no_load_error(self):
        for path in THEME_FILES:
            with self.subTest(theme=path.stem):
                registry, errors = load_one(self.tmp, path)
                self.assertEqual(errors, [])
                self.assertEqual(list(registry.themes), [path.stem])
                self.assertEqual(set(registry.themes[path.stem].colors), {"background", "foreground", "accent", *THEME_COLORS})
                spec = registry.declared_settings[f"themes/{path.stem}"]
                self.assertEqual(spec.defaults, {"overrides": {}})
                self.assertIn("description", spec.schema["overrides"])

    def test_dim_reads_on_the_background_and_on_raised_rows_and_pending_differs_from_success(self):
        for path in THEME_FILES:
            colors = load_one(self.tmp, path)[0].themes[path.stem].colors
            with self.subTest(theme=path.stem):
                self.assertGreaterEqual(ratio(colors["dim"], colors["background"]), 4.5)
                self.assertGreaterEqual(ratio(colors["dim"], colors["panel"]), 3.0)
                self.assertGreaterEqual(ratio(colors["muted"], colors["panel"]), 3.0)
                self.assertGreater(ratio(colors["muted"], colors["background"]), ratio(colors["dim"], colors["background"]))
                for text in ("foreground", "muted", "accent", "error", "success", "warning", "code", "model"):
                    self.assertGreaterEqual(ratio(colors[text], colors["background"]), 4.5, text)
                self.assertNotEqual(colors["pending_bg"], colors["success_bg"])
                self.assertGreaterEqual(ratio(colors["foreground"], colors["success_bg"]), 4.5)

    def test_latte_code_model_and_focus_are_legible(self):
        colors = load_one(self.tmp, MARKETPLACE / "themes" / "catppuccin_latte.py")[0].themes["catppuccin_latte"].colors
        self.assertGreaterEqual(ratio(colors["code"], colors["background"]), 4.5)
        self.assertGreaterEqual(ratio(colors["model"], colors["background"]), 4.5)
        self.assertGreaterEqual(ratio(colors["focus"], colors["background"]), 3.0)

    def test_overrides_replace_single_colours(self):
        for path in THEME_FILES:
            with self.subTest(theme=path.stem):
                base = load_one(self.tmp, path)[0].themes[path.stem]
                settings = user_settings(f"themes/{path.stem}", {"overrides": {"accent": "#FF8800", "dim": "#123456"}})
                registry, errors = load_one(self.tmp, path, settings)
                self.assertEqual(errors, [])
                colors = registry.themes[path.stem].colors
                self.assertEqual((colors["accent"], colors["dim"]), ("#ff8800", "#123456"))
                self.assertEqual(colors["background"], base.colors["background"])
                self.assertEqual(registry.themes[path.stem].dark, base.dark)

    def test_bad_overrides_are_load_errors_that_keep_the_valid_ones_and_never_crash(self):
        path = MARKETPLACE / "themes" / "dracula.py"
        overrides = {"accent": "#ff8800", "nope": "#ffffff", "dark": "#000000", "error": "red", "muted": 5}
        registry, errors = load_one(self.tmp, path, user_settings("themes/dracula", {"overrides": overrides}))
        self.assertEqual(len(errors), 1)
        for fragment in ("themes/dracula.overrides", "unknown colour 'nope'", "unknown colour 'dark'", "error must be #rrggbb", "muted must be #rrggbb"):
            self.assertIn(fragment, errors[0])
        self.assertEqual(registry.themes["dracula"].colors["accent"], "#ff8800")
        self.assertEqual(registry.themes["dracula"].colors["error"], load_one(self.tmp, path)[0].themes["dracula"].colors["error"])

    def test_overrides_must_be_an_object(self):
        registry, errors = load_one(self.tmp, MARKETPLACE / "themes" / "nord.py", user_settings("themes/nord", {"overrides": ["accent"]}))
        self.assertEqual(len(errors), 1)
        self.assertIn("extensions.settings.themes/nord", errors[0])
        self.assertIn("dict", errors[0])
        self.assertIn("nord", registry.themes)

    def test_accessible_themes_override_per_theme(self):
        path = MARKETPLACE / "themes" / "accessible_themes.py"
        settings = user_settings("themes/accessible_themes", {"overrides": {"high_contrast_dark": {"accent": "#ffffff"}}})
        registry, errors = load_one(self.tmp, path, settings)
        self.assertEqual(errors, [])
        self.assertEqual(registry.themes["high_contrast_dark"].colors["accent"], "#ffffff")
        self.assertEqual(registry.themes["high_contrast_light"].colors["accent"], "#0033b3")
        bad = {"overrides": {"sepia": {}, "high_contrast_light": {"accent": "blue", "glow": "#fff"}}}
        registry, errors = load_one(self.tmp, path, user_settings("themes/accessible_themes", bad))
        self.assertEqual(len(errors), 1)
        for fragment in ("unknown theme 'sepia'", "accent must be #rrggbb", "unknown colour 'glow'"):
            self.assertIn(fragment, errors[0])
        self.assertEqual(sorted(registry.themes), ["high_contrast_dark", "high_contrast_light"])


class ThemesInTheTuiTest(BundledExtensionTest, unittest.IsolatedAsyncioTestCase):
    async def test_every_theme_builds_and_each_can_be_selected_in_the_running_ui(self):
        paths = [*THEME_FILES, MARKETPLACE / "themes" / "accessible_themes.py"]
        core = self.make_app("themes/dracula.py", Session("t", self.tmp / "t.jsonl"), [])
        for path in paths:
            core.registry.themes.update(load_one(self.tmp, path)[0].themes)
        tui = MinimalTui(Host(core))
        async with tui.run_test() as pilot:
            for name in core.registry.themes:
                with self.subTest(theme=name):
                    tui.register_theme(tui_theme.available(core.registry.themes)[name])
                    tui.theme = name
                    await pilot.pause()
                    self.assertEqual(tui.theme, name)
                    shown = rgb(tui.theme_variables["accent"].lower())  # Textual rounds a channel by one at most
                    for got, want in zip(shown, rgb(core.registry.themes[name].colors["accent"])):
                        self.assertLessEqual(abs(got - want), 1)


class CheckItemsGateTest(unittest.TestCase):
    """``scripts/check_items.py``' contrast gate, run as the marketplace's CI runs it."""

    script = MARKETPLACE / "scripts" / "check_items.py"

    def setUp(self):
        self.module = load_module(self.script)

    def test_the_marketplace_passes(self):
        done = subprocess.run([sys.executable, str(self.script)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue(done.stdout.startswith("ok:"))

    def test_every_theme_item_is_read_without_importing_it(self):
        found = {item.stem: self.module.theme_palettes((self.module.ROOT / item).read_text()) for item in self.module.items() if item.parts[0] == "themes"}
        self.assertEqual(len(found["accessible_themes"]), 2)
        for stem, palettes in found.items():
            self.assertTrue(palettes, stem)
            for name, colors in palettes:
                self.assertRegex(colors["background"], "^#[0-9a-f]{6}$")

    def test_floors_and_a_weak_theme(self):
        weak = {"dark": True, "background": "#000000", "foreground": "#ffffff", "accent": "#222222", "dim": "#333333"}
        problems = self.module.check_theme("t", weak)
        joined = "\n".join(problems)
        for fragment in ("accent #222222 on background", "dim #333333 on background", "dim #333333 on panel", "pending_bg and success_bg"):
            self.assertIn(fragment, joined)
        self.assertNotIn("foreground", joined)

    def test_a_light_theme_without_state_colours_gets_minimals_defaults(self):
        colors = {"dark": False, "background": "#ffffff", "foreground": "#000000", "accent": "#0033b3", "dim": "#595959"}
        self.assertEqual([p for p in self.module.check_theme("t", colors) if "pending_bg" not in p], [])

    def test_the_ratios_match_the_wcag_formula(self):
        self.assertAlmostEqual(self.module.contrast("#000000", "#ffffff"), 21.0)
        self.assertAlmostEqual(self.module.contrast("#777777", "#ffffff"), ratio("#777777", "#ffffff"))

    def test_non_literal_colours_are_reported(self):
        self.assertEqual(
            self.module.check_theme("t", {"background": "#000000", "foreground": "white", "accent": "#ffffff"}),
            ["t: foreground must be a #rrggbb literal"],
        )

    def test_a_failing_theme_fails_the_script(self):
        import shutil
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "scripts").mkdir()
            shutil.copy(self.script, root / "scripts" / "check_items.py")
            (root / "themes").mkdir()
            (root / "README.md").write_text("`themes/bad.py`")
            (root / "themes" / "bad.py").write_text(
                'def register(minimal):\n    minimal.register_theme("bad", dark=True, background="#000000", foreground="#ffffff", '
                'accent="#ffffff", dim="#333333")\n'
            )
            done = subprocess.run([sys.executable, str(root / "scripts" / "check_items.py")], capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("themes/bad.py [bad]: dim #333333 on background #000000", done.stdout)


class AccessibleThemesTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.registry = Registry()
        _, errors = load_extensions(
            self.registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "themes" / "accessible_themes.py"]
        )
        self.assertEqual(errors, [])

    def test_registers_a_dark_and_a_light_theme(self):
        themes = self.registry.themes
        self.assertEqual(sorted(themes), ["high_contrast_dark", "high_contrast_light"])
        self.assertTrue(themes["high_contrast_dark"].dark)
        self.assertFalse(themes["high_contrast_light"].dark)
        for spec in themes.values():
            self.assertEqual(set(spec.colors), {"background", "foreground", "accent", *THEME_COLORS})

    def test_every_text_colour_reaches_aa_on_every_background_it_sits_on(self):
        for name, spec in self.registry.themes.items():
            colors = spec.colors
            for text in (*TEXT_COLORS, "dim"):
                for background in ("background", "surface", "panel", "user_bg", "pending_bg", "status_bg"):
                    with self.subTest(theme=name, text=text, background=background):
                        self.assertGreaterEqual(ratio(colors[text], colors[background]), 4.5)

    def test_text_on_state_and_selection_backgrounds_and_ui_lines(self):
        for name, spec in self.registry.themes.items():
            colors = spec.colors
            for background in ("success_bg", "error_bg", "selection"):
                with self.subTest(theme=name, background=background):
                    self.assertGreaterEqual(ratio(colors["foreground"], colors[background]), 4.5)
            for line in ("border", "focus"):  # non-text contrast: WCAG 1.4.11 asks for 3:1
                with self.subTest(theme=name, line=line):
                    self.assertGreaterEqual(ratio(colors[line], colors["background"]), 3.0)


class ThemeBuilderTest(BundledExtensionTest):
    def setUp(self):
        super().setUp()
        self.home = self.tmp / "home"
        patcher = mock.patch.object(config, "HOME_DIR", self.home)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.module = load_module(MARKETPLACE / "extensions" / "theme_builder.py")

    def test_every_theme_has_the_registry_keys_and_readable_text_for_any_accent(self):
        accents = ["#000000", "#ffffff", "#808080", "#ff0000", "#00ff00", "#0000ff", "#ffff00", "#4fc3f7", "#123", "f0a"]
        accents += [f"#{c:02x}{c * 3 % 256:02x}{c * 7 % 256:02x}" for c in range(0, 256, 17)]
        for accent in accents:
            for mode in ("dark", "light"):
                theme = self.module.build_theme(accent, mode)
                self.assertEqual(theme.pop("dark"), mode == "dark")
                self.assertEqual(set(theme), {"background", "foreground", "accent", *THEME_COLORS})
                for text in (*TEXT_COLORS, "muted"):
                    for background in ("background", "surface", "panel", "user_bg"):
                        with self.subTest(accent=accent, mode=mode, text=text, background=background):
                            self.assertGreaterEqual(ratio(theme[text], theme[background]), 4.5)
                with self.subTest(accent=accent, mode=mode, text="dim"):
                    self.assertGreaterEqual(ratio(theme["dim"], theme["panel"]), 4.5)
                    self.assertGreaterEqual(ratio(theme["dim"], theme["background"]), 4.5)
                with self.subTest(accent=accent, mode=mode, text="border"):
                    self.assertGreaterEqual(ratio(theme["border"], theme["background"]), 3.0)

    def test_accent_that_already_reads_is_kept_and_one_that_does_not_is_adjusted(self):
        self.assertEqual(self.module.build_theme("#4fc3f7", "dark")["accent"], "#4fc3f7")
        self.assertEqual(self.module.build_theme("#4fc3f7", "light")["accent"] != "#4fc3f7", True)
        self.assertEqual(self.module.build_theme("#0b1622", "dark")["accent"] != "#0b1622", True)

    def test_dark_theme_is_dark_and_light_theme_is_light(self):
        dark, light = self.module.build_theme("#4fc3f7", "dark"), self.module.build_theme("#4fc3f7", "light")
        self.assertLess(luminance(dark["background"]), 0.05)
        self.assertGreater(luminance(light["background"]), 0.8)

    def test_hex_forms_and_bad_colours(self):
        self.assertEqual(self.module.parse_hex("#4FC3F7"), (79, 195, 247))
        self.assertEqual(self.module.parse_hex("4fc3f7"), (79, 195, 247))
        self.assertEqual(self.module.parse_hex("#fa0"), (255, 170, 0))
        self.assertEqual(self.module.parse_hex(" #4fc3f7 "), (79, 195, 247))
        for bad in ("", "#", "#12345", "#1234567", "#gggggg", "blue", "##4fc3f7", "rgb(1,2,3)", "#4fc3f7\n#fff"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "not a hex colour.*#rrggbb"):
                self.module.parse_hex(bad)

    def test_build_theme_rejects_a_bad_mode_and_a_bad_accent(self):
        with self.assertRaisesRegex(ValueError, "dark or light"):
            self.module.build_theme("#4fc3f7", "sepia")
        with self.assertRaisesRegex(ValueError, "not a hex colour"):
            self.module.build_theme("teal", "dark")

    def test_parse_args(self):
        self.assertEqual(self.module.parse_args("ocean #4FC3F7"), ("ocean", "#4fc3f7", "dark", {}))
        self.assertEqual(self.module.parse_args("  sun_2   fa0   LIGHT "), ("sun_2", "#ffaa00", "light", {}))
        self.assertEqual(
            self.module.parse_args("ocean #4fc3f7 --error #F00 light --secondary=#00f"),
            ("ocean", "#4fc3f7", "light", {"error": "#ff0000", "secondary": "#0000ff"}),
        )
        for bad, message in (
            ("", "usage"),
            ("ocean", "usage"),
            ("a #fff dark extra", "usage"),
            ("Ocean #fff", "lowercase"),
            ("-x #fff", "lowercase"),
            ("o" * 33 + " #fff", "lowercase"),
            ("minimal #fff", "cannot be replaced"),
            ("ocean nope", "not a hex colour"),
            ("ocean #fff blue", "dark or light"),
            ("list #fff", "subcommand"),
            ("ocean #fff --glow #fff", "unknown option"),
            ("ocean #fff --error", "needs a colour"),
            ("ocean #fff --error red", "not a hex colour"),
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, message):
                    self.module.parse_args(bad)

    def test_saved_file_round_trip_and_corruption(self):
        path = self.tmp / "themes.json"
        self.assertEqual(self.module.load_saved(path), {})
        self.module.save(path, {"b": ("#4fc3f7", "dark", {}), "a": ("#ffaa00", "light", {"error": "#ff0000"})})
        self.assertEqual(list(json.loads(path.read_text())), ["a", "b"])
        self.assertEqual(self.module.load_saved(path), {"a": ("#ffaa00", "light", {"error": "#ff0000"}), "b": ("#4fc3f7", "dark", {})})
        for content in ("{", "[]", '{"x": 1}', '{"x": {"accent": "teal", "mode": "dark"}}', '{"X": {"accent": "#fff", "mode": "dark"}}'):
            path.write_text(content)
            with self.subTest(content=content), self.assertRaisesRegex(ValueError, "themes.json.*delete it"):
                self.module.load_saved(path)

    def load_extension(self):
        registry = Registry()
        _, errors = load_extensions(
            registry, str(self.tmp), config.Settings(), extra=[MARKETPLACE / "extensions" / "theme_builder.py"]
        )
        self.assertEqual(errors, [])
        return registry

    def test_command_saves_reloads_and_the_theme_is_registered_by_every_later_load(self):
        registry = self.load_extension()
        self.assertEqual(registry.themes, {})
        reloaded = []

        def reload():
            reloaded.append(self.load_extension())  # what ctx.reload() does: rebuild the registry from disk
            return "Loaded 1 extension"

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            registry.commands["theme_builder"].run("ocean #4fc3f7 dark", SimpleNamespace(reload=reload))
        self.assertEqual(len(reloaded), 1)
        self.assertIn("/theme ocean", out.getvalue())
        ocean = reloaded[0].themes["ocean"]
        self.assertTrue(ocean.dark)
        self.assertEqual(ocean.colors["accent"], "#4fc3f7")
        with contextlib.redirect_stdout(io.StringIO()):
            registry.commands["theme_builder"].run("sun #ffaa00 light", SimpleNamespace(reload=reload))
        self.assertEqual(sorted(self.load_extension().themes), ["ocean", "sun"])

    def test_settings_shape_the_generated_themes_and_are_validated(self):
        registry = self.load_extension()
        spec = registry.declared_settings["theme_builder"]
        self.assertEqual(
            spec.defaults, {"text_ratio": 4.5, "dim_ratio": 4.5, "secondary_hue_offset": 40, "bg_saturation": 0.5}
        )
        self.assertEqual(set(spec.schema), set(spec.defaults))
        self.assertTrue(all("description" in entry for entry in spec.schema.values()))
        for key, value in (("text_ratio", 99.0), ("dim_ratio", "high"), ("secondary_hue_offset", 400), ("bg_saturation", -1.0)):
            _, errors = load_one(self.tmp, MARKETPLACE / "extensions" / "theme_builder.py", user_settings("theme_builder", {key: value}))
            self.assertEqual(len(errors), 1, key)
            self.assertIn(f"extensions.settings.theme_builder.{key}", errors[0])

    def test_text_ratio_dim_ratio_hue_offset_and_saturation_change_the_palette(self):
        base = self.module.build_theme("#4fc3f7", "dark")
        strong = self.module.build_theme("#4fc3f7", "dark", text_ratio=8.0, dim_ratio=6.0)
        for text in ("accent", "error", "success", "muted", "foreground"):
            self.assertGreaterEqual(ratio(strong[text], strong["panel"]), 8.0 if text != "foreground" else 7.0)
        self.assertGreaterEqual(ratio(strong["dim"], strong["panel"]), 6.0)
        self.assertGreater(ratio(strong["dim"], strong["background"]), ratio(base["dim"], base["background"]))
        self.assertEqual(base["dim"] != strong["dim"], True)
        self.assertNotEqual(self.module.build_theme("#4fc3f7", secondary_hue_offset=120)["secondary"], base["secondary"])
        self.assertEqual(self.module.build_theme("#4fc3f7", secondary_hue_offset=0)["secondary"], base["accent"])
        grey = self.module.build_theme("#4fc3f7", bg_saturation=0.0)["background"]
        self.assertEqual(len(set(rgb(grey))), 1)
        self.assertGreater(len(set(rgb(base["background"]))), 1)

    def test_default_dim_reads_at_4_5_on_background_and_panel_for_any_accent(self):
        for accent in ("#000000", "#ffffff", "#ff0000", "#4fc3f7", "#808080"):
            for mode in ("dark", "light"):
                theme = self.module.build_theme(accent, mode)
                self.assertGreaterEqual(ratio(theme["dim"], theme["background"]), 4.5, (accent, mode))
                self.assertGreater(ratio(theme["muted"], theme["background"]), ratio(theme["dim"], theme["background"]) - 0.01)

    def test_secondary_and_error_options_replace_the_derived_hues_and_still_read(self):
        theme = self.module.build_theme("#4fc3f7", "dark", colors={"secondary": "#00ff00", "error": "#0000ff"})
        default = self.module.build_theme("#4fc3f7", "dark")
        self.assertEqual(theme["secondary"], "#00ff00")
        self.assertEqual(theme["path"], theme["secondary"])
        self.assertNotEqual(theme["error"], default["error"])
        self.assertNotEqual(theme["error"], "#0000ff")  # lightened until readable
        self.assertEqual(theme["diff_removed"], theme["error"])
        self.assertGreaterEqual(ratio(theme["error"], theme["panel"]), 4.5)
        with self.assertRaisesRegex(ValueError, "unknown colour option"):
            self.module.build_theme("#4fc3f7", colors={"glow": "#fff"})
        with self.assertRaisesRegex(ValueError, "not a hex colour"):
            self.module.build_theme("#4fc3f7", colors={"error": "red"})

    def run_command(self, registry, args, reload=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            registry.commands["theme_builder"].run(args, SimpleNamespace(reload=reload or (lambda: "")))
        return out.getvalue()

    def test_list_delete_and_export(self):
        registry = self.load_extension()
        self.assertIn("No saved themes", self.run_command(registry, "list"))
        self.run_command(registry, "ocean #4fc3f7 dark --error #ff6b6b")
        self.run_command(registry, "sun-day #ffaa00 light")
        listing = self.run_command(registry, "list")
        self.assertIn("ocean: #4fc3f7 dark --error #ff6b6b", listing)
        self.assertIn("sun-day: #ffaa00 light", listing)
        self.assertEqual(self.load_extension().themes["ocean"].colors["error"] != self.module.build_theme("#4fc3f7")["error"], True)

        message = self.run_command(registry, "export sun-day")
        (exported,) = self.home.rglob("sun_day.py")
        self.assertEqual(exported.parent.name, "themes")
        self.assertIn(str(exported), message)

        reloads = []
        self.run_command(registry, "delete ocean", reload=lambda: reloads.append(1) or "")
        self.assertEqual(reloads, [1])
        self.assertEqual(sorted(self.load_extension().themes), ["sun-day"])
        for bad in ("delete ocean", "export ocean"):
            with self.assertRaisesRegex(ValueError, "no saved theme 'ocean'.*sun-day"):
                self.run_command(registry, bad)
        for bad in ("list now", "delete", "export a b"):
            with self.assertRaisesRegex(ValueError, "usage"):
                self.run_command(registry, bad)

    def test_an_exported_theme_is_a_standalone_extension_that_passes_the_gate(self):
        registry = self.load_extension()
        self.run_command(registry, "ocean #4fc3f7 dark --secondary #ff9e64")
        self.run_command(registry, "export ocean")
        (exported,) = self.home.rglob("ocean.py")
        built = self.module.build_theme("#4fc3f7", "dark", colors={"secondary": "#ff9e64"})
        copy = self.tmp / "themes" / "ocean.py"
        copy.parent.mkdir()
        copy.write_text(exported.read_text())
        loaded, errors = load_one(self.tmp, copy)
        self.assertEqual(errors, [])
        self.assertEqual(loaded.themes["ocean"].colors, {k: v for k, v in built.items() if k != "dark"})
        self.assertTrue(loaded.themes["ocean"].dark)
        overridden, errors = load_one(self.tmp, copy, user_settings("themes/ocean", {"overrides": {"accent": "#010203"}}))
        self.assertEqual((errors, overridden.themes["ocean"].colors["accent"]), ([], "#010203"))
        gate = load_module(MARKETPLACE / "scripts" / "check_items.py")
        ((name, colors),) = gate.theme_palettes(exported.read_text())
        self.assertEqual(name, "ocean")
        self.assertEqual(gate.check_theme("ocean", colors), [])

    def test_command_says_when_the_accent_was_adjusted(self):
        registry = self.load_extension()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            registry.commands["theme_builder"].run("navy #0b1622", SimpleNamespace(reload=lambda: ""))
        self.assertIn("accent adjusted to", out.getvalue())

    def test_command_shows_reload_failures(self):
        registry = self.load_extension()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            registry.commands["theme_builder"].run(
                "ocean #4fc3f7", SimpleNamespace(reload=lambda: "Loaded 0\nFailed: other: boom")
            )
        self.assertIn("Failed: other: boom", out.getvalue())

    def test_bad_input_saves_nothing_and_does_not_reload(self):
        registry = self.load_extension()
        ctx = SimpleNamespace(reload=lambda: self.fail("reloaded"))
        with self.assertRaisesRegex(ValueError, "not a hex colour"):
            registry.commands["theme_builder"].run("ocean blue", ctx)
        self.assertEqual(list(self.home.rglob("themes.json")), [])

    def test_an_unreadable_saved_file_is_kept_and_does_not_break_loading(self):
        registry = self.load_extension()
        with contextlib.redirect_stdout(io.StringIO()):
            registry.commands["theme_builder"].run("ocean #4fc3f7", SimpleNamespace(reload=lambda: ""))
        (saved,) = self.home.rglob("themes.json")
        saved.write_text("{")
        self.assertEqual(self.load_extension().themes, {})
        with self.assertRaisesRegex(ValueError, "themes.json.*delete it"):
            registry.commands["theme_builder"].run("sun #ffaa00", SimpleNamespace(reload=lambda: self.fail("reloaded")))
        self.assertEqual(saved.read_text(), "{")


if __name__ == "__main__":
    unittest.main()
