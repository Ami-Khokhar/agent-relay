"""Draft preparation through the CLI and MCP public interfaces."""
import json
import os
import subprocess
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
from feedback import prepare_feedback
import mcp_server


class FeedbackTests(unittest.TestCase):
    def test_cli_produces_bounded_prefilled_form_without_submission(self):
        result = subprocess.run([sys.executable, os.path.join(ROOT, "src", "feedback.py"),
                                 "--category", "unavailable", "--operation", "mcp",
                                 "--system", "Linux", "--version", "0.3.0",
                                 "--blocked-action", "MCP request could not reach relay",
                                 "--workaround", "Completed the operation directly after switching clients"],
                                capture_output=True, text=True, check=True)
        draft = json.loads(result.stdout)
        self.assertLessEqual(len(draft["body"]), 4096)
        parsed = urlsplit(draft["formUrl"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "github.com")
        self.assertEqual(parsed.path, "/Ami-Khokhar/agent-relay/issues/new")
        self.assertLessEqual(len(draft["formUrl"]), 2000)
        self.assertEqual(parse_qs(parsed.query)["body"], [draft["body"]])
        self.assertIn("MCP request could not reach relay", draft["body"])
        self.assertIn("switching clients", draft["body"])

    def test_mcp_prepares_draft_without_relay_or_submission_call(self):
        calls = []
        original = mcp_server._relay_request
        mcp_server._relay_request = lambda *args, **kwargs: calls.append(args)
        try:
            handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
            result = handle({"method": "tools/call", "params": {"name": "prepare_feedback", "arguments": {
                "category": "crash", "operation": "delegate", "system": "Other", "version": "0.3.0",
                "blockedAction": "Relay blocked this operation", "workaround": "Used another client"}}})
            self.assertIn("formUrl", result["structuredContent"])
            self.assertEqual(calls, [])
        finally:
            mcp_server._relay_request = original

    def test_draft_omits_private_context_and_refuses_known_secret(self):
        draft = prepare_feedback("other", "other", "Other", "0.3.0", "Relay blocked operation",
                                 "Used a workaround")
        text = draft["body"] + draft["formUrl"]
        for private in ("taskId", "prompt", "output", "cwd", "environment"):
            self.assertNotIn(private, text)
        with self.assertRaisesRegex(ValueError, "credential-like"):
            prepare_feedback("other", "other", "Other", "0.3.0", "token=ghp_" + "A" * 30,
                             "Used a workaround")

    def test_credential_shapes_are_rejected_from_all_narrative_fields(self):
        secrets = ("AKIA" + "A" * 16, "-----BEGIN RSA PRIVATE KEY-----",
                   "https://user:password@example.invalid/path", "AWS_SECRET_ACCESS_KEY=synthetic",
                   "secret_key: synthetic", "access_token_v2=synthetic")
        for secret in secrets:
            for field in ("blocked", "workaround"):
                values = {"blocked": "Relay blocked operation", "workaround": "Used local fallback"}
                values[field] = secret
                with self.subTest(secret=secret, field=field), self.assertRaisesRegex(ValueError, "credential-like"):
                    prepare_feedback("other", "other", "Other", "0.3.0", values["blocked"], values["workaround"])
            with self.subTest(secret=secret, field="version"), self.assertRaisesRegex(ValueError, "credential-like"):
                prepare_feedback("other", "other", "Other", secret, "Relay blocked operation", "Used local fallback")
        self.assertEqual(prepare_feedback("other", "other", "Other", "0.3.0",
                                          "Relay could not open its socket", "Used the direct local interface")
                         ["title"], "[Relay problem] other")

    def test_cli_and_mcp_reject_credentials_without_returning_a_draft(self):
        cli = subprocess.run([sys.executable, os.path.join(ROOT, "src", "feedback.py"),
                              "--category", "other", "--operation", "mcp", "--system", "Linux",
                              "--version", "0.3.0", "--blocked-action", "AWS_SECRET_ACCESS_KEY=synthetic",
                              "--workaround", "Used local fallback"], capture_output=True, text=True)
        self.assertNotEqual(cli.returncode, 0)
        self.assertEqual(cli.stdout, "")
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        with self.assertRaisesRegex(mcp_server.RpcError, "credential-like"):
            handle({"method": "tools/call", "params": {"name": "prepare_feedback", "arguments": {
                "category": "other", "operation": "mcp", "system": "Linux", "version": "0.3.0",
                "blockedAction": "Relay blocked operation", "workaround": "https://user:pass@example.invalid/"}}})

    def test_version_is_bounded_semver_revision_or_unknown_and_free_text_rejects_controls(self):
        for version in ("v0.3.0-rc.1+build.2", "abcdef0", "unknown"):
            self.assertIn("formUrl", prepare_feedback("other", "mcp", "Linux", version,
                                                       "Relay blocked operation", "Used fallback"))
        for version in ("x" * 81, "main", "0.3.0\u202e"):
            with self.subTest(version=version), self.assertRaises(ValueError):
                prepare_feedback("other", "mcp", "Linux", version, "Relay blocked operation", "Used fallback")
        for value in ("Relay\u202e blocked operation", "Relay\x00 blocked operation"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "control or bidirectional"):
                prepare_feedback("other", "mcp", "Linux", "0.3.0", value, "Used fallback")

    def test_missing_blocker_or_workaround_produces_no_draft_or_form(self):
        for blocker, workaround in (("", "Used a workaround"), ("Relay blocked operation", "")):
            with self.assertRaises(ValueError):
                prepare_feedback("other", "other", "Other", "0.3.0", blocker, workaround)
        handle = mcp_server.create_http_handler("http://127.0.0.1:1", timeout=1)
        with self.assertRaisesRegex(mcp_server.RpcError, "blocked action and workaround"):
            handle({"method": "tools/call", "params": {"name": "prepare_feedback", "arguments": {
                "category": "crash", "operation": "delegate", "system": "Other", "version": "0.3.0",
                "blockedAction": "Relay blocked operation"}}})

    def test_bounds_and_categories_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "1000 characters"):
            prepare_feedback("other", "other", "Other", "0.3.0", "x" * 1001, "Used workaround")
        with self.assertRaisesRegex(ValueError, "supported"):
            prepare_feedback("task_failed", "other", "Other", "0.3.0", "Blocked", "Used workaround")
        with self.assertRaisesRegex(ValueError, "URL too long"):
            prepare_feedback("other", "other", "Other", "0.3.0", "&" * 1000, "Used workaround")


if __name__ == "__main__":
    unittest.main()
