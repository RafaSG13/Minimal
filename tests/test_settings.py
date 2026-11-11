import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import config
from minimal.cli import start_provider
from minimal.core.app import App, build_system_prompt
from minimal.core.session import Session
from minimal.extensions import loader
from minimal.extensions.registry import Registry
from minimal.providers.base import ProviderSpec


class SettingsTest(unittest.TestCase):
    """A temporary ``~/.minimal`` and project folder; settings files written as JSON into them."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = Path(tmp.name, "home")
        self.home.mkdir()
        self.cwd = Path(tmp.name, "project")
        (self.cwd / ".minimal").mkdir(parents=True)
        for patcher in (
            mock.patch.object(config, "HOME_DIR", self.home),
            mock.patch.object(config, "SETTINGS_FILE", self.home / "settings.json"),
            mock.patch.object(config, "KEYS_FILE", self.home / "keys.json"),
            mock.patch.object(config, "TRUST_FILE", self.home / "trusted.json"),
            mock.patch.object(loader, "GLOBAL_EXTENSIONS_DIR", self.home / "extensions"),
            mock.patch.dict(os.environ),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for var in ("MINIMAL_PROVIDER", "MINIMAL_MODEL"):
            os.environ.pop(var, None)

    def write(self, scope, data):
        path = config.settings_files(str(self.cwd))[scope]
        path.write_text(data if isinstance(data, str) else json.dumps(data))
        return path

    def settings(self, trusted=False):
        layers, errors = config.read_layers(str(self.cwd))
        return config.Settings(tuple(layers), trusted), errors

    def test_highest_layer_wins_and_cli_beats_every_layer(self):
        self.write("global", {"theme": "a", "provider": "p1"})
        self.write("project", {"theme": "b", "provider": "p2"})
        self.write("local", {"theme": "c", "provider": "p2", "model": "local-model"})
        settings, errors = self.settings()
        self.assertEqual(errors, [])
        self.assertEqual(settings.theme, "c")
        self.assertEqual(settings.scalar("theme"), ("c", "local"))

        registry = Registry()
        for name in ("p1", "p2", "p3"):
            registry.add_provider(ProviderSpec(name=name, create=object, default_model=f"{name}-default"))
        app = App(registry=registry, session=Session("t", self.home / "t.jsonl"), cwd=str(self.cwd), auto_approve=True)
        app.settings = settings
        start = SimpleNamespace(provider=None, model=None, setup=False)
        start_provider(app, start)
        self.assertEqual((app.provider_name, app.model), ("p2", "local-model"))
        start_provider(app, SimpleNamespace(provider="p3", model=None, setup=False))
        self.assertEqual((app.provider_name, app.model), ("p3", "p3-default"))
        os.environ["MINIMAL_PROVIDER"] = "p1"
        start_provider(app, start)
        self.assertEqual((app.provider_name, app.model), ("p1", "p1-default"))

    def test_prompt_append_concatenates_global_project_local(self):
        self.write("global", {"prompt": {"append": "one"}})
        self.write("project", {"prompt": {"append": ["two", "three"]}})
        self.write("local", {"prompt": {"append": "four"}})
        self.assertEqual(self.settings()[0].prompt_append, ["one", "two", "three", "four"])

    def test_project_marketplaces_are_dropped(self):
        path = self.write("project", {"marketplaces": ["https://evil.example/m.git"], "model": "m"})
        settings, errors = self.settings()
        self.assertEqual(settings.marketplaces, [])
        self.assertEqual(settings.model, "m")
        self.assertEqual(errors, [f"{path}: marketplaces is only read from ~/.minimal/settings.json"])

    def test_wrong_types_and_unknown_keys_are_dropped(self):
        path = self.write("global", {"$schema": "x", "theme": 3, "colour": "red", "prompt": {"files": "AGENTS.md"}})
        settings, errors = self.settings()
        self.assertIsNone(settings.theme)
        self.assertEqual(
            errors,
            [f"{path}: theme must be a string", f"{path}: unknown setting 'colour'", f"{path}: prompt.files must be a list of strings"],
        )

    def test_malformed_project_file_is_reported_and_other_layers_apply(self):
        self.write("global", {"theme": "a"})
        path = self.write("project", "{not json")
        self.write("local", {"model": "m"})
        settings, errors = self.settings()
        self.assertEqual((settings.theme, settings.model), ("a", "m"))
        self.assertEqual(len(errors), 1)
        self.assertTrue(errors[0].startswith(f"{path}: not valid JSON ("), errors)

    def test_untrusted_project_allow_is_ignored_but_deny_applies(self):
        self.write("global", {"permissions": {"allow": ["read"]}})
        self.write("project", {"permissions": {"allow": ["bash"], "deny": ["write"]}})
        untrusted = self.settings(trusted=False)[0]
        self.assertEqual(untrusted.allow, [("read", "global")])
        self.assertEqual(untrusted.deny, [("write", "project")])
        self.assertTrue(untrusted.untrusted_risky)
        self.assertEqual(self.settings(trusted=True)[0].allow, [("read", "global"), ("bash", "project")])

    def test_untrusted_project_extension_settings_are_ignored(self):
        self.write("global", {"extensions": {"settings": {"x": {"a": 1, "b": 1}}}})
        self.write("local", {"extensions": {"settings": {"x": {"b": 2}}, "disabled": ["y"]}})
        self.assertEqual(self.settings(trusted=False)[0].extension_settings("x"), {"a": 1, "b": 1})
        self.assertEqual(self.settings(trusted=True)[0].extension_settings("x"), {"a": 1, "b": 2})

    def test_untrusted_project_cannot_disable_extensions(self):
        self.write("global", {"extensions": {"disabled": ["g"]}})
        self.write("project", {"extensions": {"disabled": ["guardrail"]}})
        untrusted = self.settings(trusted=False)[0]
        self.assertEqual(untrusted.disabled, frozenset({"g"}))
        self.assertTrue(untrusted.untrusted_risky)
        self.assertEqual(self.settings(trusted=True)[0].disabled, frozenset({"g", "guardrail"}))

    def snapshot_digest(self):
        layers, _ = config.read_layers(str(self.cwd))
        return loader.read_project_snapshot(str(self.cwd), config.risky_subset(layers)).digest

    def test_digest_covers_trust_gated_settings_only(self):
        self.write("project", {"model": "a", "permissions": {"allow": ["bash(ls*)"]}})
        before = self.snapshot_digest()
        self.write("project", {"model": "b", "permissions": {"allow": ["bash(ls*)"]}})
        self.assertEqual(self.snapshot_digest(), before)
        self.write("project", {"model": "b", "permissions": {"allow": ["bash"]}})
        self.assertNotEqual(self.snapshot_digest(), before)

    def test_digest_without_risky_settings_is_the_files_digest(self):
        ext = self.cwd / ".minimal" / "extensions" / "hello.py"
        ext.parent.mkdir()
        ext.write_text("def register(minimal):\n    pass\n")
        self.write("project", {"model": "a"})
        self.assertEqual(self.snapshot_digest(), loader.files_digest(loader.read_extensions([ext])))

    def test_project_prompt_files_stay_inside_the_project(self):
        Path(self.cwd.parent, "outside.md").write_text("SECRET")
        (self.cwd / "NOTES.md").write_text("project notes")
        self.write("project", {"prompt": {"files": ["../outside.md", "NOTES.md", "missing.md"]}})
        prompt = build_system_prompt(Registry(), str(self.cwd), self.settings()[0])
        self.assertNotIn("SECRET", prompt)
        self.assertIn("Project instructions (NOTES.md):\nproject notes", prompt)

    def test_global_prompt_files_may_point_anywhere(self):
        Path(self.cwd.parent, "outside.md").write_text("SHARED")
        self.write("global", {"prompt": {"files": ["../outside.md"]}})
        self.assertIn("SHARED", build_system_prompt(Registry(), str(self.cwd), self.settings()[0]))

    def test_reload_marks_project_untrusted_by_its_settings(self):
        self.write("project", {"model": "y", "permissions": {"allow": ["bash(ls*)"]}})
        app = App(registry=Registry(), session=Session("t", self.home / "t.jsonl"), cwd=str(self.cwd), auto_approve=False)
        app.reload_extensions()
        self.assertEqual(app.settings.model, "y")
        self.assertEqual(app.settings.allow, [])
        self.assertEqual(app.untrusted.count, 1)
        config.save_trusted(app.untrusted.key, app.untrusted.digest)
        app.reload_extensions()
        self.assertIsNone(app.untrusted)
        self.assertEqual(app.settings.allow, [("bash(ls*)", "project")])


if __name__ == "__main__":
    unittest.main()
