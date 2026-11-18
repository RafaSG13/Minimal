import contextlib
import importlib.util
import io
import json
import unittest
from types import SimpleNamespace
from unittest import mock

from minimal import config
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
                    self.assertGreaterEqual(ratio(theme["dim"], theme["panel"]), 3.0)
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
        self.assertEqual(self.module.parse_args("ocean #4FC3F7"), ("ocean", "#4fc3f7", "dark"))
        self.assertEqual(self.module.parse_args("  sun_2   fa0   LIGHT "), ("sun_2", "#ffaa00", "light"))
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
        ):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, message):
                    self.module.parse_args(bad)

    def test_saved_file_round_trip_and_corruption(self):
        path = self.tmp / "themes.json"
        self.assertEqual(self.module.load_saved(path), {})
        self.module.save(path, {"b": ("#4fc3f7", "dark"), "a": ("#ffaa00", "light")})
        self.assertEqual(list(json.loads(path.read_text())), ["a", "b"])
        self.assertEqual(self.module.load_saved(path), {"a": ("#ffaa00", "light"), "b": ("#4fc3f7", "dark")})
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
