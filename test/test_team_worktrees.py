import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
from team_worktrees import WorktreeError, provision_edit_worktree


class TeamWorktreeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.git = shutil.which("git")
        if not self.git:
            self.skipTest("git is required for local worktree integration tests")
        self.git_run(self.source, "init", "--quiet")
        (self.source / "shared.txt").write_text("base\n")
        self.git_run(self.source, "add", "shared.txt")
        self.git_run(self.source, "-c", "user.name=Test", "-c", "user.email=test@localhost",
                     "commit", "--quiet", "-m", "base")
        self.base = self.git_run(self.source, "rev-parse", "HEAD")
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    def git_run(self, cwd, *args):
        return subprocess.check_output([self.git, "-C", str(cwd), *args], text=True).strip()

    def provision(self, worker):
        return provision_edit_worktree(str(self.source), self.state, "run-1", worker,
                                       self.base, self.git)

    def test_two_editors_get_separate_branches_and_reuse_without_reset(self):
        # The source checkout has uncommitted work; provisioning must leave it alone.
        (self.source / "shared.txt").write_text("source local edit\n")
        alice = self.provision("alice")
        bob = self.provision("bob")
        self.assertNotEqual(alice["cwd"], bob["cwd"])
        self.assertNotEqual(alice["branch"], bob["branch"])
        self.assertEqual(Path(alice["cwd"], "shared.txt").read_text(), "base\n")
        Path(alice["cwd"], "shared.txt").write_text("alice edit\n")
        again = self.provision("alice")
        self.assertEqual(again, alice)
        self.assertEqual(Path(again["cwd"], "shared.txt").read_text(), "alice edit\n")
        self.assertEqual((self.source / "shared.txt").read_text(), "source local edit\n")
        self.assertEqual(self.git_run(alice["cwd"], "branch", "--show-current"), alice["branch"])
        self.assertEqual(alice["base_revision"], self.base)

    def test_existing_unowned_path_is_refused_and_preserved(self):
        run_key = hashlib.sha256(b"run-1").hexdigest()[:20]
        worker_key = hashlib.sha256(b"mallory").hexdigest()[:20]
        target = self.state / "worktrees" / run_key / worker_key
        target.mkdir(parents=True)
        marker = target / "keep.txt"
        marker.write_text("unowned data")
        with self.assertRaisesRegex(WorktreeError, "exists but is not a Git worktree"):
            self.provision("mallory")
        self.assertEqual(marker.read_text(), "unowned data")

    def test_non_repository_source_and_invalid_base_fail_explicitly(self):
        invalid = self.root / "not-a-repo"
        invalid.mkdir()
        with self.assertRaises(WorktreeError):
            provision_edit_worktree(str(invalid), self.state, "run", "worker", self.base, self.git)
        with self.assertRaisesRegex(WorktreeError, "full 40- or 64-character"):
            provision_edit_worktree(str(self.source), self.state, "run", "worker", "HEAD", self.git)
        self.assertEqual(self.git_run(self.source, "status", "--porcelain"), "")

    def test_state_inside_source_is_rejected_before_creating_files(self):
        state = self.source / ".agent-state"
        with self.assertRaisesRegex(WorktreeError, "outside the source checkout"):
            provision_edit_worktree(str(self.source), state, "run", "worker", self.base, self.git)
        self.assertFalse(state.exists())

    def test_existing_path_with_wrong_branch_is_rejected_without_moving_it(self):
        first = self.provision("worker")
        self.git_run(first["cwd"], "checkout", "--quiet", "-b", "someone-elses-branch")
        with self.assertRaisesRegex(WorktreeError, "unexpected branch"):
            self.provision("worker")
        self.assertEqual(self.git_run(first["cwd"], "rev-parse", "HEAD"), self.base)

    def test_state_path_that_is_a_file_is_refused(self):
        self.state.write_text("not a directory")
        with self.assertRaisesRegex(WorktreeError, "state_dir must be a usable directory"):
            self.provision("worker")

    def test_detached_owned_worktree_is_reported(self):
        first = self.provision("worker")
        self.git_run(first["cwd"], "checkout", "--quiet", "--detach", self.base)
        with self.assertRaisesRegex(WorktreeError, "detached HEAD"):
            self.provision("worker")

    def test_worktree_creation_does_not_run_checkout_hooks(self):
        marker = self.root / "hook-ran"
        hook = self.source / ".git" / "hooks" / "post-checkout"
        hook.write_text("#!/bin/sh\ntouch {}\n".format(marker))
        hook.chmod(0o700)
        self.provision("worker")
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
