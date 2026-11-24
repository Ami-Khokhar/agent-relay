import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.team_history import TeamHistoryError, export_run_history
from src.teams import TeamStore


class TeamHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = TeamStore(self.root / "teams.sqlite")
        self.git = shutil.which("git")
        if not self.git:
            self.skipTest("git is required for local history integration tests")
        self.repo = self.root / "history"

    def tearDown(self):
        self.temp.cleanup()

    def test_exports_reviewable_selected_records_and_retrieves_them_at_sha(self):
        run = self.store.create_run("Build the project", "lead", [{"id": "worker"}])
        self.store.set_phase(run["id"], "lead", "specification", "Approved design", approved=True)
        thread = self.store.post_message(run["id"], "lead", "worker", "PRIVATE TRANSCRIPT bearer_secret")
        self.store.conclude(run["id"], "lead", thread, "Choose local storage", "It is durable", action="implement")
        result = export_run_history(self.store, run["id"], self.repo, self.git)

        self.assertTrue(result["changed"])
        self.assertEqual(len(result["commit"]), 40)
        self.assertIn("runs/{}/specification.md".format(run["id"]), result["paths"])
        self.assertIn("runs/{}/decisions.json".format(run["id"]), result["paths"])
        shown = subprocess.check_output([self.git, "-C", str(self.repo), "show", result["commit"]], text=True)
        self.assertIn("Approved design", shown)
        self.assertIn("Choose local storage", shown)
        self.assertNotIn("PRIVATE TRANSCRIPT", shown)
        self.assertNotIn("bearer_secret", shown)
        decisions = json.loads((self.repo / "runs" / run["id"] / "decisions.json").read_text())
        self.assertEqual(decisions[0]["rationale"], "It is durable")

    def test_unapproved_spec_is_labeled_draft(self):
        run = self.store.create_run("Build", "lead", [])
        self.store.set_phase(run["id"], "lead", "specification", "Not approved yet")
        result = export_run_history(self.store, run["id"], self.repo, self.git)
        spec = subprocess.check_output([self.git, "-C", str(self.repo), "show",
                                        result["commit"] + ":runs/{}/specification.md".format(run["id"])], text=True)
        self.assertIn("# Draft specification", spec)
        self.assertNotIn("# Approved specification", spec)

    def test_unchanged_export_returns_same_sha_without_duplicate_commit(self):
        run = self.store.create_run("Build", "lead", [])
        first = export_run_history(self.store, run["id"], self.repo, self.git)
        second = export_run_history(self.store, run["id"], self.repo, self.git)
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(first["commit"], second["commit"])
        count = subprocess.check_output([self.git, "-C", str(self.repo), "rev-list", "--count", "HEAD"], text=True)
        self.assertEqual(count.strip(), "1")

    def test_git_failure_is_visible_and_does_not_delete_database_history(self):
        run = self.store.create_run("Keep this run", "lead", [])
        with self.assertRaisesRegex(TeamHistoryError, "git_path"):
            export_run_history(self.store, run["id"], self.repo, "/missing/trusted-git")
        self.assertEqual(self.store.get_run(run["id"])["objective"], "Keep this run")
        self.assertFalse(self.repo.exists())

    def test_refuses_to_commit_pre_staged_files_from_another_run(self):
        run = self.store.create_run("Build", "lead", [])
        export_run_history(self.store, run["id"], self.repo, self.git)
        outside = self.repo / "unrelated.txt"
        outside.write_text("keep separate")
        subprocess.check_call([self.git, "-C", str(self.repo), "add", "unrelated.txt"])
        with self.assertRaisesRegex(TeamHistoryError, "outside this run"):
            export_run_history(self.store, run["id"], self.repo, self.git)
        self.assertFalse(subprocess.check_output([self.git, "-C", str(self.repo), "status", "--porcelain"], text=True) == "")

    def test_commit_does_not_run_repository_hooks(self):
        run = self.store.create_run("Build", "lead", [])
        export_run_history(self.store, run["id"], self.repo, self.git)
        hooks = self.repo / ".git" / "hooks"
        marker = self.root / "hook-ran"
        hook = hooks / "pre-commit"
        hook.write_text("#!/bin/sh\ntouch {}\n".format(marker))
        hook.chmod(0o700)
        self.store.set_phase(run["id"], "lead", "prototype")
        export_run_history(self.store, run["id"], self.repo, self.git)
        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
