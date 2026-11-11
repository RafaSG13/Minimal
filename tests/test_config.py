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
from minimal.extensions.registry import Registry
from minimal.providers.base import ProviderSpec
from minimal.ui import setup


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name, value in (("HOME_DIR", Path(tmp.name)), ("SETTINGS_FILE", Path(tmp.name) / "settings.json"),
                            ("KEYS_FILE", Path(tmp.name) / "keys.json")):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cwd = Path(tmp.name, "project")
        self.cwd.mkdir()
        env = mock.patch.dict(os.environ, {"GOOGLE_API_KEY": "stale-shell-key"}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        injected = mock.patch.object(config, "_injected_keys", set())
        injected.start()
        self.addCleanup(injected.stop)
        for var in ("MINIMAL_PROVIDER", "MINIMAL_MODEL"):
            os.environ.pop(var, None)

        registry = Registry()
        registry.add_provider(ProviderSpec(name="a", create=object, default_model="a-default"))
        registry.add_provider(ProviderSpec(name="b", create=object, default_model="b-default", env_keys=("B_KEY",)))
        session = Session("t", Path(tmp.name) / "t.jsonl")
        self.app = App(registry=registry, session=session, cwd=str(self.cwd), auto_approve=True)

    def start(self, provider=None, model=None, setup_flag=False):
        self.app.settings = config.Settings(tuple(config.read_layers(self.app.cwd)[0]))
        start_provider(self.app, SimpleNamespace(provider=provider, model=model, setup=setup_flag))
        return self.app.provider_name, self.app.model

    def test_saved_key_beats_shell_variable(self):
        config.save_key("GOOGLE_API_KEY", "good")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "good")
        self.assertEqual(config.KEYS_FILE.stat().st_mode & 0o777, 0o600)

        os.environ["GOOGLE_API_KEY"] = "stale-shell-key"  # a new process starts with the shell's value
        config.apply_keys()
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "good")

    def test_update_global_keeps_other_keys_and_refuses_broken_file(self):
        config.update_global({"provider": "a", "theme": "ocean"})
        config.update_global({"theme": None, "model": "m"})
        self.assertEqual(config.load_global(), {"provider": "a", "model": "m"})
        config.SETTINGS_FILE.write_text("{broken")
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            config.update_global({"theme": "x"})
        self.assertEqual(config.SETTINGS_FILE.read_text(), "{broken")

    def test_start_without_saved_provider_needs_setup(self):
        with mock.patch.object(setup, "can_run", return_value=False):
            with self.assertRaisesRegex(ValueError, "--setup"):
                self.start()
        self.assertEqual(self.start(provider="a"), ("a", "a-default"))

    def test_start_precedence(self):
        config.update_global({"provider": "a", "model": "a-custom"})
        self.assertEqual(self.start(), ("a", "a-custom"))
        self.assertEqual(self.start(model="m"), ("a", "m"))
        self.assertEqual(self.start(provider="a"), ("a", "a-custom"))  # same provider keeps the saved model
        with mock.patch.dict(os.environ, {"B_KEY": "k", "MINIMAL_PROVIDER": "b"}):
            self.assertEqual(self.start(), ("b", "b-default"))  # saved model belongs to another provider

    def test_saved_provider_that_cannot_start_runs_setup(self):
        config.update_global({"provider": "b", "model": "b-custom"})  # B_KEY missing
        with mock.patch.object(setup, "can_run", return_value=False):
            with self.assertRaisesRegex(ValueError, "B_KEY"):
                self.start()
        with (
            mock.patch.object(setup, "can_run", return_value=True),
            mock.patch.object(setup, "run_setup", return_value=("a", "a-picked")) as run_setup,
            mock.patch("sys.stderr"),
        ):
            self.assertEqual(self.start(), ("a", "a-picked"))
        run_setup.assert_called_once()

    def test_setup_offers_the_provider_default_when_no_model_is_saved(self):
        config.update_global({"provider": "a"})
        answer = mock.Mock(unsafe_ask=mock.Mock(return_value="a"))
        with (
            mock.patch.object(setup, "_stored_login", return_value=None),
            mock.patch.object(setup.questionary, "select", return_value=answer),
            mock.patch.object(setup.questionary, "text", side_effect=lambda _title, default, validate: mock.Mock(
                unsafe_ask=mock.Mock(return_value=default))),
            mock.patch("sys.stdout"),
        ):
            self.assertEqual(setup.run_setup(self.app.registry), ("a", "a-default"))
        self.assertEqual(config.load_global(), {"provider": "a", "model": "a-default"})


class PromptFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cwd = tmp.name
        self.prompt_file = Path(tmp.name) / "prompt.md"
        self.settings = config.Settings((config.Layer("global", Path(tmp.name, "settings.json"),
                                                      {"prompt.base": str(self.prompt_file)}),))

    def build(self) -> str:
        return build_system_prompt(Registry(), self.cwd, self.settings)

    def test_default_prompt_when_no_user_file(self):
        prompt = build_system_prompt(Registry(), self.cwd, config.Settings())
        self.assertTrue(prompt.startswith("You are a coding agent"))
        self.assertIn(f"Working directory: {self.cwd}", prompt)
        self.assertIn("You can extend minimal", prompt)

    def test_missing_configured_file_is_an_error(self):
        with self.assertRaisesRegex(ValueError, r"prompt\.md \(prompt\.base\) not found"):
            self.build()

    def test_user_file_replaces_base_prompt_only(self):
        self.prompt_file.write_text("Answer only in Spanish.", encoding="utf-8")
        Path(self.cwd, "AGENTS.md").write_text("use tabs", encoding="utf-8")
        prompt = self.build()
        self.assertTrue(prompt.startswith("Answer only in Spanish."))
        self.assertNotIn("You are a coding agent", prompt)
        for kept in ("Working directory:", "You can extend minimal", "use tabs"):
            self.assertIn(kept, prompt)

    def test_blank_user_file_means_no_base_prompt(self):
        self.prompt_file.write_text("  \n", encoding="utf-8")
        prompt = self.build()
        self.assertTrue(prompt.startswith("Working directory:"))
        self.assertNotIn("You are a coding agent", prompt)

    def test_unreadable_user_file_is_an_error(self):
        self.prompt_file.write_bytes(b"\xff\xfe\x00")
        with self.assertRaisesRegex(ValueError, "prompt.md"):
            self.build()


if __name__ == "__main__":
    unittest.main()
