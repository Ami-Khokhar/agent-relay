"""Isolated exact-commit staging without touching active source (#119, #155)."""
import base64
import hashlib
import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
import release_identity as identity
import update_staging
import safe_update

TAG = "0.4.0"
NOW = 1_770_000_000.0
STAMP = "2026-10-01T00:00:00Z"
ORIGIN = "https://github.com/Ami-Khokhar/agent-relay.git"


def _env():
    return {"PATH": "/usr/bin:/bin", "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "f",
            "GIT_AUTHOR_EMAIL": "f@example.com", "GIT_COMMITTER_NAME": "f",
            "GIT_COMMITTER_EMAIL": "f@example.com", "HOME": os.environ.get("HOME", "")}


def _run_git(root, *args):
    result = subprocess.run(["/usr/bin/git", "-C", str(root), *args],
                            capture_output=True, text=True, env=_env())
    if result.returncode != 0:
        raise AssertionError("fixture git %s failed: %s" % (args, result.stderr))
    return result.stdout


def _tree_inventory(root):
    """The full byte inventory of a tree: the unchanged-active-data proof."""
    inventory = {}
    for dirpath, dirnames, filenames in os.walk(str(root)):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            payload = b"" if path.is_dir() else path.read_bytes()
            inventory[path.relative_to(root).as_posix()] = (
                stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
                len(payload), hashlib.sha256(payload).hexdigest())
    return inventory


class _FakeTerminal:
    def __init__(self, answers):
        self.answers, self.prompts = list(answers), []

    def is_tty(self):
        return True

    def prompt(self, message):
        self.prompts.append(message)
        return self.answers.pop(0) if self.answers else ""

    def echo(self, message):
        pass


def _fake_git_script(tmp_root, log_path, origin_repo):
    """A fake git process: records its arguments and environment and forwards
    to the trusted git, translating the canonical clone URL to a local fixture
    repository, so the tests never publish to or fetch from the network."""
    parts = [
        "#!/bin/bash",
        "for _v in PATH GIT_CONFIG_GLOBAL GIT_CONFIG_NOSYSTEM GIT_ALLOW_PROTOCOL"
        " GIT_TERMINAL_PROMPT; do",
        '  _val=$(printenv "$_v")',
        '  echo "env $_v=$_val" >> @LOG@',
        "done",
        'for _a in "$@"; do echo "arg $_a" >> @LOG@; done',
        'args=("$@")',
        "for ((i=0; i<${#args[@]}; i++)); do",
        '  [ "${args[i]}" = "@ORIGIN@" ] && args[i]=@LOCAL@',
        "done",
        'unset GIT_ALLOW_PROTOCOL',
        'exec /usr/bin/git -c protocol.file.allow=always "${args[@]}"',
    ]
    script = chr(10).join(parts).replace("@LOG@", str(log_path)) \
        .replace("@ORIGIN@", ORIGIN).replace("@LOCAL@", str(origin_repo))
    path = Path(tmp_root) / "fake-git"
    path.write_text(script)
    os.chmod(str(path), 0o755)
    return path


class _Fixture:
    """One ordinary, clean, user-owned checkout with the canonical remote."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.repo = base / "relay-checkout"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "scripts").mkdir()
        (self.repo / "README.md").write_text("# agent-relay\n")
        (self.repo / "src" / "server.py").write_text("# relay server\n")
        (self.repo / "scripts" / "update.sh").write_text("# legacy updater\n")
        os.chmod(str(self.repo / "scripts" / "update.sh"), 0o755)
        self.origin = base / "origin"
        _run_git(base, "init", "-q", "--bare", str(self.origin))
        _run_git(self.repo, "init", "-q", "-b", "main")
        _run_git(self.repo, "add", "-A")
        _run_git(self.repo, "commit", "-q", "-m", "release")
        self.commit = _run_git(self.repo, "rev-parse", "HEAD").strip()
        _run_git(self.repo, "remote", "add", "origin", ORIGIN)
        _run_git(self.repo, "push", "-q", str(self.origin), "main:main")
        self.install_patch = patch.object(identity, "installation_path",
                                          lambda: self.repo.resolve())
        self.install_patch.start()
        files = []
        for path, mode in (("README.md", "100644"), ("src/server.py", "100644"),
                           ("scripts/update.sh", "100755")):
            files.append({"path": path, "mode": mode,
                          "sha256": hashlib.sha256((self.repo / path).read_bytes()).hexdigest()})
        self.manifest = {"schema": identity.MANIFEST_SCHEMA, "repository": identity.REPOSITORY,
                         "version": TAG, "tag": TAG, "commit": self.commit, "files": files}
        self.manifest_bytes = json.dumps(self.manifest).encode("utf-8")
        return self

    def __exit__(self, *args):
        self.install_patch.stop()
        self.tmp.cleanup()
        return False

    def identity(self):
        return {"repository": identity.REPOSITORY, "releaseId": 7, "tag": TAG,
                "commit": self.commit, "assetId": 42,
                "manifestDigest": hashlib.sha256(self.manifest_bytes).hexdigest(),
                "publishedAt": STAMP, "assetUpdatedAt": STAMP}

    def approve(self, state):
        identity.approve(self.identity(), state_dir=state, now=NOW,
                         terminal=_FakeTerminal([TAG, self.commit, "approve"]))

    def fake_fetch(self):
        def fetch(url):
            if url.startswith(identity.RELEASE_BY_TAG_URL):
                return json.dumps({"id": 7, "tag_name": TAG, "draft": False, "prerelease": False,
                                   "html_url": "https://github.com/" + identity.REPOSITORY +
                                               "/releases/tag/" + TAG, "published_at": STAMP,
                                   "assets": [{"id": 42, "name": "agent-relay-manifest.json",
                                               "updated_at": STAMP}]}).encode("utf-8")
            if url.startswith(identity.TAG_REF_URL):
                return json.dumps({"object": {"sha": self.commit, "type": "commit"}}).encode()
            if url.startswith(identity.ASSET_URL):
                return self.manifest_bytes
            raise identity.UpdateError("unexpected request %s" % url)
        return fetch


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.scratch = Path(self.tmp.name)
        self.state = self.scratch / "state"

    def _git_for(self, fixture):
        return update_staging.Git(str(_fake_git_script(self.scratch, self.scratch / "fake.log",
                                                       str(fixture.repo))))


class InspectTest(_Case):
    """inspect_checkout: metadata-only discovery, no active-tree writes."""

    def test_clean_canonical_checkout_is_identified_without_writes(self):
        with _Fixture() as fixture:
            git = update_staging.default_git()
            before = _tree_inventory(fixture.repo)
            summary = update_staging.inspect_checkout(fixture.repo, git=git)
            self.assertEqual(summary["root"], str(fixture.repo.resolve()))
            self.assertEqual(summary["commit"], fixture.commit)
            self.assertEqual(summary["origin"], update_staging.SINGLE_ORIGIN)
            self.assertEqual(_tree_inventory(fixture.repo), before,
                             "discovery must not write the checkout")

    def _refuses(self, label, mutate):
        with _Fixture() as fixture:
            git = self._git_for(fixture)
            scratch = self.scratch / label.replace(" ", "-")
            subprocess.run(["cp", "-a", str(fixture.repo), str(scratch)], check=True)
            mutate(scratch)
            inventory = _tree_inventory(scratch)
            with self.assertRaises(update_staging.StagingError) as caught:
                update_staging.inspect_checkout(scratch, git=git)
            self.assertGreater(len(str(caught.exception)), 30, label)
            self.assertEqual(_tree_inventory(scratch), inventory,
                             "a refusal must not write the rejected checkout")

    def test_unsupported_configurations_fail_without_change(self):
        self._refuses("dirty tracked", lambda r: (r / "src" / "server.py").write_text("hi\n"))
        self._refuses("dirty untracked", lambda r: (r / "extra.txt").write_text("hi\n"))
        self._refuses("noncanonical origin",
                      lambda r: _run_git(r, "remote", "set-url", "origin",
                                         "https://evil.example.net/x.git"))
        def linked_worktree(r):
            _run_git(r, "worktree", "add", "-q", str(r.parent / "wt"), "HEAD")
            os.rename(str(r), str(r) + "-idle")
            os.rename(str(r.parent / "wt"), str(r))

        self._refuses("linked worktree", linked_worktree)
        self._refuses("worktree pointer", lambda r: os.rename(str(r / ".git"),
                      str(r.parent / "dotgit-backup")))
        self._refuses("symlinked root", lambda r: os.rename(str(r), str(r) + "-real")
                      or os.symlink(str(r) + "-real", str(r)))
        self._refuses("symlinked ancestor", lambda r: os.rename(str(r), str(r.parent / "moved"))
                      or os.symlink(str(r.parent), str(r)))
        self._refuses("package-managed location", lambda r: (
            os.makedirs(str(r.parent / "site-packages")),
            os.rename(str(r), str(r.parent / "site-packages" / "relay-package"))))
        self._refuses("tracked symlink", lambda r: os.symlink("README.md", str(r / "linked"))
                      or _run_git(r, "add", "linked"))
        self._refuses("tracked submodule gitlink", lambda r: _run_git(
            r, "update-index", "--add", "--cacheinfo", "160000," + "a" * 40 + ",vendor"))
        self._refuses("filter configuration", lambda r: _run_git(
            r, "config", "filter.lfs.clean", "/evil/clean"))
        self._refuses("hook-path configuration", lambda r: _run_git(
            r, "config", "core.hooksPath", "/evil/hooks"))
        self._refuses("include configuration", lambda r: _run_git(
            r, "config", "include.path", "/tmp/evil.config"))

    def test_system_prefix_refusal(self):
        with _Fixture() as fixture:
            prefix = self.scratch / "user-tree"
            prefix.mkdir()
            subprocess.run(["cp", "-a", str(fixture.repo), str(prefix / "relay")], check=True)
            with patch.object(update_staging, "SYSTEM_PREFIXES", ("/", str(self.scratch))):
                with self.assertRaises(update_staging.StagingError) as caught:
                    update_staging.inspect_checkout(prefix / "relay",
                                                    git=update_staging.default_git())
            self.assertIn("system prefix", str(caught.exception))

    def test_git_requires_an_absolute_trusted_executable(self):
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.Git("git")
        self.assertIn("absolute path", str(caught.exception))
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.Git(str(self.scratch / "missing-git"))
        self.assertIn("executable file", str(caught.exception))


class GitBoundaryTest(_Case):
    """Every git call runs trusted, scrubs its environment and kills hooks."""

    def test_invocations_disable_hooks_and_pass_no_process_environment(self):
        with _Fixture() as fixture:
            log = self.scratch / "fake.log"
            git = update_staging.Git(str(_fake_git_script(self.scratch, log, str(fixture.repo))))
            update_staging.inspect_checkout(fixture.repo, git=git)
            lines = log.read_text().splitlines()
            args = [line.split(" ", 1)[1] for line in lines if line.startswith("arg ")]
            for constant in ("-c", "core.hooksPath=" + os.devnull, "core.autocrlf=false",
                             "core.eol=lf"):
                self.assertIn(constant, args, "every git call must disable hooks and filters")
            self.assertEqual([a for a in args if a.endswith("/fake-git")], [],
                             "the fake git must come from the injected absolute executable")
            envs = {}
            for line in lines:
                if line.startswith("env "):
                    name, _, value = line[4:].partition("=")
                    envs.setdefault(name, set()).add(value)
            self.assertEqual(envs["PATH"], {"/usr/bin:/bin"})
            self.assertEqual(envs["GIT_CONFIG_GLOBAL"], {os.devnull})
            self.assertEqual(envs["GIT_CONFIG_NOSYSTEM"], {"1"})
            self.assertEqual(envs["GIT_ALLOW_PROTOCOL"], {"https"})
            self.assertEqual(envs["GIT_TERMINAL_PROMPT"], {"0"})


class StagingTest(_Case):
    """stage_release: isolated exact-commit private clone, verified inventory."""

    def _stage(self):
        with _Fixture() as fixture:
            fixture.approve(self.state)
            before = _tree_inventory(fixture.repo)
            git = self._git_for(fixture)
            receipt = update_staging.stage_release(
                fixture.identity(), root=fixture.repo, state_dir=self.state, git=git,
                manifest=fixture.manifest, manifest_bytes=fixture.manifest_bytes, now=NOW + 10)
            self.assertEqual(_tree_inventory(fixture.repo), before,
                             "staging must leave the active checkout byte-for-byte intact")
            return fixture, git, receipt

    def test_stage_creates_verified_isolated_stage(self):
        fixture, git, receipt = self._stage()
        stage = Path(receipt["stage"])
        self.assertTrue(stage.is_dir())
        self.assertEqual(stat.S_IMODE(stage.stat().st_mode) & 0o077, 0)
        self.assertEqual(receipt["identity"]["commit"], fixture.commit)
        self.assertEqual(receipt["sourceRoot"], str(fixture.repo.resolve()))
        self.assertEqual(receipt["sourceCommit"], fixture.commit)
        inventory = update_staging.verify_stage(stage, fixture.identity())
        self.assertEqual({entry["path"] for entry in inventory},
                         {"README.md", "src/server.py", "scripts/update.sh"})
        self.assertEqual({entry["mode"] for entry in inventory}, {"100644", "100755"})
        self.assertEqual(json.loads((self.state / "consent.json").read_text())["consumed"], True)

    def test_stage_receipt_binds_identity_manifest_and_source(self):
        fixture, _git, receipt = self._stage()
        stage = Path(receipt["stage"])
        receipt_body = json.loads((stage / "stage-receipt.json").read_text())
        self.assertEqual(receipt_body["schema"], update_staging.STAGE_SCHEMA)
        self.assertEqual(receipt_body["identityDigest"], identity.identity_digest(fixture.identity()))
        self.assertEqual(receipt_body["identity"], fixture.identity())
        self.assertEqual(hashlib.sha256(base64.b64decode(receipt_body["manifestB64"])).hexdigest(),
                         fixture.identity()["manifestDigest"])
        self.assertEqual(receipt_body["sourceCommit"], fixture.commit)
        self.assertEqual(receipt_body["sourceRoot"], str(fixture.repo.resolve()))
        self.assertTrue((stage / "stage-receipt.json").stat().st_mode & 0o077 == 0)

    def test_stage_is_detached_at_exact_commit_with_hooks_disabled(self):
        fixture, _git, receipt = self._stage()
        stage = Path(receipt["stage"])
        lines = (self.scratch / "fake.log").read_text().splitlines()
        args = [line.split(" ", 1)[1] for line in lines if line.startswith("arg ")]
        self.assertEqual(args[args.index("checkout") + 1], "--detach")
        self.assertEqual(args[args.index("--detach") + 1], fixture.commit)
        self.assertIn("clone", args, "the stage comes from one clone of the canonical remote")
        env = {line[4:].split("=", 1)[0]: line[4:].split("=", 1)[1]
               for line in lines if line.startswith("env ")}
        self.assertEqual(env["PATH"], "/usr/bin:/bin")
        self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "https")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        for constant in ("core.hooksPath=" + os.devnull, "core.autocrlf=false", "core.eol=lf"):
            self.assertIn(constant, args, "every stage git call must disable hooks and filters")
        self.assertTrue((stage / ".git" / "HEAD").exists())

    def test_unverified_stage_contents_are_refused(self):
        fixture, _git, receipt = self._stage()
        stage = Path(receipt["stage"])
        self.assertTrue(update_staging.verify_stage(stage, fixture.identity()))
        (stage / "extra.py").write_text("print('evil')\n")
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("extra files", str(caught.exception))
        (stage / "extra.py").unlink()
        (stage / "README.md").unlink()
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("missing", str(caught.exception))
        (stage / "README.md").write_text("# tampered\n")
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("sha256", str(caught.exception))
        (stage / "README.md").write_text("# agent-relay\n")
        os.chmod(str(stage / "src" / "server.py"), 0o755)
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("mode", str(caught.exception))
        os.chmod(str(stage / "src" / "server.py"), 0o644)
        os.symlink("/etc/passwd", str(stage / "linked"))
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("regular file", str(caught.exception))
        os.remove(str(stage / "linked"))
        other_dir = self.scratch / "linked-dir-target"
        other_dir.mkdir()
        os.symlink(str(other_dir), str(stage / "linked-dir"))
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("real directory", str(caught.exception))
        os.remove(str(stage / "linked-dir"))
        (stage / "smuggled").mkdir()
        (stage / "smuggled" / "payload.txt").write_text("extra tree\n")
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        # the smuggled tree is refused either as an unmanifested directory or as
        # extra files, depending on which check sees it first
        self.assertIn("smuggled", str(caught.exception))
        shutil.rmtree(str(stage / "smuggled"))
        (stage / "empty-extra").mkdir()
        with self.assertRaises(update_staging.StagingError) as caught:
            update_staging.verify_stage(stage, fixture.identity())
        self.assertIn("directories the manifest cannot describe", str(caught.exception))
        (stage / "empty-extra").rmdir()
        evil = dict(fixture.manifest, files=[
            {"path": "../outside.txt", "mode": "100644", "sha256": "0" * 64}])
        with self.assertRaises(identity.UpdateError):
            update_staging.verify_stage(stage, fixture.identity(), manifest=evil)
        self.assertTrue(update_staging.verify_stage(stage, fixture.identity()),
                        "after removal of every tamper the stage verifies again")

    def test_mismatched_commit_is_refused(self):
        fixture, _git, receipt = self._stage()
        wrong = dict(fixture.identity(), commit="e" * 40)
        with self.assertRaises(identity.UpdateError) as caught:
            update_staging.verify_stage(Path(receipt["stage"]), wrong)
        self.assertIn("not the approved commit", str(caught.exception))

    def test_mismatched_manifest_is_refused(self):
        fixture, git, _receipt = self._stage()
        other = dict(fixture.manifest, files=list(fixture.manifest["files"])[:1],
                     commit=fixture.commit)
        with self.assertRaises(identity.UpdateError):
            update_staging.stage_release(
                fixture.identity(), root=fixture.repo, state_dir=self.state, git=git,
                manifest=other, manifest_bytes=json.dumps(other).encode("utf-8"), now=NOW + 20)
        unsafe = dict(fixture.manifest)
        unsafe["files"] = list(fixture.manifest["files"]) + [
            {"path": "linked", "mode": "120000", "sha256": "0" * 64}]
        with self.assertRaises(identity.UpdateError):
            update_staging.stage_release(
                fixture.identity(), root=fixture.repo, state_dir=self.state, git=git,
                manifest=unsafe, manifest_bytes=json.dumps(unsafe).encode("utf-8"), now=NOW + 20)

    def test_stage_needs_this_consent_and_installation(self):
        with _Fixture() as fixture:
            git = self._git_for(fixture)
            with self.assertRaises(identity.UpdateError) as caught:
                update_staging.stage_release(
                    fixture.identity(), root=fixture.repo, state_dir=self.state, git=git,
                    manifest=fixture.manifest, manifest_bytes=fixture.manifest_bytes,
                    now=NOW + 10)
            self.assertIn("consent", str(caught.exception))
            self.assertEqual(list((self.state / "staging").glob("stage-*"))
                             if (self.state / "staging").exists() else [], [])
            fixture.approve(self.state)
            other = dict(fixture.identity(), tag="0.5.0", commit="b" * 40)
            with self.assertRaises(identity.UpdateError):
                update_staging.stage_release(
                    other, root=fixture.repo, state_dir=self.state, git=git,
                    manifest=fixture.manifest, manifest_bytes=fixture.manifest_bytes,
                    now=NOW + 10)
            self.assertEqual(json.loads((self.state / "consent.json").read_text())["consumed"],
                             False)

    def test_stage_refused_for_a_mismatched_install_dir(self):
        with _Fixture() as fixture:
            fixture.approve(self.state)
            git = self._git_for(fixture)
            other_root = self.scratch / "not-this-install"
            other_root.mkdir()
            with self.assertRaises((update_staging.StagingError, identity.UpdateError)):
                update_staging.stage_release(
                    fixture.identity(), root=other_root, state_dir=self.state, git=git,
                    manifest=fixture.manifest, manifest_bytes=fixture.manifest_bytes,
                    now=NOW + 10)
            self.assertEqual(json.loads((self.state / "consent.json").read_text())["consumed"],
                             False)


class CliTest(_Case):
    """`safe_update.py stage TAG --install-dir PATH`."""

    def _cli(self, fixture, fetch):
        with patch.object(safe_update, "_state_dir", lambda: self.state):
            with patch.object(safe_update.time, "time", lambda: NOW + 10):
                with patch.object(safe_update, "_fetch", fetch):
                    with patch.object(update_staging, "default_git",
                                      lambda: str(self._git_for(fixture).exe)):
                        return safe_update.main(["stage", TAG, "--install-dir",
                                                 str(fixture.repo)])

    def test_stage_rejects_a_dirty_checkout_without_service_or_writes(self):
        with _Fixture() as fixture:
            fixture.approve(self.state)
            (fixture.repo / "dirty.txt").write_text("hi\n")
            before = _tree_inventory(fixture.repo)
            with patch.object(identity, "strict_json", wraps=identity.strict_json) as parses:
                code = self._cli(fixture, fixture.fake_fetch())
            self.assertEqual(code, 1)
            self.assertGreater(len(parses.call_args_list), 0,
                               "the identity fetch happened (metadata only)")
            self.assertEqual(_tree_inventory(fixture.repo), before)
            self.assertEqual(json.loads((self.state / "consent.json").read_text())["consumed"],
                             False, "a refused stage must not burn the consent")
            self.assertFalse((self.state / "staging").exists()
                             and list((self.state / "staging").glob("stage-*")),
                             "a refused stage leaves no stage directory")

    def test_stage_records_verified_receipt_only_after_inventory_checks(self):
        with _Fixture() as fixture:
            fixture.approve(self.state)
            before = _tree_inventory(fixture.repo)
            self.assertEqual(self._cli(fixture, fixture.fake_fetch()), 0)
            self.assertEqual(_tree_inventory(fixture.repo), before)
            stages = list((self.state / "staging").glob("stage-*"))
            self.assertEqual(len(stages), 1)
            receipt = json.loads((stages[0] / "stage-receipt.json").read_text())
            self.assertEqual(receipt["identity"]["tag"], TAG)
            self.assertEqual(receipt["sourceRoot"], str(fixture.repo.resolve()))
            inventory = update_staging.verify_stage(stages[0], fixture.identity())
            self.assertEqual({entry["path"] for entry in inventory},
                             {"README.md", "src/server.py", "scripts/update.sh"})
            self.assertEqual(json.loads((self.state / "consent.json").read_text())["consumed"],
                             True)

    def test_cli_never_executes_staged_content(self):
        with _Fixture() as fixture:
            fixture.approve(self.state)
            self.assertEqual(self._cli(fixture, fixture.fake_fetch()), 0)
            stage = next(iter(list((self.state / "staging").glob("stage-*"))))
            # the staged legacy updater is copied byte-for-byte and never executed;
            # nothing staged is run by the CLI or by staging
            staged = stage / "scripts" / "update.sh"
            self.assertEqual(staged.read_text(), "# legacy updater\n")
            self.assertEqual(staged.stat().st_mode & 0o777, 0o755)
            tampered = stage / "src" / "server.py"
            tampered.write_text("evil = True\n")
            with self.assertRaises(identity.UpdateError):
                update_staging.verify_stage(stage, fixture.identity())


if __name__ == "__main__":
    unittest.main()
