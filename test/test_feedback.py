"""Draft preparation through the library and MCP public interfaces."""
import os
import sys
import unittest
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import feedback
from feedback import prepare_feedback
import mcp_server
from test_mcp import FakeUpstream


class FeedbackTests(unittest.TestCase):
    def test_prepare_draft_produces_bounded_prefilled_form_without_submission(self):
        draft = prepare_feedback("unavailable", "mcp", "Linux", "0.3.0",
                                 "MCP request could not reach relay",
                                 "Completed the operation directly after switching clients")
        parsed = urlsplit(draft["formUrl"])
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "github.com")
        self.assertEqual(parsed.path, "/Ami-Khokhar/agent-relay/issues/new")
        self.assertEqual(parse_qs(parsed.query)["body"], [draft["body"]])
        self.assertIn("MCP request could not reach relay", draft["body"])
        self.assertIn("switching clients", draft["body"])

    def test_mcp_prepares_draft_without_relay_or_submission_call(self):
        # The upstream stands in for the relay at the process boundary: it records every
        # request, so an empty call list means the tool reached the network zero times.
        # Autostart is attempted only from a failed request, so it could not fire either.
        upstream = FakeUpstream(lambda method, path, body: (200, {}))
        self.addCleanup(upstream.close)
        handle = mcp_server.create_http_handler(upstream.url, timeout=1)
        result = handle({"method": "tools/call", "params": {"name": "prepare_feedback", "arguments": {
            "category": "crash", "operation": "delegate", "system": "Other", "version": "0.3.0",
            "blockedAction": "Relay blocked this operation", "workaround": "Used another client"}}})
        self.assertIn("formUrl", result["structuredContent"])
        self.assertEqual(upstream.calls, [])

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
        secrets = ("AKIA" + "A" * 16, "-----BEGIN " + "RSA " + "PRIVATE KEY-----",
                   "https://user:password@example.invalid/path", "AWS_SECRET_ACCESS_KEY=synthetic",
                   "secret_key: synthetic", "access_token_v2=synthetic",
                   "Bearer " + "A" * 16)
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

    def test_bearer_scheme_mentioned_in_prose_is_not_a_credential(self):
        # A genuine report often names the auth scheme; only a credential-shaped token is refused.
        draft = prepare_feedback("incorrect_behavior", "mcp", "Linux", "0.3.0",
                                 "the MCP client sends Bearer auth but the relay rejects the token",
                                 "Used the HTTP API with the token file")
        self.assertIn("Bearer auth", draft["body"])
        with self.assertRaisesRegex(ValueError, "credential-like"):
            prepare_feedback("incorrect_behavior", "mcp", "Linux", "0.3.0",
                             "the client sends Bearer " + "A" * 16 + " and the relay rejects it",
                             "Used the HTTP API")

    def test_mcp_rejects_credentials_without_returning_a_draft(self):
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

    def test_max_length_fields_are_usable_and_one_character_more_is_refused(self):
        draft = prepare_feedback("other", "other", "Other", "0.3.0", "a" * feedback.LIMIT,
                                 "b" * feedback.LIMIT)
        self.assertIn("formUrl", draft)
        with self.assertRaisesRegex(ValueError, "1000 characters"):
            prepare_feedback("other", "other", "Other", "0.3.0", "a" * (feedback.LIMIT + 1), "b")
        with self.assertRaisesRegex(ValueError, "1000 characters"):
            prepare_feedback("other", "other", "Other", "0.3.0", "a", "b" * (feedback.LIMIT + 1))

    def test_max_length_non_ascii_fields_are_usable_and_one_character_more_is_refused(self):
        # A published maximum must not be refused because its text cost more URL characters:
        # 1000 CJK code points encode to 9 URL characters each and 1000 astral emoji to 12.
        for name, text in (("cjk", "\u6f22"), ("emoji", "\U0001F600")):
            with self.subTest(field=name):
                draft = prepare_feedback("other", "other", "Other", "0.3.0",
                                         text * feedback.LIMIT, text * feedback.LIMIT)
                self.assertIn("formUrl", draft)
                self.assertIn(text * feedback.LIMIT, draft["body"])
                with self.assertRaisesRegex(ValueError, "1000 characters"):
                    prepare_feedback("other", "other", "Other", "0.3.0",
                                     text * (feedback.LIMIT + 1), text * feedback.LIMIT)

    def test_published_limits_bound_the_prefilled_url(self):
        # Each code point costs at most 12 URL characters ("%XX" per UTF-8 byte, four bytes for
        # an astral code point), so the field limits bound the link: 12 * (2 * LIMIT +
        # VERSION_LIMIT) plus the fixed template, repository and title overhead (about 1 kB).
        # Keep this derivation in step with the limits instead of capping the URL, which is what
        # made the two bounds contradict each other.
        ceiling = 12 * (2 * feedback.LIMIT + feedback.VERSION_LIMIT) + 1024
        astral = "\U0001F600" * feedback.LIMIT
        version = "v0.0.0-" + "a" * 71 + "+b"
        draft = prepare_feedback("other", "other", "Other", version, astral, astral)
        self.assertLessEqual(len(draft["formUrl"]), ceiling)

    def test_version_limit_is_reachable(self):
        at_limit = "v0.0.0-" + "a" * 71 + "+b"
        self.assertEqual(len(at_limit), 80)
        self.assertIn("formUrl", prepare_feedback("other", "other", "Other", at_limit,
                                                   "Relay blocked operation", "Used fallback"))
        with self.assertRaisesRegex(ValueError, "80 characters"):
            prepare_feedback("other", "other", "Other", at_limit + "a",
                             "Relay blocked operation", "Used fallback")

    def test_categories_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "supported"):
            prepare_feedback("task_failed", "other", "Other", "0.3.0", "Blocked", "Used workaround")

    def test_published_tool_limits_match_the_library_bounds(self):
        schema = next(tool for tool in mcp_server.TOOLS
                      if tool["name"] == "prepare_feedback")["inputSchema"]["properties"]
        self.assertEqual(schema["blockedAction"]["maxLength"], feedback.LIMIT)
        self.assertEqual(schema["workaround"]["maxLength"], feedback.LIMIT)
        self.assertEqual(schema["version"]["maxLength"], feedback.VERSION_LIMIT)


if __name__ == "__main__":
    unittest.main()
