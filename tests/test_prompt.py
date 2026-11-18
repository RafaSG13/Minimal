import contextlib
import io
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

import minimal.core.context
import minimal.extensions.facade
from minimal import config
from minimal.core.app import App, build_system_prompt
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import PROMPT_SECTIONS, Registry
from minimal.ui.commands import register_builtin_commands

GOLDEN = Path(__file__).with_name("golden") / "default_prompt.txt"
"""The default system prompt with machine-specific values written as ``<TOKEN>``.

After an intended change to ``core/prompt.md`` or ``SELF_EXTEND_PROMPT``, regenerate it:
``PINNED_PROMPT_WRITE=1 uv run python -m unittest tests.test_prompt``.
"""


class DefaultPromptTest(unittest.TestCase):
    """The composed default prompt is pinned byte for byte, whatever the code that composes it."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.cwd = self.tmp / "project"
        self.cwd.mkdir()
        patcher = mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.tmp / "global")
        patcher.start()
        self.addCleanup(patcher.stop)

    def normalised(self, prompt: str) -> str:
        """``prompt`` with the paths and the date of this machine and run replaced by tokens."""
        for value, token in (
            (str(Path(minimal.extensions.facade.__file__)), "<FACADE>"),
            (str(Path(minimal.core.context.__file__)), "<CONTEXT>"),
            (str(self.tmp / "global"), "<GLOBAL>"),
            (str(self.cwd), "<CWD>"),
            (datetime.now(timezone.utc).date().isoformat(), "<DATE>"),
        ):
            prompt = prompt.replace(value, token)
        return prompt

    def test_default_prompt_is_unchanged(self):
        (self.cwd / "AGENTS.md").write_text("Use tabs.\n", encoding="utf-8")
        (self.cwd / "NOTES.md").write_text("Second file.", encoding="utf-8")
        layer = config.Layer(
            "global",
            self.tmp / "settings.json",
            {"prompt.append": ["First appended.", "Second appended."], "prompt.files": ["AGENTS.md", "NOTES.md"]},
        )
        registry = Registry()
        registry.append_system_prompt("From an extension.")
        registry.append_system_prompt("From another extension.")
        prompt = self.normalised(build_system_prompt(registry, str(self.cwd), config.Settings((layer,))))
        if os.environ.get("PINNED_PROMPT_WRITE"):
            GOLDEN.parent.mkdir(exist_ok=True)
            GOLDEN.write_text(prompt, encoding="utf-8")
        self.assertEqual(prompt, GOLDEN.read_text(encoding="utf-8"))

    def test_default_prompt_without_extras_is_the_base_environment_and_extending_parts(self):
        prompt = build_system_prompt(Registry(), str(self.cwd), config.Settings())
        parts = prompt.split("\n\n")
        self.assertTrue(parts[0].startswith("You are a coding agent working in the user's terminal."))
        self.assertTrue(prompt.rstrip().endswith("call reload_extensions and fix any reported errors."))
        self.assertEqual(prompt, prompt.strip())
        self.assertEqual(sum(p.startswith("Working directory: ") for p in parts), 1)


class PromptSectionsTest(unittest.TestCase):
    """Extensions add, replace and remove named sections of the system prompt."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        self.registry = Registry()
        patcher = mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", Path(tmp.name) / "global")
        patcher.start()
        self.addCleanup(patcher.stop)

    def build(self, ctx=None, settings=None):
        return build_system_prompt(self.registry, self.cwd, settings or config.Settings(), ctx)

    def test_the_built_in_sections_have_these_names_in_order(self):
        self.assertEqual(PROMPT_SECTIONS, ("base", "environment", "extending", "instructions", "append"))
        self.assertEqual(self.registry.prompt_section_names(), list(PROMPT_SECTIONS))

    def test_a_new_section_goes_after_the_built_in_ones_and_after_earlier_additions(self):
        self.registry.register_prompt_section("first", "FIRST")
        self.registry.append_system_prompt("PARAGRAPH")
        self.registry.register_prompt_section("second", "SECOND")
        parts = self.build().split("\n\n")
        self.assertEqual(parts[-3:], ["FIRST", "PARAGRAPH", "SECOND"])
        self.assertEqual(self.registry.prompt_section_names()[-3:], ["first", "append:2", "second"])

    def test_replace_takes_the_place_of_a_built_in_section(self):
        self.registry.register_prompt_section("my_base", "You are terse.", replace="base")
        prompt = self.build()
        self.assertTrue(prompt.startswith("You are terse.\n\nWorking directory: "))
        self.assertNotIn("You are a coding agent", prompt)
        self.assertIn("You can extend minimal", prompt)
        self.assertEqual(self.registry.prompt_section_names()[0], "my_base")

    def test_replace_works_on_a_section_an_extension_added_and_on_a_replacement(self):
        self.registry.register_prompt_section("x", "X1")
        self.registry.register_prompt_section("y", "Y1", replace="x")
        self.registry.register_prompt_section("z", "Z1", replace="y")
        self.assertTrue(self.build().endswith("\n\nZ1"))
        self.assertNotIn("X1", self.build() + "Y1")
        self.assertEqual(self.registry.prompt_section_names()[-1], "z")

    def test_registering_an_existing_name_replaces_its_text_in_place(self):
        self.registry.register_prompt_section("x", "X1")
        self.registry.register_prompt_section("y", "Y1")
        self.registry.register_prompt_section("x", "X2")
        self.assertEqual(self.build().split("\n\n")[-2:], ["X2", "Y1"])
        self.registry.register_prompt_section("base", "Only me.")
        self.assertTrue(self.build().startswith("Only me.\n\nWorking directory"))

    def test_remove_drops_a_built_in_or_an_added_section(self):
        self.registry.register_prompt_section("x", "X1")
        self.registry.remove_prompt_section("extending")
        self.registry.remove_prompt_section("x")
        prompt = self.build()
        self.assertNotIn("You can extend minimal", prompt)
        self.assertNotIn("X1", prompt)
        self.assertEqual(self.registry.prompt_section_names(), ["base", "environment", "instructions", "append"])
        self.registry.register_prompt_section("x", "X3")  # a removed name can be used again
        self.assertTrue(self.build().endswith("\n\nX3"))

    def test_a_removed_section_is_never_computed(self):
        settings = config.Settings((config.Layer("global", Path(self.cwd, "s.json"), {"prompt.base": "/no/such/file"}),))
        with self.assertRaisesRegex(ValueError, r"prompt\.base\) not found"):
            self.build(settings=settings)
        self.registry.register_prompt_section("mine", "Mine.", replace="base")
        self.assertTrue(self.build(settings=settings).startswith("Mine."))

    def test_text_can_be_a_function_of_ctx_and_an_empty_result_leaves_the_section_out(self):
        self.registry.register_prompt_section("who", lambda ctx: f"ctx is {ctx}")
        self.registry.register_prompt_section("nothing", lambda ctx: None)
        self.registry.register_prompt_section("blank", "")
        prompt = self.build(ctx="the context")
        self.assertTrue(prompt.endswith("\n\nctx is the context"))
        self.assertNotIn("\n\n\n", prompt)

    def test_a_text_function_that_raises_is_reported_and_skipped(self):
        def boom(ctx):
            raise RuntimeError("no")

        self.registry.register_prompt_section("boom", boom)
        self.registry.register_prompt_section("after", "AFTER")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            prompt = self.build()
        self.assertTrue(prompt.endswith("\n\nAFTER"))
        self.assertIn('prompt section "boom": no', err.getvalue())

    def test_invalid_registrations_raise_value_error(self):
        register, remove = self.registry.register_prompt_section, self.registry.remove_prompt_section
        for args in (("", "t"), ("x", 3), ("x", None)):
            with self.assertRaisesRegex(ValueError, "register_prompt_section"):
                register(*args)
        with self.assertRaisesRegex(ValueError, "no section 'nope' to replace.*base"):
            register("x", "t", replace="nope")
        with self.assertRaisesRegex(ValueError, "'append' is already a section"):
            register("append", "t", replace="base")
        with self.assertRaisesRegex(ValueError, "no section 'nope'"):
            remove("nope")
        self.assertEqual(self.registry.prompt_edits, [])  # nothing half-applied

    def test_reset_forgets_every_edit(self):
        self.registry.register_prompt_section("x", "X1")
        self.registry.remove_prompt_section("base")
        self.registry.reset()
        self.assertEqual(self.registry.prompt_section_names(), list(PROMPT_SECTIONS))

    def test_an_extension_edits_sections_through_the_facade_and_ctx_lists_them(self):
        ext_dir = Path(self.cwd, "global")
        ext_dir.mkdir()
        (ext_dir / "tweak.py").write_text(
            '__api__ = "2.2"\n'
            "def register(minimal):\n"
            '    minimal.register_prompt_section("rules", lambda ctx: "Rules for " + ctx.cwd, replace="extending")\n'
            '    minimal.remove_prompt_section("environment")\n'
        )
        with mock.patch.object(config, "SETTINGS_FILE", Path(self.cwd) / "settings.json"):
            app = App(
                registry=self.registry,
                session=Session("t", Path(self.cwd) / "t.jsonl"),
                cwd=self.cwd,
                auto_approve=True,
                register_commands=register_builtin_commands,
            )
            self.assertEqual(app.reload_extensions()[1], [])
        # the bundled marketplace_tools extension appends its own paragraph after these
        self.assertEqual(app.context.prompt_sections()[:4], ["base", "rules", "instructions", "append"])
        prompt = app.system_prompt()
        self.assertIn(f"\n\nRules for {self.cwd}", prompt)
        self.assertNotIn("Working directory", prompt)
        self.assertNotIn("You can extend minimal", prompt)
