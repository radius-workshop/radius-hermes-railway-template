"""Secret entry must not expose credentials in argv, output, or failure messages."""
from __future__ import annotations

import contextlib
import getpass
import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "egress"))
import set_credential  # noqa: E402


class CredentialEntryTests(unittest.TestCase):
    args = ["--project", "project-id", "--environment", "production", "--service", "egress", "--name", "OPENROUTER_API_KEY"]
    secret = "sk-or-v1-test-secret-never-print"

    def invoke(self, *, tty=True, confirmation="egress", result=None, error=None, getpass_error=None, secret=None, name=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr), \
                patch.object(sys.stdin, "isatty", return_value=tty), \
                patch.object(stderr, "isatty", return_value=tty), \
                patch("builtins.input", return_value=confirmation) as prompt, \
                patch.object(set_credential.getpass, "getpass", return_value=self.secret if secret is None else secret,
                             side_effect=getpass_error) as entry, \
                patch.object(set_credential.subprocess, "run", return_value=result or subprocess.CompletedProcess([], 0),
                             side_effect=error) as run:
            args = [*self.args[:-1], name] if name else self.args
            code = set_credential.main(args)
        output = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(self.secret, output)
        return code, output, prompt, entry, run

    def test_secret_only_passed_on_stdin_with_explicit_target_and_no_deploy(self):
        code, output, _, _, run = self.invoke()
        self.assertEqual(code, 0)
        command = run.call_args.args[0]
        self.assertEqual(command, ["railway", "variable", "set", "OPENROUTER_API_KEY", "--stdin", "--skip-deploys",
                                   "--project", "project-id", "--environment", "production", "--service", "egress"])
        self.assertNotIn(self.secret, command)
        self.assertEqual(run.call_args.kwargs["input"], self.secret)
        self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("not yet checked", output)

    def test_failed_command_output_is_not_displayed(self):
        code, _, _, _, _ = self.invoke(result=subprocess.CompletedProcess([], 1, self.secret, self.secret))
        self.assertEqual(code, 1)

    def test_exception_contents_are_not_displayed(self):
        code, _, _, _, _ = self.invoke(error=subprocess.TimeoutExpired([self.secret], 60, output=self.secret))
        self.assertEqual(code, 1)

    def test_noninteractive_input_refused_before_prompt(self):
        code, _, prompt, entry, run = self.invoke(tty=False)
        self.assertEqual(code, 1)
        prompt.assert_not_called()
        entry.assert_not_called()
        run.assert_not_called()

    def test_target_must_be_confirmed_before_credential_entry(self):
        code, _, _, entry, run = self.invoke(confirmation="wrong-service")
        self.assertEqual(code, 1)
        entry.assert_not_called()
        run.assert_not_called()

    def test_getpass_must_not_fall_back_to_echo(self):
        code, _, _, _, run = self.invoke(getpass_error=getpass.GetPassWarning("cannot hide input"))
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_empty_and_multiline_credentials_refused(self):
        for secret in ("", "  ", "line1\nline2", "line1\rline2", "null\x00byte"):
            with self.subTest(secret=repr(secret)):
                code, _, _, _, run = self.invoke(secret=secret)
                self.assertEqual(code, 1)
                run.assert_not_called()

    def test_telegram_rejects_quotes_urls_and_malformed_tokens_before_upload(self):
        for secret in ('"123:Ab_c-9"', "https://api.telegram.org/bot123:Ab_c-9", "bot123:Ab_c-9",
                       "123", "123:", ":Ab_c-9", "123:Ab c", " 123:Ab_c-9", "123:Ab_c-9 "):
            with self.subTest(secret=secret):
                code, output, _, _, run = self.invoke(secret=secret, name="TELEGRAM_BOT_TOKEN")
                self.assertEqual(code, 1)
                self.assertIn("Paste only the bot token from BotFather, without quotes or a URL.", output)
                self.assertNotIn(secret, output)
                run.assert_not_called()

    def test_telegram_accepts_token_shape_without_length_constraint(self):
        for secret in ("1:a", "123456789:AbCd_ef-0123"):
            with self.subTest(secret=secret):
                code, output, _, _, run = self.invoke(secret=secret, name="TELEGRAM_BOT_TOKEN")
                self.assertEqual(code, 0)
                self.assertEqual(run.call_args.kwargs["input"], secret)
                self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
