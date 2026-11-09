"""Revision evidence uses owned real Git fixtures, without providers or Rex."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import factory
import review_evidence


class RevisionEvidence(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        environment = factory.git_environment()
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
        clean = patch.dict(os.environ, environment, clear=True)
        clean.start()
        self.addCleanup(clean.stop)
        self.git("init", "-q", "--initial-branch=main")
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                           ("commit.gpgsign", "false"), ("core.hooksPath", os.devnull)):
            self.git("config", key, value)
        (self.repo / "README.md").write_bytes(b"original\r\n")
        (self.repo / "removed.txt").write_text("removed\n")
        self.git("add", "--all")
        self.git("commit", "-qm", "Base")
        self.base = self.git("rev-parse", "HEAD").decode().strip()
        (self.repo / "README.md").write_bytes(b"candidate\r\n")
        (self.repo / "removed.txt").unlink()
        (self.repo / "added.txt").write_text("added\n")
        (self.repo / "odd\tname.txt").write_text("odd path\n")
        self.git("add", "--all")
        self.git("commit", "-qm", "Candidate")
        self.candidate = self.git("rev-parse", "HEAD").decode().strip()

    def git(self, *arguments):
        result = subprocess.run([factory.git_binary(), "-C", str(self.repo), *arguments], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def render(self, **changes):
        return review_evidence.render(self.repo, **{**dict(candidate=self.candidate,
            requirements="requirements-1", base=self.base, role="code-review"), **changes})

    def validate(self, instructions, **changes):
        review_evidence.validate(instructions, **{**dict(candidate=self.candidate,
            requirements="requirements-1", base=self.base, role="code-review"), **changes})

    def test_exact_range_paths_and_patch_are_frozen_despite_later_checkout_changes(self):
        instructions = "# Assignment: code-review\nReview the actual diff.\n" + self.render()
        value = json.loads(instructions.partition(review_evidence.MARKER)[2])
        self.assertEqual((value["base"], value["candidate"], value["requirements"]),
                         (self.base, self.candidate, "requirements-1"))
        self.assertEqual(value["paths"], ["README.md", "added.txt", "odd\tname.txt"])
        self.assertEqual(value["changed_paths"], ["README.md", "added.txt", "odd\tname.txt", "removed.txt"])
        self.assertIn("-original\r\n+candidate\r\n", value["diff"])
        self.assertIn("deleted file mode 100644", value["diff"])
        self.validate(instructions)
        (self.repo / "README.md").write_text("later unrelated edit\n")
        (self.repo / "untracked.txt").write_text("not in candidate\n")
        self.git("add", "README.md")
        self.git("commit", "-qm", "Later unrelated commit")
        self.assertEqual(self.render(), instructions.partition(review_evidence.MARKER)[1] + instructions.partition(review_evidence.MARKER)[2])

    def test_inventory_only_does_not_invent_a_review_base(self):
        text = self.render(base=None, role="qa")
        value = json.loads(text[len(review_evidence.MARKER):])
        self.assertIsNone(value["base"])
        self.assertIsNone(value["diff"])
        self.assertIsNone(value["changed_paths"])
        self.assertEqual(value["paths"], ["README.md", "added.txt", "odd\tname.txt"])
        self.validate("# Assignment: qa\n" + text, base=None, role="qa")
        self.validate("Historical QA brief", base=None, role="qa")

    def test_missing_alias_and_unavailable_base_refuse(self):
        for base in (None, "HEAD", "HEAD~1", "--help", "a" * 40):
            with self.subTest(base=base), self.assertRaises(factory.FactoryError):
                self.render(base=base)
        with self.assertRaises(factory.FactoryError):
            self.validate("# Assignment: code-review\nMissing evidence")

    def test_damaged_or_revision_mismatched_evidence_refuses(self):
        original = json.loads(self.render()[len(review_evidence.MARKER):])
        for changed in (dict(schema_version=True), dict(schema_version=2), dict(candidate=self.base),
                        dict(base=self.candidate), dict(requirements="other"), dict(paths=[None]),
                        dict(changed_paths=None), dict(diff=None)):
            damaged = {**copy.deepcopy(original), **changed}
            with self.subTest(changed=changed), self.assertRaises(factory.FactoryError):
                self.validate("# Assignment: code-review\n" + review_evidence.MARKER + json.dumps(damaged))
        with self.assertRaises(factory.FactoryError):
            self.validate("# Assignment: code-review\n" + review_evidence.MARKER + "{broken")

    def test_external_diff_and_text_conversion_never_execute(self):
        sentinel = self.repo / "callback-ran"
        helper = self.repo / "helper.sh"
        helper.write_text("#!/bin/sh\nset -eu\ntouch " + str(sentinel) + "\n")
        helper.chmod(0o700)
        self.git("config", "diff.external", str(helper))
        self.git("config", "diff.fixture.textconv", str(helper))
        (self.repo / ".gitattributes").write_text("*.txt diff=fixture\n*.md diff=fixture\n")
        value = json.loads(self.render()[len(review_evidence.MARKER):])
        self.assertIn("+candidate\r\n", value["diff"])
        self.assertFalse(sentinel.exists())
