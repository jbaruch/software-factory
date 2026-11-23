"""Fixed GitHub/process transports and temporary Git only; no external services."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

import codex_capacity
import codex_worker
import factory
import integration
import release_evidence
import test_codex_worker as fixtures
from test_rex_launcher import FakeRex


class GitHub:
    def __init__(self, base, head, merge):
        self.identity: dict = dict(repository="fixture/project", repository_id=11, number=7,
            url="https://github.com/fixture/project/pull/7", head=head,
            head_repository_id=11, head_ref="candidate", base=base, target="main")
        self.pr: dict = dict(number=7, html_url=self.identity["url"], state="open", merged=False,
            head=dict(sha=head, ref="candidate", repo=dict(id=11, full_name="fixture/project")),
            base=dict(sha=base, ref="main", repo=dict(id=11, full_name="fixture/project")), merge_commit_sha=None)
        body = dict(id=1, author="reviewer", body="Original historical full body\nNo regex verdict.",
                    html_url="https://github.com/fixture/project/pull/7#pullrequestreview-1",
                    state="DISMISSED", submitted_at="2026-11-07", commit_id=head)
        self.snapshot_value: dict = dict(pr_number=7, head_sha=head,
            ci=dict(status="success", checks=[], actions=dict(status="success", runs=[dict(id=61, workflow_id=51,
                path=".github/workflows/checks.yml", run_number=7, run_attempt=1, status="completed", conclusion="success")])),
            reviews={lane: dict(state="COMMENTED", commit_id=head, stale=False) for lane in ("codex", "claude")},
            review_bodies=[body], inline_comment_bodies=[dict(id=2, author="someone", body="Reply\nAdvisory.",
                html_url="https://github.com/fixture/project/pull/7#discussion_r2", path="file", line=1, in_reply_to_id=1)],
            merge_state=dict(status="CLEAN", mergeable="MERGEABLE"))
        self.merge_sha = merge
        self.compare = dict(status="identical", merge_base_commit=dict(sha=merge))

    def binding(self):
        return {"installed-snapshot": "fixture-unchanged-policy", "gh": "fixture-cli"}

    def api(self, endpoint):
        root = "repos/fixture/project/"
        values = {"pulls/7": self.pr, "git/ref/heads/main": dict(object=dict(sha=self.merge_sha)),
            "compare/" + self.merge_sha + "..." + self.merge_sha: self.compare}
        if not endpoint.startswith(root) or endpoint[len(root):] not in values:
            raise AssertionError("Unexpected API call: " + endpoint)
        return copy.deepcopy(values[endpoint[len(root):]])

    def snapshot(self, identity):
        assert all(identity[key] == value for key, value in self.identity.items() if key != "base")
        return copy.deepcopy(self.snapshot_value)

    def issues(self, identity):
        return dict(pr=identity["number"], verdict="ok", state="OPEN",
                    closing=[], partial=[], partial_references=[], no_issue=True)


class EvidenceRex(FakeRex):
    running = False

    def call(self, *args, **kwargs):
        if args[:2] == ("block", "call") and args[-1] == "process":
            type(self).calls.append(args)
            return dict(child=dict(pid=123)) if self.running else dict(last_exit=dict(exit_code=0))
        return super().call(*args, **kwargs)


class Integration(unittest.TestCase):
    def setUp(self):
        servers = patch.object(codex_worker, "configured_mcp_servers", return_value=[])  # No real `codex mcp list` (#153).
        servers.start()
        self.addCleanup(servers.stop)
        self.native = fixtures.NativeResults()
        self.native.setUp()
        self.addCleanup(self.native.doCleanups)
        self.root = self.native.root.resolve()
        self.repo = self.native.repo.resolve()
        self.base = self.native.candidate
        result = factory.git_run(str(self.repo), "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                                 "commit", "--quiet", "--allow-empty", "-m", "Candidate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.head = factory.git_commit(str(self.repo), "HEAD")
        tree = factory.git_run(str(self.repo), "rev-parse", "HEAD^{tree}").stdout.strip()
        merged = factory.git_run(str(self.repo), "-c", "commit.gpgsign=false", "commit-tree", tree,
                                 "-p", self.base, "-p", self.head, "-m", "Merge fixture")
        self.assertEqual(merged.returncode, 0, merged.stderr)
        self.transport = GitHub(self.base, self.head, merged.stdout.strip())
        self.tasks = self.root / "tasks"
        self.tasks.mkdir()
        self.path = self.tasks / "deliver.json"
        factory.git_run(str(self.repo), "remote", "add", "origin", "https://github.com/fixture/project.git")
        factory.admit(self.path, task="deliver", goal="Deliver fixture", repository=str(self.repo), acceptance=["original-requirements"],
                      corrections=2, investigations=1)
        self.record = factory.load_task(self.path)
        self.frozen = release_evidence.capture(self.record, self.transport.identity["url"], self.transport)
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        EvidenceRex.running = False
        rex_patch = patch.object(factory, "Rex", EvidenceRex)
        rex_patch.start()
        self.addCleanup(rex_patch.stop)
        catalog = patch.object(codex_capacity, "snapshot", self.codex_catalog)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.review("qa", "qa", frozen=False)
        self.review("release", "release")

    @staticmethod
    async def codex_catalog(binary):
        """The release judgment's effort check reads Codex's catalog; the fixture model tops out at high."""
        return dict(models=[dict(model="fixture-model", effort_ids=["low", "medium", "high"])])

    def review(self, key, role, *, context=None, frozen=True, outcome="approved", writer=False, delivery=None, purpose="work", failed=False, change=None, legacy=False, cwd=None, collect=True):
        cwd = self.repo if cwd is None else cwd
        frozen_release = self.frozen if frozen and delivery is None else None
        # A release judgment ID freezes the `delivered` receipt that confirmed it (#151).
        frozen_delivery = release_evidence.delivered_receipt(factory.load_task(self.path), delivery) if delivery else None
        command = factory.codex_command(self.path, task="deliver", assignment_id=key, codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(cwd), role=role, write=writer,
            commit_message="fix(fixture): Apply correction\n\nExercise sequential writer provenance." if change else None,
            release_evidence=frozen_release, delivery_evidence=frozen_delivery)
        if legacy:
            kind, payload = ("delivery", frozen_delivery) if delivery else ("release", frozen_release)
            index = command.index("--" + kind + "-evidence-digest")
            command[index:index + 2] = ["--" + kind + "-evidence", json.dumps(payload, sort_keys=True)]
        with patch.object(factory, "Rex", EvidenceRex):
            record, assignment = factory.launch(self.path, task="deliver", assignment_id=key, role=role,
                goal="Judge the original complete evidence", cwd=str(cwd), command=command, purpose=purpose,
                binary=sys.executable, server="unix:///fixture.sock",
                frozen_release=frozen_release, frozen_delivery=frozen_delivery)
        return self.finish_review(key, record, assignment, cwd, context=context, outcome=outcome,
                                  failed=failed, change=change, collect=collect)

    def finish_review(self, key, record, assignment, cwd, *, context=None, outcome="approved", failed=False,
                      change=None, collect=True):
        command = assignment["request"]["command"]
        replacements = {"{repository}": str(self.repo), "{cwd}": str(cwd), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": self.head,
            "{requirements}": record["inputs"]["requirements"], "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        self.native.args = codex_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        events = fixtures.events(outcome)
        events[0]["thread_id"] = context or ("context-" + key)
        self.native.process = fixtures.FakeProcess(events[:2] if failed else events, timeout=failed, exit_code=-2 if failed else 0)
        spawn = self.native.spawn
        async def edit(*args, **kwargs):
            if change is not None:
                (cwd / "feature.txt").write_text(change)
            return await spawn(*args, **kwargs)
        previous_cwd = Path.cwd()
        try:
            os.chdir(cwd)
            with patch.object(self.native, "spawn", side_effect=edit):
                receipt = self.native.execute()
        finally:
            os.chdir(previous_cwd)
        if failed:
            self.assertEqual(receipt["state"], "failed")
            factory.record_result(self.path, assignment_id=key, candidate=self.head,
                requirements=record["inputs"]["requirements"], outcome="blocked", artifacts=[str(self.native.args.receipt)])
            return factory.load_task(self.path)["assignments"][key]
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        if not collect:
            return assignment
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_codex(self.path, key, None, record, assignment)
        return self.record["assignments"][key]

    def test_large_frozen_evidence_stays_in_original_brief_and_legacy_commands_still_read(self):
        self.frozen = copy.deepcopy(self.frozen)
        self.frozen["snapshot"]["review_bodies"][0]["body"] = "Original feedback 雪\n" * 100000
        self.frozen["review_digest"] = release_evidence.digest(release_evidence.feedback(self.frozen["snapshot"], self.frozen["pr"]))
        assignment = self.review("large-release", "release")
        self.assertLess(sum(len(part.encode()) for part in assignment["request"]["command"]), 8192)
        self.assertGreater(Path(assignment["brief"]).stat().st_size, 2000000)
        self.assertEqual(release_evidence.from_assignment(self.record, assignment), self.frozen)
        instructions = Path(assignment["brief"]).read_text()
        for damaged in (instructions + "tampered", instructions.replace("Original feedback", "Altered feedback", 1)):
            with self.assertRaises(factory.FactoryError):
                release_evidence.brief_payload(damaged, "release", release_evidence.digest(self.frozen))
        original = self.review("legacy-release", "release", legacy=True)
        self.assertIn("--release-evidence", original["request"]["command"])
        self.assertEqual(release_evidence.from_assignment(self.record, original), self.frozen)

    def test_a_release_brief_reads_back_after_later_releases_reword_it(self):
        # Rewording COVERAGE in 0.39.18 left eight nanoclaw deliveries unable to freeze their final acceptance.
        assignment = self.review("worded-release", "release")
        with patch.object(release_evidence, "COVERAGE", "Reworded coverage guidance. "):
            self.assertEqual(release_evidence.from_assignment(self.record, assignment), self.frozen)
            instructions = Path(assignment["brief"]).read_text()
            self.assertEqual(release_evidence.brief_payload(instructions, "release", release_evidence.digest(self.frozen)),
                             self.frozen)
            for damaged in (instructions.replace(release_evidence.MARKER, "\n"), instructions.rstrip("\n")):
                with self.subTest(damaged=damaged[-40:]), self.assertRaises(factory.FactoryError):
                    release_evidence.brief_payload(damaged, "release", release_evidence.digest(self.frozen))

    def test_codex_release_judgment_runs_at_its_models_maximum_effort_from_the_fresh_catalog(self):
        with self.assertRaisesRegex(factory.FactoryError, "maximum effort: select high, not medium"):
            command = factory.codex_command(self.path, task="deliver", assignment_id="lazy-release", codex=sys.executable,
                model="fixture-model", effort="medium", cwd=str(self.repo), role="release", release_evidence=self.frozen)
            with patch.object(factory, "Rex", FakeRex):
                factory.launch(self.path, task="deliver", assignment_id="lazy-release", role="release",
                    goal="Judge the original complete evidence", cwd=str(self.repo), command=command, purpose="work",
                    binary=sys.executable, server="unix:///fixture.sock", frozen_release=self.frozen)
        self.assertNotIn("lazy-release", factory.load_task(self.path)["assignments"])
        ranks = [(["low", "high", "xhigh"], "xhigh"), (["max", "ultra", "high"], "ultra"), (["none"], "none")]
        for efforts, top in ranks:
            release_evidence.require_max_effort(top, efforts)
        for efforts in ([], ["turbo"], None):
            with self.assertRaisesRegex(factory.FactoryError, "effort ordering"):
                release_evidence.require_max_effort("high", efforts)

    def release_skill_merges(self):
        """The repository's release skill merges the PR; the factory makes no merge call."""
        self.transport.pr.update(state="closed", merged=True, merge_commit_sha=self.transport.merge_sha)

    def confirm(self, **options):
        args = dict(pr=self.transport.identity["url"], review="release", required=["qa"], transport=self.transport)
        return integration.delivered(self.path, **{**args, **options})

    def test_release_skill_delivery_confirms_the_judged_head_merged_and_records_it_once(self):
        with self.assertRaisesRegex(factory.FactoryError, "not merged yet"):
            self.confirm()
        self.release_skill_merges()
        receipt = self.confirm()
        self.assertEqual((receipt["kind"], receipt["head"], receipt["merge_commit"]),
                         ("release-skill-delivery", self.head, self.transport.merge_sha))
        self.assertEqual(sorted(receipt["approvals"]), ["qa", "release"])
        self.assertEqual(Path(receipt["receipt_path"]), self.tasks / "deliver.release.delivered.json")
        self.assertEqual(self.confirm(), receipt)
        self.transport.merge_sha = "e" * 40  # A later observation that differs cannot replace the record.
        self.transport.pr["merge_commit_sha"] = self.transport.merge_sha
        self.transport.compare = dict(status="identical", merge_base_commit=dict(sha=self.transport.merge_sha))
        with self.assertRaisesRegex(factory.FactoryError, "different delivery is already recorded"):
            self.confirm()

    def test_release_skill_delivery_refuses_an_unjudged_head_another_pr_or_a_missing_judgment(self):
        self.release_skill_merges()
        self.transport.pr["head"]["sha"] = "f" * 40
        with self.assertRaisesRegex(factory.FactoryError, "never judged"):
            self.confirm()
        self.transport.pr["head"]["sha"] = self.head
        with self.assertRaisesRegex(factory.FactoryError, "differs from the one the release judgment approved"):
            self.confirm(pr="https://github.com/fixture/project/pull/8")
        with self.assertRaisesRegex(factory.FactoryError, "read-only approved"):
            self.confirm(review="qa", required=["release"])
        self.transport.compare = dict(status="diverged", merge_base_commit=dict(sha=self.base))
        with self.assertRaisesRegex(factory.FactoryError, "target ancestry"):
            self.confirm()
        self.assertFalse((self.tasks / "deliver.release.delivered.json").exists())

    def test_final_acceptance_judges_the_recorded_delivered_receipt(self):
        # The release skill's deliveries once had no evidence-bound final acceptance (#151).
        self.release_skill_merges()
        with self.assertRaisesRegex(factory.FactoryError, "No `delivered` receipt"):
            self.review("early", "release", delivery="release")
        receipt = self.confirm()
        self.review("accept", "release", delivery="release")
        record = factory.load_task(self.path)
        frozen = release_evidence.from_delivery_assignment(record, record["assignments"]["accept"])
        self.assertEqual((frozen["schema_version"], frozen["review"]), (3, "release"))
        self.assertEqual(frozen["receipt"], {key: value for key, value in receipt.items() if key != "receipt_path"})
        original = self.path.read_bytes()
        verdict = integration.delivered_accepted(self.path, "release", "accept")
        self.assertEqual((verdict["outcome"], verdict["review"], verdict["delivery_receipt_digest"]),
                         ("approved", "release", frozen["receipt_digest"]))
        self.assertEqual(self.path.read_bytes(), original)
        with self.assertRaisesRegex(factory.FactoryError, "approved current native release judgment"):
            integration.delivered_accepted(self.path, "release", "qa")
        self.review("reject", "release", delivery="release", outcome="findings")
        with self.assertRaisesRegex(factory.FactoryError, "approved current native release judgment"):
            integration.delivered_accepted(self.path, "release", "reject")
        command = ["--state-dir", str(self.tasks), "delivered", "--task", "deliver", "--pr", self.transport.identity["url"],
            "--review", "release", "--after", "qa", "--gh", "/installed/gh", "--acceptance", "accept",
            "--release-scripts", "/installed/.tessl/plugins/jbaruch/coding-policy/skills/release"]
        output = StringIO()
        with patch.object(release_evidence, "GitHub", return_value=self.transport), redirect_stdout(output):
            self.assertEqual(factory.main(command), 0)
        self.assertEqual(json.loads(output.getvalue())["acceptance"], verdict)
        # A damaged or unsupported receipt is no evidence for a new judgment.
        path = Path(receipt["receipt_path"])
        saved = path.read_text()
        for damage in (lambda value: value.pop("merge_commit"), lambda value: value.update(schema_version=2),
                       lambda value: value.update(extra=True), lambda value: value.update(required=[]),
                       lambda value: value["approvals"].update(qa=None),
                       lambda value: value["approvals"]["qa"].update(receipt="not-a-digest"),
                       lambda value: value["approvals"]["release"].pop("context"),
                       lambda value: value.update(required=["qa", "qa"]),
                       lambda value: value["approvals"]["qa"].update(receipt="0" * 64)):
            value = json.loads(saved)
            damage(value)
            path.write_text(json.dumps(value))
            with self.subTest(damage=value), self.assertRaisesRegex(factory.FactoryError, "Unsupported or damaged|differ from the task's collected"):
                release_evidence.delivered_receipt(factory.load_task(self.path), "release")
        path.write_text(saved)
        # A receipt changed after the judgment no longer matches what the judge assessed.
        changed = json.loads(path.read_text())
        changed["merge_commit"] = "e" * 40
        path.write_text(json.dumps(changed))
        with self.assertRaisesRegex(factory.FactoryError, "differs from the recorded `delivered` receipt"):
            integration.delivered_accepted(self.path, "release", "accept")

    def delivered_cli(self, acceptance=None, *, review="cleanup-release", required="cleanup-qa"):
        command = ["--state-dir", str(self.tasks), "delivered", "--task", "deliver", "--pr", self.transport.identity["url"],
            "--review", review, "--after", required, "--gh", "/installed/gh",
            "--release-scripts", "/installed/.tessl/plugins/jbaruch/coding-policy/skills/release"]
        if acceptance is not None:
            command.extend(["--acceptance", acceptance])
        output, errors = StringIO(), StringIO()
        with patch.object(release_evidence, "GitHub", return_value=self.transport), redirect_stdout(output), redirect_stderr(errors):
            code = factory.main(command)
        if code:
            self.assertEqual(output.getvalue(), "")
        return code, json.loads(output.getvalue()) if output.getvalue() else None, errors.getvalue()

    def test_mixed_runtimes_deliver_writer_candidate_and_accept_frozen_receipt(self):
        # Strictly collect with three launch prefixes, leaving the saved requests untouched.
        python = self.root / "previous-python"
        python.symlink_to(sys.executable)
        runtime = self.root / "previous-runtime"
        runtime.symlink_to(factory.ROOT, target_is_directory=True)
        for key, value in (("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
            factory.git_run(str(self.repo), "config", key, value)
        with patch.object(factory, "ROOT", runtime), patch.object(sys, "executable", str(python)):
            writer = self.review("previous-writer", "backend", writer=True, frozen=False,
                                 outcome="completed", change="original writer candidate\n")
        self.head = writer["result"]["candidate"]
        factory.set_inputs(self.path, candidate=self.head, requirements=self.record["inputs"]["requirements"])
        tree = factory.git_run(str(self.repo), "rev-parse", "HEAD^{tree}").stdout.strip()
        merge = factory.git_run(str(self.repo), "-c", "commit.gpgsign=false", "commit-tree", tree,
            "-p", self.base, "-p", self.head, "-m", "Merge original writer").stdout.strip()
        self.transport = GitHub(self.base, self.head, merge)
        self.frozen = release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)
        with patch.object(sys, "executable", str(python)):
            self.review("previous-reader", "qa", frozen=False)
        with patch.object(factory, "ROOT", runtime):
            self.review("omitted-reader", "qa", frozen=False)
            self.review("previous-release", "release")
        python.unlink()
        runtime.unlink()
        self.release_skill_merges()
        original = factory.load_task(self.path)["assignments"]
        artifacts = {}
        for assignment in original.values():
            command = assignment["request"]["command"]
            for name in (assignment["brief"], command[command.index("--receipt") + 1], command[command.index("--events") + 1]):
                artifacts[Path(name)] = Path(name).read_bytes()
        select = dict(review="previous-release", required="previous-reader")
        code, receipt, errors = self.delivered_cli(**select)
        self.assertEqual((code, errors), (0, ""))
        assert receipt is not None
        self.assertEqual((receipt["head"], receipt["merge_commit"]), (self.head, merge))
        self.assertEqual(self.delivered_cli(**select), (0, receipt, ""))

        with redirect_stdout(StringIO()), redirect_stderr(StringIO()) as errors:
            self.assertEqual(factory.main(self.native_cli("final", delivery="previous-release")), 0, errors.getvalue())
        record = factory.load_task(self.path)
        self.finish_review("final", record, record["assignments"]["final"], self.repo)
        code, accepted, errors = self.delivered_cli("final", **select)
        self.assertEqual((code, errors), (0, ""))
        assert accepted is not None
        frozen = release_evidence.digest({k: v for k, v in receipt.items() if k != "receipt_path"})
        self.assertEqual((accepted["acceptance"]["outcome"], accepted["acceptance"]["delivery_receipt_digest"]),
                         ("approved", frozen))
        with patch.object(factory, "ROOT", self.root / "another-install"):
            self.assertEqual(self.delivered_cli("final", **select), (0, accepted, ""))
        after = factory.load_task(self.path)["assignments"]
        self.assertEqual(original, {key: after[key] for key in original})
        self.assertEqual(artifacts, {path: path.read_bytes() for path in artifacts})

    def test_mixed_unselected_reader_requires_original_command_and_collected_artifacts(self):
        with patch.object(factory, "ROOT", self.root / "earlier"):
            previous = self.review("previous", "qa", frozen=False)
        self.release_skill_merges()
        original = factory.load_task(self.path)
        command = previous["request"]["command"]
        select = dict(review="release", required="qa")
        for flag, value in (("--job", "deliver/qa"), ("--brief", previous["brief"]), ("--repo", "/elsewhere"),
                            ("--requirements", "other"), ("--candidate", self.base), ("--model", "different"),
                            ("--receipt", str(self.tasks / "deliver.qa.codex.json")),
                            ("--events", str(self.tasks / "deliver.qa.codex.jsonl"))):
            with self.subTest(flag=flag):
                record = copy.deepcopy(original)
                record["assignments"]["previous"]["request"]["command"][command.index(flag) + 1] = value
                factory.save(self.path, record)
                self.assertEqual(self.delivered_cli(**select)[0], 1)
        for damage in ("unknown-worker", "duplicate-option", "abbreviation", "bad-value", "help", "reader-commit", "partial-decision", "different-result"):
            with self.subTest(damage=damage):
                record = copy.deepcopy(original)
                item = record["assignments"]["previous"]
                argv = item["request"]["command"]
                if damage == "unknown-worker":
                    argv[3] += ".old"
                elif damage == "duplicate-option":
                    argv.extend(["--job", "deliver/previous"])
                elif damage == "abbreviation":
                    argv[argv.index("--requirements")] = "--requirem"
                elif damage == "bad-value":
                    argv[argv.index("--seconds") + 1] = "private-fixture-text"
                elif damage == "help":
                    argv.append("--help")
                elif damage == "reader-commit":
                    argv.extend(["--commit-message", "Apply fixture change"])
                elif damage == "partial-decision":
                    argv.extend(["--decision-store", str(self.tasks)])
                else:
                    item["result"]["artifacts"].append("different-report")
                factory.save(self.path, record)
                code, output, error = self.delivered_cli(**select)
                self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
                self.assertIn("deliver/previous:", error)
                if damage in ("abbreviation", "bad-value", "help"):
                    self.assertIn("restore the original assignment arguments", error)
                    self.assertNotIn("private-fixture-text", error)
        factory.save(self.path, original)
        for name in (previous["brief"], command[command.index("--events") + 1], command[command.index("--receipt") + 1]):
            path = Path(name)
            content = path.read_bytes()
            with self.subTest(artifact=path.name):
                other = path.with_suffix(".copy")
                path.rename(other)
                try:
                    for damage in ("missing", "symlink", "corrupt"):
                        if damage == "symlink":
                            path.symlink_to(other)
                        elif damage == "corrupt":
                            path.write_text("{}\n")
                            path.chmod(0o600)
                        code, output, error = self.delivered_cli(**select)
                        self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
                        self.assertIn("deliver/previous:", error)
                        self.assertIn({".md": "brief", ".jsonl": "event", ".json": "receipt"}[path.suffix], error.lower())
                        self.assertRegex(error.lower(), "restore|inspect")
                        path.unlink(missing_ok=True)
                finally:
                    other.replace(path)
                self.assertEqual(path.read_bytes(), content)
        self.assertFalse((self.tasks / "deliver.release.delivered.json").exists())
        self.assertEqual(self.delivered_cli(**select)[0], 0)

    def test_codex_receipt_read_refusals_hide_saved_arguments(self):
        self.release_skill_merges()
        marker = "private-fixture-receipt"
        record = factory.load_task(self.path)
        argv = record["assignments"]["qa"]["request"]["command"]
        index = argv.index("--receipt") + 1
        receipt = Path(argv[index])
        alias = self.tasks / marker
        alias.symlink_to(receipt)
        argv[index] = str(alias)
        factory.save(self.path, record)
        read_error = "Cannot read native receipt for this assignment; inspect or restore the original artifact before collection"
        binding_error = "Restore the original native command bound to this task, assignment and local artifacts"
        for runtime, message in ((factory.ROOT, read_error), (self.root / "other-install", binding_error)):
            with self.subTest(runtime=runtime), patch.object(factory, "ROOT", runtime):
                code, output, error = self.delivered_cli(review="release", required="qa")
                self.assertEqual((code, output, error), (1, None, f"factory: deliver/qa: {message}\n"))
                self.assertNotIn(marker, error)

        argv[index] = str(receipt)
        factory.save(self.path, record)
        alias.unlink()
        receipt.rename(alias)
        receipt.symlink_to(alias)
        with patch.object(factory, "ROOT", self.root / "other-install"):
            self.assertEqual(self.delivered_cli(review="release", required="qa"),
                             (1, None, f"factory: deliver/qa: {read_error}\n"))

    def test_mixed_writer_context_refuses_release_and_final_self_review(self):
        with patch.object(factory, "ROOT", self.root / "earlier"):
            self.review("writer", "backend", writer=True, frozen=False, outcome="completed", context="writer-session")
        self.review("self-review", "release", context="writer-session")
        self.review("duplicate", "qa", frozen=False, context="context-release")
        self.release_skill_merges()
        self.assertIn("independent of contributors", self.delivered_cli(review="self-review", required="qa")[2])
        self.assertIn("distinct original native contexts", self.delivered_cli(review="release", required="duplicate")[2])
        select = dict(review="release", required="qa")
        self.assertEqual(self.delivered_cli(**select)[0], 0)
        with patch.object(factory, "ROOT", self.root / "earlier"):
            self.review("self-final", "release", delivery="release", context="writer-session")
        self.assertIn("contributor cannot judge", self.delivered_cli("self-final", **select)[2])

    def test_mixed_stale_approval_and_unverified_failed_writer_refuse(self):
        with patch.object(factory, "ROOT", self.root / "earlier"):
            self.review("previous-qa", "qa", frozen=False)
        original = self.path.read_bytes()
        self.release_skill_merges()
        for head, requirements in ((self.base, self.record["inputs"]["requirements"]), (self.head, "new-requirements")):
            with self.subTest(head=head, requirements=requirements):
                factory.set_inputs(self.path, candidate=head, requirements=requirements)
                changed = "candidate" if head == self.base else "requirements"
                code, output, error = self.delivered_cli(review="release", required="previous-qa")
                self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
                self.assertIn(f"deliver/previous-qa: stale result ({changed} changed)", error)
                self.assertIn("select a fresh native judgment", error)
                self.path.write_bytes(original)
        failed = self.review("failed", "backend", writer=True, failed=True, frozen=False, context="context-qa")
        self.assertIn("independent of contributors", self.delivered_cli(review="release", required="qa")[2])
        with patch.object(factory, "ROOT", self.root / "earlier"):
            code, output, error = self.delivered_cli(review="release", required="qa")
            self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
            self.assertIn("deliver/failed: cannot verify the historical writer's original contributor identity", error)
            self.assertIn("preserve the recorded attempt and ask the task owner", error)
            with self.assertRaisesRegex(factory.FactoryError, "deliver/failed: not a native Codex worker.*inspect its original"):
                factory.collect_codex(self.path, "failed", None, factory.load_task(self.path), failed)

    def test_mixed_manual_result_cannot_replace_first_native_collection(self):
        with patch.object(factory, "ROOT", self.root / "earlier"):
            assignment = self.review("manual", "qa", frozen=False, collect=False)
        command = assignment["request"]["command"]
        self.release_skill_merges()
        with patch.object(factory, "ROOT", self.root / "earlier"):
            code, output, error = self.delivered_cli(review="release", required="manual")
            self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
            self.assertIn("deliver/manual: missing native result; use codex-collect", error)
        code, output, error = self.delivered_cli(review="release", required="manual")
        self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
        self.assertIn("deliver/manual: missing native result; preserve the attempt", error)
        self.assertNotIn("codex-collect", error)
        receipt_path = command[command.index("--receipt") + 1]
        receipt = json.loads(Path(receipt_path).read_text())
        record = factory.record_result(self.path, assignment_id="manual", candidate=receipt["candidate"],
            requirements=receipt["inputs"]["requirements"], outcome=receipt["report"]["outcome"],
            artifacts=[receipt_path, *receipt["report"]["artifacts"]])
        self.assertFalse(record["assignments"]["manual"]["native_collected"])
        self.assertEqual(self.delivered_cli(review="release", required="manual")[0], 1)
        with self.assertRaisesRegex(factory.FactoryError, "not a native Codex worker"):
            factory.collect_codex(self.path, "manual", None, record, record["assignments"]["manual"])

    def test_delivery_refuses_when_owner_changed_during_observation(self):
        self.release_skill_merges()
        original = self.path.read_bytes()
        api = self.transport.api
        receipt = self.tasks / "deliver.release.delivered.json"
        for existing in (False, True):
            before = None
            if existing:
                confirmed = self.confirm()
                before = receipt.read_bytes()
                self.assertEqual(self.confirm(), confirmed)
            for change in ("inputs", "hold"):
                with self.subTest(existing=existing, change=change):
                    def observe(endpoint):
                        if "/compare/" in endpoint:
                            with factory.task_lock(self.tasks, "deliver"):
                                if change == "inputs":
                                    factory.set_inputs(self.path, candidate=self.head, requirements="revised")
                                else:
                                    factory.hold_task(self.path, "Operator hold")
                        return api(endpoint)
                    with patch.object(self.transport, "api", side_effect=observe):
                        code, output, error = self.delivered_cli(review="release", required="qa")
                    self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
                    self.assertIn("deliver/release: task changed during delivery observation", error)
                    self.assertEqual(receipt.read_bytes() if receipt.exists() else None, before)
                    self.assertNotEqual(self.path.read_bytes(), original)
                    self.path.write_bytes(original)

    def test_delivered_cli_prints_the_receipt(self):
        self.release_skill_merges()
        code, output, errors = self.delivered_cli(review="release", required="qa")
        self.assertEqual((code, errors), (0, ""))
        assert output is not None
        self.assertEqual(output["head"], self.head)

    def test_failed_contributor_keeps_original_failure_and_context(self):
        failed = self.review("failed-writer", "backend", writer=True, frozen=False, failed=True)
        command = failed["request"]["command"]
        paths = [Path(command[command.index(flag) + 1]) for flag in ("--receipt", "--events")]
        originals = [path.read_bytes() for path in paths]
        self.release_skill_merges()
        self.assertEqual(self.confirm()["head"], self.head)
        self.assertEqual(factory.load_task(self.path)["assignments"]["failed-writer"], failed)
        self.assertEqual([path.read_bytes() for path in paths], originals)
        self.assertEqual(json.loads(paths[0].read_text())["state"], "failed")

    def test_two_successful_sequential_writers_preserve_collected_provenance(self):
        for key, value in (("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
            factory.git_run(str(self.repo), "config", key, value)
        first = self.review("writer", "backend", frozen=False, writer=True, outcome="completed", change="first\n")
        first_receipt = Path(first["result"]["artifacts"][0])
        original_bytes = first_receipt.read_bytes()
        self.head = first["result"]["candidate"]
        factory.set_inputs(self.path, candidate=self.head, requirements=self.record["inputs"]["requirements"])
        second = self.review("correction", "backend", frozen=False, writer=True, outcome="completed",
            change="corrected\n", purpose="correction")
        self.head = second["result"]["candidate"]
        self.assertNotEqual(first["result"]["candidate"], self.head)
        factory.set_inputs(self.path, candidate=self.head, requirements=self.record["inputs"]["requirements"])
        tree = factory.git_run(str(self.repo), "rev-parse", "HEAD^{tree}").stdout.strip()
        merge = factory.git_run(str(self.repo), "-c", "commit.gpgsign=false", "commit-tree", tree,
            "-p", self.base, "-p", self.head, "-m", "Corrected merge").stdout.strip()
        self.transport = GitHub(self.base, self.head, merge)
        record = factory.load_task(self.path)
        self.frozen = release_evidence.capture(record, self.transport.identity["url"], self.transport)
        self.review("qa-new", "qa", frozen=False)
        self.review("release-new", "release")
        record = factory.load_task(self.path)
        self.assertEqual(integration.contributor_context("writer", record, first), "context-writer")
        with patch.object(factory, "status", return_value=dict(state="exited")):
            with self.assertRaisesRegex(factory.FactoryError, "worktree HEAD"):
                factory.collect_codex(self.path, "writer", None, record, first)
        self.release_skill_merges()
        self.assertEqual(self.confirm(review="release-new", required=["qa-new"])["head"], self.head)
        self.assertEqual(first_receipt.read_bytes(), original_bytes)
        self.assertEqual(factory.load_task(self.path)["assignments"]["writer"], first)
        self.assertEqual(factory.task_view(factory.load_task(self.path))["allowances"]["correction"]["used"], 1)
        damaged = copy.deepcopy(first)
        damaged["result"]["artifacts"].append("invented")
        with self.assertRaisesRegex(factory.FactoryError, "complete original"):
            integration.contributor_context("writer", factory.load_task(self.path), damaged)

    def native_cli(self, key, *, delivery=None):
        return ["--state-dir", str(self.tasks), "codex-start", "--task", "deliver", "--id", key,
            "--role", "release", "--goal", "Judge delivery", "--cwd", str(self.repo),
            "--codex", sys.executable, "--model", "fixture-model", "--effort", "high", "--purpose", "work",
            *(["--delivery", delivery] if delivery else ["--release-pr", self.transport.identity["url"],
                "--gh", "/installed/gh", "--release-scripts", "/installed/acr__jbaruch__coding-policy__release"])]

    def test_capture_stall_leaves_controls_and_unrelated_launches_available(self):
        entered, resume = threading.Event(), threading.Event()
        original = self.transport.snapshot
        def stall(*args, **kwargs):
            entered.set()
            if not resume.wait(10):
                raise AssertionError("Test controller did not release capture")
            return original(*args, **kwargs)
        output, errors, result = StringIO(), StringIO(), []
        with patch.object(release_evidence, "GitHub", return_value=self.transport), patch.object(self.transport, "snapshot", side_effect=stall):
            def capture():
                with redirect_stdout(output), redirect_stderr(errors):
                    result.append(factory.main(self.native_cli("stalled")))
            thread = threading.Thread(target=capture)
            thread.start()
            try:
                self.assertTrue(entered.wait(10))
                with factory.execution_lock(self.tasks), factory.task_lock(self.tasks, "unrelated") as other:
                    with patch.object(factory, "Rex", FakeRex):
                        factory.launch(other, task="unrelated", assignment_id="work", role="backend", goal="Other work",
                            cwd=str(self.repo), command=[sys.executable, "-c", "pass"], binary=sys.executable,
                            server="unix:///fixture.sock")
                with factory.task_lock(self.tasks, "deliver"):
                    factory.hold_task(self.path, "Paused during capture")
                    factory.set_inputs(self.path, candidate=self.head, requirements="changed-during-capture")
            finally:
                resume.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
        self.assertEqual(result, [1], errors.getvalue())
        self.assertIn("Task changed during evidence capture", errors.getvalue())
        self.assertNotIn("stalled", factory.load_task(self.path)["assignments"])

    def test_final_acceptance_cli_freezes_the_delivered_receipt(self):
        self.release_skill_merges()
        self.confirm()
        for delivery, code in (("release", 0), ("release", 0), ("qa", 1)):
            output, errors = StringIO(), StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                self.assertEqual(factory.main(self.native_cli("accept-cli", delivery=delivery)), code, errors.getvalue())
        self.assertIn("Original delivery differs", errors.getvalue())
        record = factory.load_task(self.path)
        frozen = release_evidence.from_delivery_assignment(record, record["assignments"]["accept-cli"])
        self.assertEqual(frozen, release_evidence.delivered_receipt(record, "release"))

    def test_failed_writer_recovers_thread_from_supported_original_partial_events(self):
        failed = self.review("failed-writer", "backend", writer=True, frozen=False, failed=True)
        command = failed["request"]["command"]
        path = Path(command[command.index("--receipt") + 1])
        receipt = json.loads(path.read_text())
        receipt.update(schema_version=4, thread_id=None)
        factory.save(path, receipt)
        before = path.read_bytes()
        self.release_skill_merges()
        self.assertEqual(self.confirm()["head"], self.head)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(factory.load_task(self.path)["assignments"]["failed-writer"], failed)

    def test_assignment_source_cannot_supply_installed_release_commands(self):
        self.transport.binding = lambda: {str(self.repo / "poll-pr-reviews.sh"): "writer-source"}
        with self.assertRaisesRegex(factory.FactoryError, "assignment checkout"):
            release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)

    def test_sibling_worktree_and_new_selected_cwd_helpers_refuse_before_capture(self):
        sibling = self.root / "sibling"
        worktree = factory.git_run(str(self.repo), "worktree", "add", "--quiet", "-b", "helper-fixture", str(sibling), self.head)
        self.assertEqual(worktree.returncode, 0, worktree.stderr)
        gh = self.root / "gh"
        gh.write_text("#!/bin/sh\nexit 99\n")
        gh.chmod(0o700)
        for cwd in (sibling, self.root / "new-checkout"):
            scripts = cwd / "acr__jbaruch__coding-policy__release"
            scripts.mkdir(parents=True)
            for name in ("poll-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
                (scripts / name).write_text("untrusted source")
            with patch.object(release_evidence.GitHub, "run", side_effect=AssertionError("Untrusted helper executed")):
                with self.assertRaisesRegex(factory.FactoryError, "outside source|assignment checkout"):
                    transport = release_evidence.GitHub(gh, scripts, self.repo)
                    release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], transport, cwd=str(cwd))

    def test_failed_writer_cannot_review_itself_and_unknown_identity_refuses(self):
        failed = self.review("failed-writer", "backend", context="context-qa", writer=True, frozen=False, failed=True)
        with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
            self.confirm()
        command = failed["request"]["command"]
        events = Path(command[command.index("--events") + 1])
        events.write_text("")
        with self.assertRaisesRegex(factory.FactoryError, "Unknown or ambiguous"):
            self.confirm()

    def test_prior_ordinary_qa_does_not_require_new_external_review_prose(self):
        qa = copy.deepcopy(factory.load_task(self.path)["assignments"]["qa"])
        self.assertNotIn("--release-evidence", qa["request"]["command"])
        self.transport.snapshot_value["review_bodies"][0]["body"] += " New advisory prose."
        self.frozen = release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)
        self.review("new-release", "release")
        self.release_skill_merges()
        self.assertEqual(self.confirm(review="new-release")["head"], self.head)
        self.assertEqual(factory.load_task(self.path)["assignments"]["qa"], qa)

    def test_bad_frozen_packet_versions_refuse(self):
        record = factory.load_task(self.path)
        for version in (None, 0, 4, True, "1"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.validate({**self.frozen, "schema_version": version}, record)
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported frozen"):
                release_evidence.render_delivery({"schema_version": version})
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported frozen"):
                release_evidence.render({**self.frozen, "schema_version": version})
        for version in (1, 2):  # Delivery evidence versions 1 and 2 froze the removed `integrate` path.
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported frozen"):
                release_evidence.render_delivery({"schema_version": version})
        # Version 2 carries the specialist roster; version 1 briefs, recorded before it, still validate.
        historical = {key: value for key, value in self.frozen.items() if key != "specialists"}
        release_evidence.validate({**historical, "schema_version": 1}, record)
        self.assertNotIn("ui-design", release_evidence.render({**historical, "schema_version": 1}))
        self.assertIn("ui-design", release_evidence.render(self.frozen))
        # A coverage-only gap is blocked, not findings, so it never reads as a defect round (nanoclaw, four judgments).
        self.assertIn("When missing specialists are the only gap, return blocked", release_evidence.render(self.frozen))
        # acr-232 named ui-design, then data-engineering a round later on the same head: name them all at once.
        self.assertIn("all in this one verdict", release_evidence.render(self.frozen))
        # port-mail-eagain still split the gap into two pairs; one ruling line per effect forces all four.
        self.assertIn("`ui-design: needed` or `ui-design: not needed`", release_evidence.render(self.frozen))
        identity, item = next(iter(record["assignments"].items()))
        row = dict(id=identity, role=item["request"]["role"], purpose=item["request"].get("purpose"), outcome=None, current=False)
        # Version 3 names the candidate each specialist judged, so the judge can carry an approval over
        # code a replay or correction left alone; version 2 briefs, recorded before it, still validate.
        release_evidence.validate({**self.frozen, "schema_version": 2, "specialists": [row]}, record)
        self.assertNotIn("earlier `candidate`", release_evidence.render({**self.frozen, "schema_version": 2, "specialists": [row]}))
        self.assertIn("earlier `candidate`", release_evidence.render(self.frozen))
        self.assertEqual(self.frozen["schema_version"], 3)
        judged = {row["id"]: row["candidate"] for row in release_evidence.specialists(record)}
        self.assertEqual(judged, {key: None if value.get("result") is None else value["result"]["candidate"]
                                  for key, value in record["assignments"].items()})
        self.assertTrue(any(judged.values()))
        for candidate in (None, "a" * 40, "b" * 64):
            release_evidence.validate({**self.frozen, "specialists": [{**row, "candidate": candidate}]}, record)
        for roster in (None, [{**row, "role": "ui-design-x", "candidate": None}], [{**row, "id": "absent", "candidate": None}],
                       [{**row, "candidate": None, "extra": 1}], [row], [{**row, "candidate": "HEAD"}],
                       [{**row, "candidate": 1}], [{**row, "candidate": "A" * 40}]):
            with self.subTest(roster=roster), self.assertRaises(factory.FactoryError):
                release_evidence.validate({**self.frozen, "specialists": roster}, record)
        with self.assertRaises(factory.FactoryError):
            release_evidence.validate({**self.frozen, "schema_version": 2, "specialists": [{**row, "candidate": None}]}, record)

    def test_original_process_and_host_identity_are_revalidated(self):
        EvidenceRex.running = True
        with self.assertRaisesRegex(factory.FactoryError, "has not exited"):
            self.confirm()
        EvidenceRex.running = False
        FakeRex.host = "different-host"
        with self.assertRaisesRegex(factory.FactoryError, "host differs"):
            self.confirm()

    def test_stale_requirements_refuse_before_mutation(self):
        factory.set_inputs(self.path, candidate=self.head, requirements="changed")
        self.release_skill_merges()
        with self.assertRaises(factory.FactoryError):
            self.confirm()
        self.assertFalse((self.tasks / "deliver.release.delivered.json").exists())

    def test_same_native_context_cannot_supply_two_approvals(self):
        self.review("duplicate", "security", context="context-release")
        with self.assertRaisesRegex(factory.FactoryError, "distinct original native contexts"):
            self.confirm(required=["duplicate"])

    def test_contributor_cannot_approve_own_work(self):
        self.review("writer", "backend", frozen=False, writer=True, context="context-qa", outcome="completed")
        with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
            self.confirm()

    def test_completed_is_not_an_approval(self):
        self.review("finished", "security", outcome="completed")
        with self.assertRaisesRegex(factory.FactoryError, r"finished \(role security, outcome completed\)"):
            self.confirm(required=["finished"])

    def test_hold_preserves_attempts_and_blocks_mutation(self):
        factory.hold_task(self.path, "Operator hold")
        before = self.path.read_bytes()
        self.release_skill_merges()
        with self.assertRaisesRegex(factory.FactoryError, "held"):
            self.confirm()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse((self.tasks / "deliver.release.delivered.json").exists())

    def test_missing_damaged_and_manually_recorded_native_evidence_refuses(self):
        original = factory.load_task(self.path)
        for mode in ("receipt", "events", "report"):
            with self.subTest(mode=mode):
                record = copy.deepcopy(original)
                assignment = record["assignments"]["qa"]
                command = assignment["request"]["command"]
                if mode == "report":
                    assignment["result"]["artifacts"] = ["manually-recorded-report"]
                    factory.save(self.path, record)
                    with self.assertRaises(factory.FactoryError):
                        self.confirm()
                else:
                    target = Path(command[command.index("--" + mode) + 1])
                    data = target.read_bytes()
                    target.write_text("{}\n")
                    try:
                        with self.assertRaises(factory.FactoryError):
                            self.confirm()
                    finally:
                        target.write_bytes(data)
                factory.save(self.path, original)

    def test_acceptance_replays_original_evidence_after_linked_worktree_cleanup(self):
        checkout = self.root / "completed-worktree"
        added = factory.git_run(str(self.repo), "worktree", "add", "--detach", str(checkout), self.head)
        self.assertEqual(added.returncode, 0, added.stderr)
        for key, value in (("commit.gpgsign", "false"), ("core.hooksPath", "/dev/null")):
            factory.git_run(str(self.repo), "config", key, value)
        writer = self.review("cleanup-writer", "backend", frozen=False, writer=True,
            outcome="completed", change="retained candidate\n", cwd=checkout)
        self.head = writer["result"]["candidate"]
        factory.set_inputs(self.path, candidate=self.head, requirements=self.record["inputs"]["requirements"])
        tree = factory.git_run(str(self.repo), "rev-parse", self.head + "^{tree}").stdout.strip()
        merge = factory.git_run(str(self.repo), "-c", "commit.gpgsign=false", "commit-tree", tree,
            "-p", self.base, "-p", self.head, "-m", "Cleanup merge").stdout.strip()
        self.transport = GitHub(self.base, self.head, merge)
        self.frozen = release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)
        self.review("cleanup-qa", "qa", frozen=False, cwd=checkout)
        self.review("cleanup-release", "release", cwd=checkout)
        self.release_skill_merges()
        self.confirm(review="cleanup-release", required=["cleanup-qa"])
        self.review("cleanup-accept", "release", delivery="cleanup-release", cwd=checkout)
        record = factory.load_task(self.path)
        verdict = integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept")
        self.assertEqual(verdict["outcome"], "approved")
        code, before, errors = self.delivered_cli("cleanup-accept")
        self.assertEqual((code, errors), (0, ""))
        assert before is not None
        self.assertEqual(before["acceptance"], verdict)
        panes = copy.deepcopy(FakeRex.sessions)
        originals = {self.path: self.path.read_bytes()}
        for assignment in record["assignments"].values():
            paths = [assignment["brief"], *assignment["result"]["artifacts"]]
            command = assignment["request"]["command"]
            if "--events" in command:
                paths.append(command[command.index("--events") + 1])
            for name in paths:
                path = Path(name)
                if path.is_file():
                    originals[path] = path.read_bytes()
        removed = factory.git_run(str(self.repo), "worktree", "remove", str(checkout))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(checkout.exists())
        self.assertEqual(integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept"), verdict)
        code, after, errors = self.delivered_cli("cleanup-accept")
        self.assertEqual((code, errors), (0, ""))
        self.assertEqual(after, before)
        self.assertEqual(FakeRex.sessions, panes)
        # An unrelated checkout at the old path supplies no historical authority.
        subprocess.run(["git", "init", "--quiet", str(checkout)], check=True)
        self.assertEqual(integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept"), verdict)
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original, str(path))

        damaged = copy.deepcopy(record["assignments"]["cleanup-accept"])
        damaged["result"]["artifacts"].append("invented")
        with self.assertRaisesRegex(factory.FactoryError, "complete original"):
            integration.collected_native("cleanup-accept", record, damaged)
        damaged["result"] = None
        with self.assertRaises(factory.FactoryError):
            factory.codex_evidence("cleanup-accept", record, damaged, collected=True,
                observe=lambda: dict(state="exited"))
        receipt_path = Path(writer["result"]["artifacts"][0])
        original = receipt_path.read_bytes()
        for field, wrong in (("parent", self.base), ("staged_tree", "0" * 40), ("candidate", self.base)):
            with self.subTest(provenance=field):
                receipt = json.loads(original)
                receipt["commit"][field] = wrong
                try:
                    receipt_path.write_text(json.dumps(receipt))
                    with self.assertRaises(factory.FactoryError):
                        integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept")
                finally:
                    receipt_path.write_bytes(original)
        receipt = json.loads(receipt_path.read_bytes())
        common = Path(record["admission"]["repository_identity"]["common_dir"])
        for object_id in (receipt["candidate"], receipt["commit"]["parent"], receipt["commit"]["staged_tree"]):
            with self.subTest(missing_object=object_id):
                object_path = common / "objects" / object_id[:2] / object_id[2:]
                original_object = object_path.read_bytes()
                try:
                    object_path.unlink()
                    with self.assertRaises(factory.FactoryError):
                        integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept")
                finally:
                    object_path.write_bytes(original_object)
        marker = Path(record["admission"]["repository_identity"]["common_dir"]) / factory.REPOSITORY_MARKER
        original = marker.read_bytes()
        try:
            identity = json.loads(original)
            identity["repository_id"] = "0" * 32
            marker.write_text(json.dumps(identity))
            with self.assertRaises(factory.FactoryError):
                integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept")
        finally:
            marker.write_bytes(original)
        factory.set_inputs(self.path, candidate=self.head, requirements="changed-requirements")
        with self.assertRaises(factory.FactoryError):
            integration.delivered_accepted(self.path, "cleanup-release", "cleanup-accept")

    def test_manual_native_result_cannot_supply_historical_approval_or_acceptance(self):
        for final in (False, True):
            with self.subTest(final_acceptance=final):
                checkout = self.root / ("manual-final" if final else "manual-release")
                added = factory.git_run(str(self.repo), "worktree", "add", "--detach", str(checkout), self.head)
                self.assertEqual(added.returncode, 0, added.stderr)
                if final:
                    self.release_skill_merges()
                    self.confirm()
                key = "manual-final" if final else "manual-release"
                assignment = self.review(key, "release", delivery="release" if final else None,
                    cwd=checkout, collect=False)
                command = assignment["request"]["command"]
                receipt_path = command[command.index("--receipt") + 1]
                receipt = json.loads(Path(receipt_path).read_text())
                report = receipt["report"]
                record = factory.record_result(self.path, assignment_id=key, candidate=receipt["candidate"],
                    requirements=receipt["inputs"]["requirements"], outcome=report["outcome"],
                    artifacts=list(dict.fromkeys([receipt_path, *report["artifacts"]])))
                removed = factory.git_run(str(self.repo), "worktree", "remove", str(checkout))
                self.assertEqual(removed.returncode, 0, removed.stderr)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(factory.FactoryError, "strict collection"):
                    if final:
                        integration.delivered_accepted(self.path, "release", key)
                    else:
                        integration.approvals(record, key, ["qa"])
                self.assertEqual(self.path.read_bytes(), before)

    def test_legacy_collection_needs_explicit_strict_recollection(self):
        checkout = self.root / "legacy-worktree"
        added = factory.git_run(str(self.repo), "worktree", "add", "--detach", str(checkout), self.head)
        self.assertEqual(added.returncode, 0, added.stderr)
        for key in ("legacy-recollected", "legacy-unproven"):
            self.review(key, "qa", frozen=False, cwd=checkout)
        original = factory.load_task(self.path)
        legacy = copy.deepcopy(original)
        legacy["schema_version"] = 8
        for assignment in legacy["assignments"].values():
            del assignment["native_collected"]
            assignment.pop("rex_instance", None)
        factory.save(self.path, legacy)
        artifacts = {}
        for assignment in original["assignments"].values():
            command = assignment["request"]["command"]
            for ref in [assignment["brief"], command[command.index("--events") + 1],
                    *assignment["result"]["artifacts"]]:
                path = Path(ref)
                if path.is_file():
                    artifacts[path] = path.read_bytes()
        with patch.object(factory, "status", side_effect=AssertionError("Migration cannot infer collection")):
            migrated = factory.load_task(self.path)
        self.assertEqual(migrated["schema_version"], 11)
        self.assertEqual({key: value for key, value in migrated.items() if key not in ("schema_version", "assignments")},
            {key: value for key, value in legacy.items() if key not in ("schema_version", "assignments")})
        for key, assignment in migrated["assignments"].items():
            self.assertEqual(assignment, {**legacy["assignments"][key], "native_collected": False})
        with self.assertRaisesRegex(factory.FactoryError, "strict collection"):
            integration.collected_native("legacy-recollected", migrated, migrated["assignments"]["legacy-recollected"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            recollected = factory.collect_codex(self.path, "legacy-recollected", None, migrated,
                migrated["assignments"]["legacy-recollected"])
        self.assertEqual(recollected["assignments"]["legacy-recollected"],
            {key: value for key, value in original["assignments"]["legacy-recollected"].items() if key != "rex_instance"})
        removed = factory.git_run(str(self.repo), "worktree", "remove", str(checkout))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        integration.collected_native("legacy-recollected", recollected, recollected["assignments"]["legacy-recollected"])
        with self.assertRaisesRegex(factory.FactoryError, "strict collection"):
            integration.collected_native("legacy-unproven", recollected, recollected["assignments"]["legacy-unproven"])
        before = self.path.read_bytes()
        with patch.object(factory, "status", return_value=dict(state="exited")):
            with self.assertRaises(factory.FactoryError):
                factory.collect_codex(self.path, "legacy-unproven", None, recollected,
                    recollected["assignments"]["legacy-unproven"])
        self.assertEqual(self.path.read_bytes(), before)
        for path, content in artifacts.items():
            self.assertEqual(path.read_bytes(), content)

    def test_first_collection_refuses_removed_original_checkout(self):
        checkout = self.root / "uncollected-worktree"
        added = factory.git_run(str(self.repo), "worktree", "add", "--detach", str(checkout), self.head)
        self.assertEqual(added.returncode, 0, added.stderr)
        assignment = self.review("uncollected-qa", "qa", frozen=False, cwd=checkout, collect=False)
        removed = factory.git_run(str(self.repo), "worktree", "remove", str(checkout))
        self.assertEqual(removed.returncode, 0, removed.stderr)
        with patch.object(factory, "status", return_value=dict(state="exited")):
            with self.assertRaises(factory.FactoryError):
                factory.collect_codex(self.path, "uncollected-qa", None, factory.load_task(self.path), assignment)
        self.assertIsNone(factory.load_task(self.path)["assignments"]["uncollected-qa"]["result"])

    def test_missing_canonical_bodies_or_authors_refuse(self):
        original = copy.deepcopy(self.transport.snapshot_value)
        for mode in ("missing", "author", "id", "ci"):
            self.transport.snapshot_value = copy.deepcopy(original)
            if mode == "missing":
                del self.transport.snapshot_value["review_bodies"]
            elif mode == "ci":
                self.transport.snapshot_value["ci"] = None
            else:
                self.transport.snapshot_value["inline_comment_bodies"][0][mode] = None
            with self.assertRaises(factory.FactoryError):
                release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)

    def test_origin_identity_cannot_be_substituted(self):
        factory.git_run(str(self.repo), "remote", "set-url", "origin", "https://github.com/different/project.git")
        with self.assertRaisesRegex(factory.FactoryError, "origin"):
            release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)

    def test_original_consumed_correction_survives_delivery(self):
        self.review("prior-correction", "qa", purpose="correction")
        before = factory.task_view(factory.load_task(self.path))["allowances"]
        self.assertEqual(before["correction"]["used"], 1)
        self.release_skill_merges()
        self.confirm()
        self.confirm()
        self.assertEqual(factory.task_view(factory.load_task(self.path))["allowances"], before)

    def test_required_issue_checks_cannot_be_bypassed(self):
        original = self.transport.issues
        self.transport.issues = lambda identity: {**original(identity), "verdict": "unlinked"}
        with self.assertRaisesRegex(factory.FactoryError, "issue declaration"):
            release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)


class CommandAuthority(unittest.TestCase):
    def installed_transport(self, root):
        repo = root / "repo"
        repo.mkdir()
        gh = root / "gh"
        gh.write_text("#!/bin/sh\nprintf '%s\\n' pinned-gh\n")
        gh.chmod(0o700)
        scripts = root / "acr__jbaruch__coding-policy__release"
        scripts.mkdir()
        for name in ("poll-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
            (scripts / name).write_text("fixture installed helper")
        return release_evidence.GitHub(gh, scripts, repo)

    def test_exported_bash_functions_cannot_override_fixed_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = self.installed_transport(Path(folder).resolve())
            functions = {"BASH_FUNC_" + name + "%%": "() { printf '%s\\n' inherited-function-ran; }"
                for name in ("gh", "bash", "jq", "poll-pr-reviews.sh")}
            with patch.dict(os.environ, functions):
                environment = transport.environment()
            self.assertFalse(any(key.startswith("BASH_FUNC_") for key in environment))
            observed = subprocess.run([transport.bash, "-c", "gh"], env=environment,
                capture_output=True, text=True, check=True)
            self.assertEqual(observed.stdout, "pinned-gh\n")

    def test_package_manager_git_installation_is_not_a_writer_checkout(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            subprocess.run(["git", "init", "--quiet", str(root)], check=True)
            transport = self.installed_transport(root)
            self.assertEqual(len(transport.binding()), 6)

    def test_spawn_and_timeout_diagnostics_name_the_restoration(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = self.installed_transport(Path(folder).resolve())
            for failure in (FileNotFoundError("removed helper"), subprocess.TimeoutExpired("gh", 60)):
                with self.subTest(failure=type(failure).__name__), patch.object(release_evidence.subprocess, "run", side_effect=failure):
                    with self.assertRaises(factory.FactoryError) as caught:
                        transport.api("repos/fixture/project")
                    self.assertIn("restore the installed command", str(caught.exception))
                    self.assertIn("run the command again", str(caught.exception))
            Path(transport.poll).unlink()
            with self.assertRaisesRegex(factory.FactoryError, "restore an explicit installed poll-pr-reviews.sh"):
                transport.api("repos/fixture/project")

    def test_malformed_output_reports_contract_without_exposing_payload(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = self.installed_transport(Path(folder).resolve())
            for output in ("", '{"secret":', '{"a":1,"a":2}', '{"a":NaN}'):
                result = subprocess.CompletedProcess([], 0, stdout=output, stderr="")
                with self.subTest(output=output), patch.object(release_evidence.subprocess, "run", return_value=result):
                    with self.assertRaises(factory.FactoryError) as caught:
                        transport.api("repos/fixture/project")
                    message = str(caught.exception)
                    self.assertIn("output contract", message)
                    self.assertIn("run the command again", message)
                    self.assertNotIn(output if output else "secret", message)
            invalid = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            with patch.object(release_evidence.subprocess, "run", side_effect=invalid):
                with self.assertRaisesRegex(factory.FactoryError, "output contract.*run the command again"):
                    transport.api("repos/fixture/project")

    def test_canonical_snapshot_is_unmodified_and_cannot_use_an_environment_fake(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo = root / "repo"
            repo.mkdir()
            scripts = root / "acr__jbaruch__coding-policy__release"
            scripts.mkdir()
            gh = root / "gh"
            gh.write_text("fixture binary")
            gh.chmod(0o700)
            for name in ("poll-pr-reviews.sh", "check-closing-issues.py", "review-lanes.sh"):
                (scripts / name).write_text("fixture installed helper")
            transport = release_evidence.GitHub(gh, scripts, repo)
            before = transport.binding()
            self.assertIn("factory-issue-checker:v1", before)
            (scripts / "check-closing-issues.py").unlink()
            self.assertEqual(transport.binding(), before)
            process = subprocess.CompletedProcess([], 0, json.dumps(dict(pr_number=7)), "")
            overrides = dict(WATCH_PR_REVIEWS_POLL_CMD="/untrusted/fake", WATCH_PR_REVIEWS_BUDGET_SEC="1",
                GH_HOST="evil.example", GH_REPO="evil/project", GH_CONFIG_DIR=str(root / "auth"), BASH_ENV="/untrusted/startup")
            with patch.dict(os.environ, overrides), patch.object(release_evidence.subprocess, "run", return_value=process) as run:
                transport.snapshot(dict(repository="fixture/project", number=7))
            argv = run.call_args.args[0]
            self.assertEqual(argv, [transport.bash, str(scripts / "poll-pr-reviews.sh"), "fixture", "project", "7"])
            self.assertEqual(run.call_args.kwargs["timeout"], 60)
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("WATCH_PR_REVIEWS_POLL_CMD", environment)
            self.assertNotIn("WATCH_PR_REVIEWS_BUDGET_SEC", environment)
            self.assertNotIn("BASH_ENV", environment)
            self.assertNotIn("GH_REPO", environment)
            self.assertEqual(environment["GH_HOST"], "github.com")
            self.assertEqual(environment["GH_CONFIG_DIR"], str(root / "auth"))
            self.assertEqual(transport.binding(), before)

    def test_managed_ignored_helpers_are_allowed_but_source_and_replacements_refuse(self):
        # nanoclaw installs coding-policy through Tessl, the factory's own repository through ACR.
        for install in release_evidence.RELEASE_INSTALLS:
            with self.subTest(install=str(install)):
                self.check_managed_install(install)

    def check_managed_install(self, install):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo = root / "repo"
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            factory.git_run(str(repo), "config", "core.excludesFile", "/dev/null")
            scripts = repo / install
            scripts.mkdir(parents=True)
            (repo / ".git" / "info" / "exclude").write_text(install.parts[0] + "/\n")
            gh = root / "gh"
            gh.write_text("fixture binary")
            gh.chmod(0o700)
            for name in ("poll-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
                (scripts / name).write_text("installed helper")
                (scripts / name).chmod(0o600)
            transport = release_evidence.GitHub(gh, scripts, repo)
            self.assertIn(str(scripts / "poll-pr-reviews.sh"), transport.binding())
            target = scripts / "poll-pr-reviews.sh"
            target.chmod(0o666)
            with self.assertRaisesRegex(factory.FactoryError, "owner-controlled"):
                transport.binding()
            target.chmod(0o600)
            top = repo / install.parts[0]  # The outermost managed directory, six levels up for Tessl.
            top.chmod(0o777)
            with self.assertRaisesRegex(factory.FactoryError, "owner-controlled"):
                transport.binding()
            top.chmod(0o755)
            target.unlink()
            target.symlink_to(scripts / "review-lanes.sh")
            with self.assertRaises(factory.FactoryError):
                transport.binding()
            target.unlink()
            target.write_text("installed helper")
            # A tracked sibling, such as the shared hook host an ACR project commits as .codex/config.toml, can't
            # change the installed helpers (#171).
            host = repo / install.parts[0] / "config.toml"
            host.write_text("[hooks]\n")
            self.assertEqual(factory.git_run(str(repo), "add", "-f", str(host.relative_to(repo))).returncode, 0)
            self.assertIn(str(scripts / "poll-pr-reviews.sh"), transport.binding())
            factory.git_run(str(repo), "add", "-f", install.parts[0])
            with self.assertRaisesRegex(factory.FactoryError, "ignored, untracked"):
                transport.binding()

    def test_release_skill_directory_must_be_an_acr_or_tessl_installation(self):
        for name in ("/opt/acr__jbaruch__coding-policy__release", "/home/u/.tessl/plugins/jbaruch/coding-policy/skills/release"):
            self.assertTrue(release_evidence.release_install(Path(name)), name)
        with tempfile.TemporaryDirectory() as folder:
            gh, repo = Path(folder).resolve() / "gh", Path(folder).resolve() / "repo"
            gh.write_text("fixture binary")
            gh.chmod(0o700)
            repo.mkdir()
            for name in ("/opt/release", "/home/u/.tessl/plugins/someone/coding-policy/skills/release",
                         "/opt/acr__jbaruch__coding-policy__release/nested"):
                self.assertFalse(release_evidence.release_install(Path(name)), name)
                with self.assertRaisesRegex(factory.FactoryError, "ACR or Tessl"):
                    release_evidence.GitHub(gh, Path(name), repo)

    def test_unvalidated_paths_and_urls_refuse(self):
        for url in ("https://evil.example/x/y/pull/1", "https://github.com/x/y/pull/1;echo", "https://github.com/x/y/pull/0"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.pr_url(url)
        for binary in ("gh", "/bin/sh", "/not-installed/gh"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.GitHub(binary, Path("/not-installed/release"), "/tmp")


class IssueDeclarations(unittest.TestCase):
    def setUp(self):
        self.identity = dict(repository="fixture/project", number=7,
            url="https://github.com/fixture/project/pull/7", head="a" * 40)
        self.pr = dict(number=7, url=self.identity["url"], headRefOid=self.identity["head"],
            state="OPEN", body="No issue\n", closingIssuesReferences=dict(totalCount=0,
                pageInfo=dict(hasNextPage=False), nodes=[]))
        self.transport = object.__new__(release_evidence.GitHub)
        self.transport.gh = "/installed/gh"
        self.transport.run = lambda *args, **kwargs: dict(data=dict(repository=dict(
            nameWithOwner="fixture/project", pullRequest=copy.deepcopy(self.pr))))
        self.requests = []
        def api(endpoint):
            self.requests.append(endpoint)
            repository, number = endpoint.removeprefix("repos/").rsplit("/issues/", 1)
            return dict(number=int(number), html_url=f"https://github.com/{repository}/issues/{number}", state="open")
        self.transport.api = api

    def test_no_issue_and_complete_closing_links_are_source_bound(self):
        self.assertTrue(self.transport.issues(self.identity)["no_issue"])
        issue = dict(number=2, url="https://github.com/fixture/project/issues/2", state="OPEN",
                     repository=dict(nameWithOwner="fixture/project"))
        self.pr.update(body="Closes #2", closingIssuesReferences=dict(totalCount=1,
            pageInfo=dict(hasNextPage=False), nodes=[issue]))
        self.assertEqual(self.transport.issues(self.identity)["closing"], [issue])

    def test_incomplete_or_changed_native_evidence_never_passes(self):
        cases = [dict(headRefOid="b" * 40), dict(state="CLOSED"), dict(number=8),
            dict(closingIssuesReferences=dict(totalCount=1, pageInfo=dict(hasNextPage=False), nodes=[])),
            dict(closingIssuesReferences=dict(totalCount=0, pageInfo=dict(hasNextPage=True), nodes=[])),
            dict(closingIssuesReferences=None)]
        for changes in cases:
            with self.subTest(changes=changes), patch.dict(self.pr, changes):
                with self.assertRaisesRegex(factory.FactoryError, "complete source-bound"):
                    self.transport.issues(self.identity)

    def test_supported_partial_declarations_resolve_issues_without_closing_them(self):
        self.pr["body"] = "Part of #2\nPart of https://github.com/other/project/issues/3\n"
        value = self.transport.issues(self.identity)
        self.assertEqual(value["partial_references"], ["https://github.com/fixture/project/issues/2",
                                                       "https://github.com/other/project/issues/3"])
        self.assertEqual(self.requests, ["repos/fixture/project/issues/2", "repos/other/project/issues/3"])
        self.assertFalse(value["no_issue"])
        with patch.object(self.transport, "api", return_value=dict(number=2,
                html_url="https://github.com/fixture/project/issues/2", state="open", pull_request={})):
            with self.assertRaisesRegex(factory.FactoryError, "accessible issue"):
                self.transport.issues(self.identity)

    def test_missing_conflicting_or_unsupported_declarations_refuse(self):
        for body in ("", "We have No issue here", "Refs #2", "Part of #2 extra", "No issue\nPart of #2"):
            with self.subTest(body=body):
                self.pr["body"] = body
                with self.assertRaises(factory.FactoryError):
                    self.transport.issues(self.identity)


if __name__ == "__main__":
    unittest.main()
