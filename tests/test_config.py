import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from minimal import config, setup
from minimal.app import App
from minimal.cli import start_provider
from minimal.providers.base import ProviderSpec
from minimal.registry import Registry
from minimal.session import Session


class ConfigTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        for name in ("CONFIG_FILE", "KEYS_FILE"):
            patcher = mock.patch.object(config, name, Path(tmp.name) / f"{name}.json")
            patcher.start()
            self.addCleanup(patcher.stop)
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
        self.app = App(registry=registry, session=session, cwd=tmp.name, auto_approve=True)

    def start(self, provider=None, model=None, setup_flag=False):
        start_provider(self.app, SimpleNamespace(provider=provider, model=model, setup=setup_flag))
        return self.app.provider_name, self.app.model

    def test_saved_key_beats_shell_variable(self):
        config.save_key("GOOGLE_API_KEY", "good")
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "good")
        self.assertEqual(config.KEYS_FILE.stat().st_mode & 0o777, 0o600)

        os.environ["GOOGLE_API_KEY"] = "stale-shell-key"  # a new process starts with the shell's value
        config.apply_keys()
        self.assertEqual(os.environ["GOOGLE_API_KEY"], "good")

    def test_unreadable_config_is_an_error(self):
        config.CONFIG_FILE.write_text('{"provider": 1}')
        with self.assertRaisesRegex(ValueError, "--setup"):
            config.load_config()

    def test_start_without_saved_provider_needs_setup(self):
        with mock.patch.object(setup, "can_run", return_value=False):
            with self.assertRaisesRegex(ValueError, "--setup"):
                self.start()
        self.assertEqual(self.start(provider="a"), ("a", "a-default"))

    def test_start_precedence(self):
        config.save_config("a", "a-custom")
        self.assertEqual(self.start(), ("a", "a-custom"))
        self.assertEqual(self.start(model="m"), ("a", "m"))
        self.assertEqual(self.start(provider="a"), ("a", "a-custom"))  # same provider keeps the saved model
        with mock.patch.dict(os.environ, {"B_KEY": "k", "MINIMAL_PROVIDER": "b"}):
            self.assertEqual(self.start(), ("b", "b-default"))  # saved model belongs to another provider

    def test_saved_provider_that_cannot_start_runs_setup(self):
        config.save_config("b", "b-custom")  # B_KEY missing
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


if __name__ == "__main__":
    unittest.main()
