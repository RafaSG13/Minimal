import base64
import hashlib
import io
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from minimal.providers import claude_auth
from minimal.providers.claude_auth import Credentials


class ClaudeAuthTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(claude_auth, "AUTH_FILE", Path(tmp.name) / "claude-auth.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_login_round_trip(self):
        seen = {}

        def open_browser(url):
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
            seen.update(query)
            self.assertEqual(query["code"], "true")
            self.assertEqual(query["code_challenge_method"], "S256")
            callback = query["redirect_uri"]
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(f"{callback}?code=abc&state=wrong", timeout=5)
            self.assertEqual(ctx.exception.code, 400)
            ctx.exception.close()
            with urllib.request.urlopen(f"{callback}?code=abc&state={query['state']}", timeout=5) as response:
                self.assertEqual(response.status, 200)
            return True

        token_request = mock.Mock(
            return_value={"access_token": "a", "refresh_token": "r", "expires_in": 3600, "account": {"email_address": "u@x"}}
        )
        with mock.patch.object(claude_auth, "_token_request", token_request), redirect_stdout(io.StringIO()):
            creds = claude_auth.login(open_browser=open_browser, timeout=5)

        body = token_request.call_args.args[0]
        self.assertEqual(body["grant_type"], "authorization_code")
        self.assertEqual(body["code"], "abc")
        self.assertEqual(body["state"], seen["state"])
        self.assertEqual(body["redirect_uri"], seen["redirect_uri"])
        challenge = base64.urlsafe_b64encode(hashlib.sha256(body["code_verifier"].encode()).digest()).rstrip(b"=")
        self.assertEqual(challenge.decode(), seen["code_challenge"])
        self.assertEqual(creds.email, "u@x")
        self.assertEqual(claude_auth.load(), creds)
        self.assertEqual(claude_auth.AUTH_FILE.stat().st_mode & 0o777, 0o600)

    def test_token_provider_refreshes_only_when_expired(self):
        provider = claude_auth.TokenProvider()

        claude_auth.save(Credentials("old", "r", int(time.time()) + 3600))
        token_request = mock.Mock(return_value={"access_token": "new", "expires_in": 3600})
        with mock.patch.object(claude_auth, "_token_request", token_request):
            self.assertEqual(provider().token, "old")
            token_request.assert_not_called()

            claude_auth.save(Credentials("old", "r", int(time.time()) - 1))
            self.assertEqual(provider().token, "new")
        token_request.assert_called_once_with(
            {"grant_type": "refresh_token", "client_id": claude_auth.CLIENT_ID, "refresh_token": "r"},
            {"anthropic-beta": "oauth-2025-04-20"},
        )
        stored = claude_auth.load()
        self.assertEqual((stored.access, stored.refresh), ("new", "r"))

        with redirect_stdout(io.StringIO()):
            claude_auth.logout()
        with self.assertRaisesRegex(RuntimeError, "Not logged in"):
            provider()


if __name__ == "__main__":
    unittest.main()
