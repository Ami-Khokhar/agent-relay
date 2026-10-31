"""Device-local approval and its public CLI boundaries (#119)."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_discovery as discovery
import release_identity as identity
import safe_update

TAG = "0.4.0"
COMMIT = "a" * 40
NOW = 1_770_000_000.0
STAMP = "2026-10-01T00:00:00Z"


class _Boundaries:
    """Spy that every git, install and service command stays unexecuted."""

    def __enter__(self):
        self.calls = []
        def spy(name):
            def raise_if_called(*args, **kwargs):
                self.calls.append(name)
                raise AssertionError("a %s boundary command was executed" % name)
            return raise_if_called
        self.mocks = [patch.object(subprocess, name, spy(name))
                      for name in ("run", "call", "check_call", "check_output",
                                   "Popen", "getoutput", "getstatusoutput")]
        for mock in self.mocks:
            mock.start()
        return self

    def __exit__(self, *args):
        for mock in self.mocks:
            mock.stop()
        return False


class FakeTerminal:
    def __init__(self, answers=(), tty=True):
        self.answers, self.tty = list(answers), tty
        self.prompts, self.echoed = [], []

    def is_tty(self):
        return self.tty

    def prompt(self, message):
        self.prompts.append(message)
        return self.answers.pop(0) if self.answers else ""

    def echo(self, message):
        self.echoed.append(message)


def release(tag=TAG):
    return {"id": 7, "tag_name": tag, "draft": False, "prerelease": False,
            "html_url": discovery.RELEASE_URL + tag, "published_at": STAMP,
            "target_commitish": "decoy-not-proof",
            "assets": [{"id": 42, "name": "agent-relay-manifest.json", "updated_at": STAMP}]}


def manifest_body(tag=TAG, commit=COMMIT):
    return {"schema": "agent-relay.update/v1", "repository": identity.REPOSITORY,
            "version": tag, "tag": tag, "commit": commit,
            "files": [{"path": "src/server.py", "mode": "100644", "sha256": "0" * 64}]}


def fake_fetch():
    calls = []
    def fetch_json(url):
        calls.append(url)
        if url.startswith(identity.RELEASE_BY_TAG_URL):
            return json.dumps(release()).encode("utf-8")
        if url.startswith(identity.TAG_REF_URL):
            return json.dumps({"object": {"sha": COMMIT, "type": "commit"}}).encode("utf-8")
        if url.startswith(identity.ASSET_URL):
            return json.dumps(manifest_body()).encode("utf-8")
        raise identity.UpdateError("unexpected identity request %s" % url)
    fetch_json.calls = calls
    return fetch_json


def resolved_identity():
    return identity.resolve_release(TAG, fetch_json=fake_fetch())[0]


def read_consent(state_dir):
    return json.loads((Path(state_dir) / "consent.json").read_text(encoding="utf-8"))


def write_consent(state_dir, consent):
    (Path(state_dir) / "consent.json").write_text(json.dumps(consent, indent=1), encoding="utf-8")


class ApprovalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"

    def test_missing_tty_refuses_to_record_anything(self):
        with self.assertRaises(identity.UpdateError) as caught:
            identity.approve(resolved_identity(), state_dir=self.state, now=NOW,
                             terminal=FakeTerminal(tty=False))
        self.assertIn("interactive terminal", str(caught.exception))
        self.assertFalse((self.state / "consent.json").exists())

    def test_exact_typed_identity_records_private_consent(self):
        terminal = FakeTerminal([TAG, COMMIT, "approve"])
        with _Boundaries() as boundaries:
            consent = identity.approve(resolved_identity(), state_dir=self.state,
                                       now=NOW, terminal=terminal)
        self.assertEqual(boundaries.calls, [], "approval must never run commands")
        self.assertEqual([intro for intro in terminal.prompts], [identity.VERSION_PROMPT,
                          identity.COMMIT_PROMPT, identity.CONSENT_PROMPT])
        self.assertTrue(any(identity.VERSION_PROMPT in echoed or "commit" in echoed
                            for echoed in terminal.echoed), terminal.echoed)
        for name in ("consent.json", "device-id"):
            mode = stat.S_IMODE((self.state / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)
        stored = read_consent(self.state)
        self.assertEqual(stored["identityDigest"], identity.identity_digest(resolved_identity()))
        self.assertEqual(stored["issuedAt"], NOW)
        self.assertEqual(stored["expiresAt"], NOW + 900)
        self.assertEqual(stored["consumed"], False)
        self.assertEqual(consent["installation"], str(identity.installation_path()))

    def test_wrong_typed_version_or_commit_records_nothing(self):
        for answers in (["0.3.0", COMMIT, "approve"], [TAG, "b" * 40, "approve"],
                        [TAG, COMMIT, "y"]):
            state = Path(tempfile.mkdtemp()) / "s"
            self.addCleanup(lambda state=state: __import__("shutil").rmtree(str(state),
                             ignore_errors=True))
            with self.subTest(answers=answers):
                with self.assertRaises(identity.UpdateError):
                    identity.approve(resolved_identity(), state_dir=state,
                                     now=NOW, terminal=FakeTerminal(answers))
                self.assertFalse((state / "consent.json").exists())

    def _approved(self):
        identity.approve(resolved_identity(), state_dir=self.state, now=NOW,
                         terminal=FakeTerminal([TAG, COMMIT, "approve"]))
        return read_consent(self.state)

    def test_consume_validates_then_replay_is_refused(self):
        consent = self._approved()
        with _Boundaries() as boundaries:
            identity.consume_consent(resolved_identity(), state_dir=self.state, now=NOW + 10)
        self.assertEqual(boundaries.calls, [], "consent intake must never run commands")
        self.assertTrue(read_consent(self.state)["consumed"])
        del consent
        with self.assertRaises(identity.UpdateError) as caught:
            identity.consume_consent(resolved_identity(), state_dir=self.state, now=NOW + 10)
        self.assertIn("already consumed", str(caught.exception))

    def test_expired_future_and_mutated_consent_is_refused(self):
        self._approved()
        cases = [("expired", dict(now=NOW + 901), None),
                 ("future", dict(now=NOW - 1), None),
                 ("mutated digest", dict(now=NOW + 10), "identityDigest"),
                 ("wrong device", dict(now=NOW + 10), "device"),
                 ("changed installation", dict(now=NOW + 10), "installation"),
                 ("mutated commit", dict(now=NOW + 10), "commit")]
        for label, kwargs, field in cases:
            with self.subTest(label):
                write_consent(self.state, self._approved())
                if field == "identityDigest":
                    consent = read_consent(self.state); consent[field] = "0" * 64
                    write_consent(self.state, consent); continue
                if field:
                    consent = read_consent(self.state)
                    consent[field] = "x" * 10
                    write_consent(self.state, consent)
                with self.assertRaises(identity.UpdateError):
                    identity.consume_consent(resolved_identity(), state_dir=self.state, **kwargs)
        # wrong identity for a valid record is refused without consuming it
        write_consent(self.state, self._approved())
        other = dict(resolved_identity(), tag="0.4.1", commit="c" * 40)
        with self.assertRaises(identity.UpdateError):
            identity.consume_consent(other, state_dir=self.state, now=NOW + 10)

    def test_unsafe_state_files_and_paths_are_refused(self):
        self._approved()
        consent_path = self.state / "consent.json"
        os.chmod(consent_path, 0o644)
        with self.assertRaises(identity.UpdateError):
            identity.consume_consent(resolved_identity(), state_dir=self.state, now=NOW + 10)
        os.chmod(consent_path, 0o600)
        raw = consent_path.read_bytes()
        os.remove(consent_path)
        symlink = self.tmp.name + "/elsewhere.json"
        open(symlink, "w").write("{}")
        os.symlink(symlink, consent_path)
        with self.assertRaises(identity.UpdateError):
            identity.consume_consent(resolved_identity(), state_dir=self.state, now=NOW + 10)
        os.remove(consent_path)
        consent_path.write_bytes(raw)
        # a symlinked state directory resolves elsewhere
        other_dir = Path(tempfile.mkdtemp()) / "elsewhere"
        moved = other_dir / "state"
        moved.mkdir(parents=True)
        write_consent(moved, read_consent(self.state))
        (moved / "device-id").write_text((self.state / "device-id").read_text())
        link = self.state.parent / "linked-state"
        os.symlink(moved, link)
        with self.assertRaises(identity.UpdateError):
            identity.consume_consent(resolved_identity(), state_dir=link, now=NOW + 10)

    def test_nonexistent_consent_is_refused(self):
        self.state.mkdir()
        with self.assertRaises(identity.UpdateError):
            identity.consume_consent(resolved_identity(), state_dir=self.state, now=NOW)


class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.fetch = fake_fetch()
        enter = [patch.object(safe_update, "_fetch", self.fetch),
                 patch.object(safe_update, "_state_dir", lambda: self.state),
                 patch("safe_update.time.time", return_value=NOW)]
        for mock in enter:
            mock.start()
            self.addCleanup(mock.stop)

    def _terminal(self, answers):
        terminal = FakeTerminal(answers)
        mock = patch.object(identity, "_StdTerminal", lambda: terminal)
        mock.start()
        self.addCleanup(mock.stop)
        return terminal

    def test_inspect_is_metadata_only(self):
        with _Boundaries() as boundaries:
            code = safe_update.main(["inspect", TAG])
        self.assertEqual(code, 0)
        self.assertEqual(boundaries.calls, [], "the CLI must never run git or install commands")

    def test_missing_tty_considered_from_cli(self):
        terminal = FakeTerminal(tty=False)
        with _Boundaries() as boundaries:
            code = safe_update.main(["approve", TAG])
        self.assertEqual(code, 1)
        self.assertFalse((self.state / "consent.json").exists())
        self.assertEqual(boundaries.calls, [])

    def test_cli_approve_then_replayed_consume_fails(self):
        self._terminal([TAG, COMMIT, "approve"])
        with _Boundaries() as boundaries:
            self.assertEqual(safe_update.main(["approve", TAG]), 0)
            self.assertEqual(safe_update.main(["consume", TAG]), 0)
            self.assertEqual(safe_update.main(["consume", TAG]), 1)
        self.assertEqual(boundaries.calls, [])
        self.assertEqual(len(self.fetch.calls) % 3, 0)

    def test_cli_no_release_is_a_clear_refusal(self):
        def fetch(url):
            if url.startswith(identity.RELEASE_BY_TAG_URL):
                raise identity.UpdateError("no published stable release matches this tag; check the tag name")
            raise identity.UpdateError("unexpected identity request %s" % url)
        with patch.object(safe_update, "_fetch", fetch):
            with _Boundaries():
                code = safe_update.main(["inspect", "0.9.9"])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
