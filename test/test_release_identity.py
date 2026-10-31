"""Resolution and validation of published release identities (#119)."""
import hashlib
import io
import json
import os
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

TAG = "0.4.0"
COMMIT = "a" * 40
TAG_OBJECT = "b" * 40
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


def release(tag=TAG, *, draft=False, prerelease=False, asset_id=42):
    return {"id": 7, "tag_name": tag, "draft": draft, "prerelease": prerelease,
            "html_url": discovery.RELEASE_URL + tag, "published_at": STAMP,
            "target_commitish": "decoy-not-proof",
            "assets": [{"id": asset_id, "name": "agent-relay-manifest.json",
                        "updated_at": STAMP}]}


def manifest(tag=TAG, commit=COMMIT, **overrides):
    body = {"schema": "agent-relay.update/v1", "repository": identity.REPOSITORY,
            "version": tag, "tag": tag, "commit": commit,
            "files": [{"path": "src/server.py", "mode": "100644", "sha256": "0" * 64}]}
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def fake_api(tag=TAG, *, rel=None, ref=None, peel=None, asset=None, status=None):
    calls = []
    if rel is None:
        rel = release(tag)
    if ref is None:
        ref = {"object": {"sha": COMMIT, "type": "commit"}}

    def encode(value):
        return value if isinstance(value, (bytes, bytearray)) else json.dumps(value).encode("utf-8")

    def fetch_json(url):
        calls.append(url)
        if status and url == identity.RELEASE_BY_TAG_URL + tag:
            raise identity.UpdateError("no published stable release matches this tag; check the tag name")
        for prefix, value in ((identity.RELEASE_BY_TAG_URL, rel), (identity.TAG_REF_URL, ref),
                              (identity.TAG_PEEK_URL, peel), (identity.ASSET_URL, asset)):
            if url.startswith(prefix):
                resolved = value(tag) if callable(value) else value
                return encode(resolved)
        raise identity.UpdateError("unexpected identity request %s" % url)
    fetch_json.calls = calls
    return fetch_json


def asset_bytes(tag="0.4.0", commit=COMMIT, **overrides):
    return manifest(tag, commit, **overrides)


class ReleaseIdentityTest(unittest.TestCase):
    def resolve(self, *args, **kwargs):
        with _Boundaries() as boundaries:
            result = identity.resolve_release(*args, **kwargs)
        self.assertEqual(boundaries.calls, [], "identity resolution must never run commands")
        return result

    def test_lightweight_tag_resolves_immutable_commit(self):
        fake = fake_api(ref={"object": {"sha": COMMIT, "type": "commit"}},
                   asset=asset_bytes())
        result = self.resolve(TAG, fetch_json=fake)
        self.assertEqual(result[0]["commit"], COMMIT)
        self.assertEqual(result[0]["tag"], TAG)
        self.assertEqual(len(fake.calls), 3)
        self.assertTrue(all(url.startswith("https://api.github.com/repos/"
                                       + identity.REPOSITORY + "/") for url in fake.calls))

    def test_annotated_tag_peels_once_to_commit(self):
        fake = fake_api(ref={"object": {"sha": TAG_OBJECT, "type": "tag"}},
                   peel={"object": {"sha": COMMIT, "type": "commit"}},
                   asset=asset_bytes())
        result = self.resolve(TAG, fetch_json=fake)
        self.assertEqual(result[0]["commit"], COMMIT)
        self.assertEqual(len(fake.calls), 4)

    def test_target_commitish_is_never_proof(self):
        # The release object claims a different commit; only the refs/tags API counts.
        fake = fake_api(rel=release(), ref={"object": {"sha": COMMIT, "type": "commit"}},
                   asset=asset_bytes())
        result = self.resolve(TAG, fetch_json=fake)
        self.assertEqual(result[0]["commit"], COMMIT)
        self.assertNotEqual(result[0]["commit"], "decoy-not-proof")

    def test_missing_release_is_a_clear_refusal(self):
        fake = fake_api(status=404)
        with self.assertRaises(identity.UpdateError) as caught:
            identity.resolve_release(TAG, fetch_json=fake)
        self.assertIn("no published stable release", str(caught.exception))

    def test_no_release_is_never_invented(self):
        def fetch_json(url):
            raise identity.UpdateError("release service unavailable; check connectivity")
        with self.assertRaises(identity.UpdateError):
            identity.resolve_release(TAG, fetch_json=fetch_json)

    def test_non_semantic_or_prerelease_tag_refuses_before_any_request(self):
        for bad in ("what", "0.4.0-rc.1", ""):
            fake = fake_api()
            with self.assertRaises(identity.UpdateError):
                self.resolve(bad, fetch_json=fake)
            self.assertEqual(fake.calls, [], "an invalid tag must cause no requests")

    def test_noncanonical_release_metadata_is_rejected(self):
        bad = release()
        bad["html_url"] = "https://example.com/agent-relay/releases/tag/" + TAG
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(rel=bad, asset=asset_bytes()))

    def test_prerelease_or_draft_or_missing_asset_release_is_rejected(self):
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(rel=release(prerelease=True), asset=asset_bytes()))
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(rel=release(draft=True), asset=asset_bytes()))
        no_asset = release()
        no_asset["assets"] = []
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(rel=no_asset, asset=asset_bytes()))
        wrong_kind = release()
        wrong_kind["assets"] = [{"id": 42, "name": "agent-relay.zip", "updated_at": STAMP}]
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(rel=wrong_kind, asset=asset_bytes()))

    def test_tag_ref_object_kinds_are_restricted(self):
        tree_ring = {"object": {"sha": COMMIT, "type": "tree"}}
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(ref=tree_ring, asset=asset_bytes()))
        short = {"object": {"sha": "abc", "type": "commit"}}
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(ref=short, asset=asset_bytes()))
        anno = {"object": {"sha": TAG_OBJECT, "type": "tag"}}
        peel_tag = {"object": {"sha": TAG_OBJECT, "type": "tag"}}
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(ref=anno, peel=peel_tag, asset=asset_bytes()))

    def test_identity_carries_release_and_asset_timestamps_and_digest(self):
        fake = fake_api(asset=asset_bytes())
        result = self.resolve(TAG, fetch_json=fake)
        identity_dict, _manifest, manifest_bytes = result
        self.assertEqual(identity_dict["manifestDigest"], hashlib.sha256(manifest_bytes).hexdigest())
        self.assertEqual(identity_dict["publishedAt"], STAMP)
        self.assertEqual(identity_dict["assetUpdatedAt"], STAMP)
        self.assertEqual(identity_dict["releaseId"], 7)
        self.assertEqual(identity_dict["assetId"], 42)
        self.assertEqual(identity_dict["repository"], "Ami-Khokhar/agent-relay")

    def test_manifest_rejects_mismatch_and_mutation(self):
        fake = fake_api(asset=asset_bytes(tag="0.5.0"))
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake)
        fake = fake_api(asset=asset_bytes(commit="c" * 40))
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake)
        fake = fake_api(asset=asset_bytes(repository="evil/agent-relay"))
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake)
        fake = fake_api(asset=asset_bytes(schema="other/v9"))
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake)

    def test_manifest_rejects_duplicate_keys_and_bad_inventory(self):
        doubled = b'{"schema": "agent-relay.update/v1", "schema": "x", "repository": "r"}'
        with self.assertRaises(identity.UpdateError):
            self.resolve(TAG, fetch_json=fake_api(asset=doubled))
        for bad in ({"files": []},
                    {"files": [{"path": "/etc/passwd", "mode": "100644", "sha256": "0" * 64}]},
                    {"files": [{"path": "../escape", "mode": "100644", "sha256": "0" * 64}]},
                    {"files": [{"path": "a.sh", "mode": "100777", "sha256": "0" * 64}]},
                    {"files": [{"path": "a", "mode": "100644", "sha256": "zz" * 32}]},
                    {"files": [{"path": "a\nb", "mode": "100644", "sha256": "0" * 64}]}):
            body = dict(json.loads(asset_bytes()), **bad)
            with self.assertRaises(identity.UpdateError):
                self.resolve(TAG, fetch_json=fake_api(asset=json.dumps(body).encode("utf-8")))

    def test_manifest_digest_authenticates_content_against_fetched_bytes(self):
        # The digest binds the exact fetched asset bytes, so a mutated asset
        # payload changes the identity that consent will bind.
        trust = asset_bytes()
        other = asset_bytes(files=[{"path": "src/other.py", "mode": "100644", "sha256": "1" * 64}])
        first = self.resolve(TAG, fetch_json=fake_api(asset=trust))
        second = self.resolve(TAG, fetch_json=fake_api(asset=other))
        self.assertNotEqual(first[0]["manifestDigest"], second[0]["manifestDigest"])
        fake = fake_api(asset=trust)
        third = self.resolve(TAG, fetch_json=fake)
        self.assertEqual(first[0]["manifestDigest"], third[0]["manifestDigest"])


if __name__ == "__main__":
    unittest.main()
