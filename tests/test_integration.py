"""Fixed GitHub/process transports and temporary Git only; no external services."""
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
            base=dict(sha=base, ref="main", repo=dict(id=11, full_name="fixture/project")),
            mergeable=True, mergeable_state="clean", merge_commit_sha=None)
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
        self.workflow = dict(id=51, path=".github/workflows/checks.yml", state="active")
        self.protection: dict | None = dict(required_status_checks=dict(strict=True,
            contexts=["verify"], checks=[dict(context="verify", app_id=15368)]),
            enforce_admins=dict(enabled=True), allow_force_pushes=dict(enabled=False), allow_deletions=dict(enabled=False))
        self.pr_run = dict(id=61, workflow_id=51, path=self.workflow["path"], head_sha=head, event="pull_request",
            head_branch="candidate", run_number=7, run_attempt=1, repository=dict(id=11, full_name="fixture/project"),
            status="completed", conclusion="success")
        self.job = dict(id=71, name="verify", run_id=61, head_sha=head, status="completed", conclusion="success",
            check_run_url="https://api.github.com/repos/fixture/project/check-runs/91")
        self.check = dict(id=91, name="verify", head_sha=head, status="completed", conclusion="success",
            app=dict(id=15368, slug="github-actions"))
        self.server_base = base
        self.on_merge = lambda: None
        self.run: dict = dict(id=81, workflow_id=51, path=self.workflow["path"], head_sha=merge, event="push", head_branch="main",
            run_number=8, run_attempt=1, repository=dict(id=11, full_name="fixture/project"), status="completed", conclusion="success")
        self.runs = [self.run]
        self.watcher_result = "ready"
        self.merges, self.calls, self.watches = [], [], []
        self.lost = False
        self.merge_lands = True
        self.rejected = False
        self.unavailable = False
        self.parents = [base, head]
        self.compare = dict(status="identical", merge_base_commit=dict(sha=merge))
        self.on_watch = lambda: None
        self.on_run_watch = lambda: None

    def binding(self):
        return {"installed-watcher": "fixture-unchanged-policy", "gh": "fixture-cli"}

    def api(self, endpoint):
        self.calls.append(endpoint)
        if self.unavailable:
            raise factory.FactoryError("Observation unavailable")
        root = "repos/fixture/project/"
        if endpoint == root[:-1]:
            return dict(id=11, full_name="fixture/project", owner=dict(type="User"))
        values = {"pulls/7": self.pr, "commits/" + self.merge_sha: dict(sha=self.merge_sha, parents=[dict(sha=p) for p in self.parents]),
            "git/ref/heads/main": dict(object=dict(sha=self.merge_sha)),
            "compare/" + self.merge_sha + "..." + self.merge_sha: self.compare,
            "actions/workflows/checks.yml": self.workflow, "actions/runs/81": self.run,
            "branches/main/protection": self.protection, "actions/runs/61": self.pr_run, "check-runs/91": self.check}
        if not endpoint.startswith(root) or endpoint[len(root):] not in values:
            raise AssertionError("Unexpected API call: " + endpoint)
        return copy.deepcopy(values[endpoint[len(root):]])

    def pages(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == f"repos/fixture/project/actions/runs/61/attempts/{self.pr_run['run_attempt']}/jobs?per_page=100":
            return copy.deepcopy([dict(total_count=1, jobs=[self.job])])
        if endpoint != f"repos/fixture/project/actions/workflows/51/runs?head_sha={self.merge_sha}&event=push&per_page=100":
            raise AssertionError(endpoint)
        return copy.deepcopy([dict(total_count=len(self.runs), workflow_runs=self.runs)])

    def snapshot(self, identity, *, watch=False):
        assert all(identity[key] == value for key, value in self.identity.items() if key != "base")
        result = copy.deepcopy(self.snapshot_value)
        if watch:
            self.watches.append("canonical")
            result["watch"] = dict(result=self.watcher_result, attempts=1, elapsed_seconds=0)
            self.on_watch()
        return result

    def issues(self, identity, *, merged=False):
        return dict(pr=identity["number"], verdict="ok", state="MERGED" if merged else "OPEN",
                    closing=[], partial=[], partial_references=[], no_issue=True)

    def requirements(self, identity):
        return dict(headRefOid=identity["head"], baseRefOid=identity["base"], reviewDecision="APPROVED",
                    mergeable="MERGEABLE", mergeStateStatus="CLEAN")

    def merge(self, identity):
        self.merges.append(copy.deepcopy(identity))
        assert identity == self.identity
        self.on_merge()
        if (self.protection and self.protection["required_status_checks"]["strict"]
                and self.protection["enforce_admins"]["enabled"] and self.server_base != identity["base"]):
            return dict(merged=False, message="Strict required CI: branch is behind base")
        if self.rejected:
            return dict(merged=False, message="Rejected")
        if self.merge_lands:
            self.pr.update(state="closed", merged=True, merge_commit_sha=self.merge_sha)
        if self.lost:
            raise subprocess.TimeoutExpired("gh", 60)
        return dict(merged=True, sha=self.merge_sha)

    def watch_run(self, repository, identity):
        assert (repository, identity) == ("fixture/project", 81)
        self.watches.append("push-ci")
        self.on_run_watch()


class EvidenceRex(FakeRex):
    running = False
    integration_running = False

    def call(self, *args, **kwargs):
        if args[:2] == ("block", "call") and args[-1] == "process":
            type(self).calls.append(args)
            block_id = args[args.index("-b") + 1]
            integration_pane = any(window["label"].startswith("integration-") and
                any(block["block_id"] == block_id for layer in window["layers"] for block in layer["blocks"])
                for session in self.sessions for window in session["windows"])
            running = self.integration_running if integration_pane else self.running
            return dict(child=dict(pid=123)) if running else dict(last_exit=dict(exit_code=0))
        return super().call(*args, **kwargs)


class Integration(unittest.TestCase):
    def setUp(self):
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
        self.review("qa", "qa", frozen=False)
        self.review("release", "release")

    def review(self, key, role, *, context=None, frozen=True, outcome="approved", writer=False, delivery=None, purpose="work", failed=False, change=None, legacy=False):
        frozen_release = self.frozen if frozen and delivery is None else None
        frozen_delivery = release_evidence.capture_delivery(factory.load_task(self.path), delivery, self.transport) if delivery else None
        command = factory.codex_command(self.path, task="deliver", assignment_id=key, codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.repo), role=role, write=writer,
            commit_message="fix(fixture): Apply correction\n\nExercise sequential writer provenance." if change else None,
            release_evidence=frozen_release, delivery_evidence=frozen_delivery)
        if legacy:
            kind, payload = ("delivery", frozen_delivery) if delivery else ("release", frozen_release)
            index = command.index("--" + kind + "-evidence-digest")
            command[index:index + 2] = ["--" + kind + "-evidence", json.dumps(payload, sort_keys=True)]
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(self.path, task="deliver", assignment_id=key, role=role,
                goal="Judge the original complete evidence", cwd=str(self.repo), command=command, purpose=purpose,
                binary=sys.executable, server="unix:///fixture.sock",
                frozen_release=frozen_release, frozen_delivery=frozen_delivery)
        replacements = {"{repository}": str(self.repo), "{cwd}": str(self.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": self.head,
            "{requirements}": record["inputs"]["requirements"], "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        self.native.args = codex_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        events = fixtures.events(outcome)
        events[0]["thread_id"] = context or ("context-" + key)
        self.native.process = fixtures.FakeProcess(events[:2] if failed else events, timeout=failed, exit_code=-2 if failed else 0)
        spawn = self.native.spawn
        async def edit(*args, **kwargs):
            if change is not None:
                (self.repo / "feature.txt").write_text(change)
            return await spawn(*args, **kwargs)
        with patch.object(self.native, "spawn", side_effect=edit):
            receipt = self.native.execute()
        if failed:
            self.assertEqual(receipt["state"], "failed")
            factory.record_result(self.path, assignment_id=key, candidate=self.head,
                requirements=record["inputs"]["requirements"], outcome="blocked", artifacts=[str(self.native.args.receipt)])
            return factory.load_task(self.path)["assignments"][key]
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_codex(self.path, key, None, record, assignment)
        return self.record["assignments"][key]

    def integrate(self, **options):
        args = dict(assignment_id="merge", pr=self.transport.identity["url"], review="release", required=["qa"],
                    gh=Path("/installed/gh"), scripts=Path("/installed/acr__jbaruch__coding-policy__release"), transport=self.transport)
        args.update(options)
        result = integration.integrate(self.path, **args)
        if result["state"] == "launched":
            record = factory.load_task(self.path)
            intent = integration.intent_of(record["assignments"][args["assignment_id"]])
            EvidenceRex.integration_running = True
            try:
                return integration.execute(self.path, intent, transport=self.transport, start=True)
            finally:
                EvidenceRex.integration_running = False
        return result

    def launch_integration(self):
        return integration.integrate(self.path, assignment_id="merge", pr=self.transport.identity["url"],
            review="release", required=["qa"], gh=Path("/installed/gh"),
            scripts=Path("/installed/acr__jbaruch__coding-policy__release"), transport=self.transport)

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

    def test_real_pane_lifecycle_and_ordinary_schema_seven_readers(self):
        started = self.launch_integration()
        self.assertEqual(started["state"], "launched")
        record = factory.load_task(self.path)
        assignment = record["assignments"]["merge"]
        self.assertEqual(assignment["state"], "launched")
        self.assertIsNone(assignment["result"])
        self.assertEqual((started["window_id"], started["block_id"]),
                         (assignment["window_id"], assignment["block_id"]))
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 3)
        self.assertEqual(factory.task_view(record)["schema_version"], 7)
        rex = factory.connect(record)
        EvidenceRex.integration_running = True
        try:
            self.assertEqual(factory.status(rex, record, assignment)["state"], "running")
            self.assertEqual(self.launch_integration()["state"], "launched")
            command = assignment["request"]["command"]
            with patch.object(release_evidence, "GitHub", return_value=self.transport), redirect_stdout(StringIO()):
                self.assertEqual(integration.main(command[4:]), 0)
        finally:
            EvidenceRex.integration_running = False
        self.assertEqual(self.launch_integration()["state"], "delivered")
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 3)
        damaged = factory.load_task(self.path)
        del damaged["assignments"]["merge"]["block_id"]
        factory.save(self.path, damaged)
        with self.assertRaisesRegex(factory.FactoryError, "Missing launched pane identity"):
            factory.load_task(self.path)

    def test_lost_rex_launch_reply_reconnects_original_pane_without_second_merge(self):
        FakeRex.fail_after_create = True
        try:
            with self.assertRaisesRegex(factory.FactoryError, "Launch outcome uncertain"):
                self.launch_integration()
        finally:
            FakeRex.fail_after_create = False
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["state"], "launching")
        # Cold recovery never starts a second execution even if no receipt survived.
        self.assertEqual(self.launch_integration()["state"], "merge_outcome_unknown")
        self.assertEqual(len(FakeRex.sessions[0]["windows"]), 3)
        self.assertEqual(self.transport.merges, [])

    def test_pause_and_inputs_remain_usable_during_watcher_and_are_not_overwritten(self):
        def control():
            with factory.execution_lock(self.tasks):
                with factory.task_lock(self.tasks, "unrelated"):
                    pass
            with factory.task_lock(self.tasks, "deliver"):
                factory.hold_task(self.path, "Operator paused during wait", request_id="watch-pause")
                factory.set_inputs(self.path, candidate=self.head, requirements="changed-during-watch")
        self.transport.on_watch = control
        receipt = self.integrate()
        self.assertEqual(receipt["state"], "held")
        self.assertEqual(self.transport.merges, [])
        record = factory.load_task(self.path)
        self.assertEqual(record["hold"], "Operator paused during wait")
        self.assertEqual(record["inputs"]["requirements"], "changed-during-watch")
        self.assertEqual(record["control_events"][-1]["id"], "watch-pause")
        self.assertEqual(record["assignments"]["merge"]["result"]["inputs"], self.record["inputs"])

    def test_integration_mutex_leaves_other_ready_work_and_controls_available(self):
        self.launch_integration()
        record = factory.load_task(self.path)
        intent = integration.intent_of(record["assignments"]["merge"])
        with integration.integration_lock(self.path, intent):
            with self.assertRaisesRegex(factory.FactoryError, "Another integration"):
                integration.execute(self.path, intent, transport=self.transport)
            with factory.execution_lock(self.tasks), factory.task_lock(self.tasks, "deliver"):
                factory.hold_task(self.path, "Operator controls remain available")
        self.assertEqual(self.transport.merges, [])

    def test_pre_effect_reload_catches_control_after_network_checks(self):
        original = self.transport.api
        def changed(endpoint):
            result = original(endpoint)
            if self.transport.watches and endpoint.endswith("pulls/7"):
                with factory.task_lock(self.tasks, "deliver"):
                    factory.hold_task(self.path, "Late hold", request_id="late")
            return result
        self.transport.api = changed
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])
        self.assertEqual(factory.load_task(self.path)["hold"], "Late hold")

    def test_result_collection_preserves_controls_changed_during_postmerge_wait(self):
        def pause():
            with factory.task_lock(self.tasks, "deliver"):
                factory.hold_task(self.path, "Postmerge pause", request_id="postmerge")
        self.transport.on_run_watch = pause
        self.assertEqual(self.integrate()["state"], "delivered")
        record = factory.load_task(self.path)
        self.assertEqual(record["hold"], "Postmerge pause")
        self.assertEqual(record["control_events"][-1]["id"], "postmerge")

    def test_failed_contributor_keeps_original_failure_and_context(self):
        failed = self.review("failed-writer", "backend", writer=True, frozen=False, failed=True)
        command = failed["request"]["command"]
        paths = [Path(command[command.index(flag) + 1]) for flag in ("--receipt", "--events")]
        originals = [path.read_bytes() for path in paths]
        self.assertEqual(self.integrate()["state"], "delivered")
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
        self.assertEqual(self.integrate(review="release-new", required=["qa-new"])["state"], "delivered")
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
        for delivered in (False, True):
            if delivered:
                self.assertEqual(self.integrate()["state"], "delivered")
            entered, resume = threading.Event(), threading.Event()
            original = self.transport.watch_run if delivered else self.transport.snapshot
            def stall(*args, **kwargs):
                entered.set()
                if not resume.wait(10):
                    raise AssertionError("Test controller did not release capture")
                return original(*args, **kwargs)
            output, errors, result = StringIO(), StringIO(), []
            method = "watch_run" if delivered else "snapshot"
            with patch.object(release_evidence, "GitHub", return_value=self.transport), patch.object(self.transport, method, side_effect=stall):
                def capture():
                    with redirect_stdout(output), redirect_stderr(errors):
                        result.append(factory.main(self.native_cli("stalled-" + str(delivered).lower(), delivery="merge" if delivered else None)))
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
            record = factory.load_task(self.path)
            self.assertNotIn("stalled-" + str(delivered).lower(), record["assignments"])
            with factory.task_lock(self.tasks, "deliver"):
                factory.hold_task(self.path, None)
                factory.set_inputs(self.path, candidate=self.head, requirements=self.frozen["inputs"]["requirements"])

    def test_failed_writer_recovers_thread_from_supported_original_partial_events(self):
        failed = self.review("failed-writer", "backend", writer=True, frozen=False, failed=True)
        command = failed["request"]["command"]
        path = Path(command[command.index("--receipt") + 1])
        receipt = json.loads(path.read_text())
        receipt.update(schema_version=4, thread_id=None)
        factory.save(path, receipt)
        before = path.read_bytes()
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(factory.load_task(self.path)["assignments"]["failed-writer"], failed)

    def test_assignment_source_cannot_supply_installed_release_commands(self):
        self.transport.binding = lambda: {str(self.repo / "watch-pr-reviews.sh"): "writer-source"}
        with self.assertRaisesRegex(factory.FactoryError, "assignment checkout"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

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
            for name in ("watch-pr-reviews.sh", "poll-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
                (scripts / name).write_text("untrusted source")
            with patch.object(release_evidence.GitHub, "run", side_effect=AssertionError("Untrusted helper executed")):
                with self.assertRaisesRegex(factory.FactoryError, "outside source|assignment checkout"):
                    transport = release_evidence.GitHub(gh, scripts, self.repo)
                    release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], transport, cwd=str(cwd))
        self.assertEqual(self.transport.merges, [])

    def test_failed_writer_cannot_review_itself_and_unknown_identity_refuses(self):
        failed = self.review("failed-writer", "backend", context="context-qa", writer=True, frozen=False, failed=True)
        with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
            self.integrate()
        command = failed["request"]["command"]
        events = Path(command[command.index("--events") + 1])
        events.write_text("")
        with self.assertRaisesRegex(factory.FactoryError, "Unknown or ambiguous"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_prior_ordinary_qa_does_not_require_new_external_review_prose(self):
        qa = copy.deepcopy(factory.load_task(self.path)["assignments"]["qa"])
        self.assertNotIn("--release-evidence", qa["request"]["command"])
        self.transport.snapshot_value["review_bodies"][0]["body"] += " New advisory prose."
        self.frozen = release_evidence.capture(factory.load_task(self.path), self.transport.identity["url"], self.transport)
        self.review("new-release", "release")
        self.assertEqual(self.integrate(review="new-release")["state"], "delivered")
        self.assertEqual(factory.load_task(self.path)["assignments"]["qa"], qa)

    def test_unsupported_or_corrupt_receipts_never_resend(self):
        self.assertEqual(self.integrate()["state"], "delivered")
        path = self.tasks / "deliver.merge.integration.json"
        original = json.loads(path.read_text())
        for version in (None, 0, 2, True, "1"):
            value = {**original, "schema_version": version}
            factory.save(path, value)
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported or corrupt"):
                self.integrate()
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported or corrupt"):
                release_evidence.delivery(factory.load_task(self.path), "merge")
        for change in ({"observations": None}, {"state": "approved"}, {"delivery": None}):
            factory.save(path, {**original, **change})
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported or corrupt"):
                self.integrate()
        self.assertEqual(len(self.transport.merges), 1)

    def test_bad_frozen_packet_versions_and_original_worker_binding_refuse(self):
        record = factory.load_task(self.path)
        for version in (None, 0, 2, True, "1"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.validate({**self.frozen, "schema_version": version}, record)
            if version != 2:
                with self.assertRaisesRegex(factory.FactoryError, "Unsupported frozen"):
                    release_evidence.render_delivery({"schema_version": version})
            with self.assertRaisesRegex(factory.FactoryError, "Unsupported frozen"):
                release_evidence.render({**self.frozen, "schema_version": version})
        self.launch_integration()
        record = factory.load_task(self.path)
        intent = integration.intent_of(record["assignments"]["merge"])
        for change in ({"assignment": "qa"}, {"task": "another"}, {"schema_version": 2}):
            with self.assertRaises(factory.FactoryError):
                integration.execute(self.path, {**intent, **change}, transport=self.transport, start=True)
        Path(record["assignments"]["merge"]["brief"]).write_text("Changed frozen brief")
        with self.assertRaisesRegex(factory.FactoryError, "brief differs"):
            integration.execute(self.path, intent, transport=self.transport, start=True)
        self.assertEqual(self.transport.merges, [])

    def test_delivery_uses_original_intent_and_selected_reports_without_accepting_task(self):
        original = copy.deepcopy(self.record)
        allowance = factory.task_view(original)["allowances"]
        self.assertIn("Original historical full body", Path(original["assignments"]["release"]["brief"]).read_text())
        self.assertIn("Reply\\nAdvisory.", Path(original["assignments"]["release"]["brief"]).read_text())
        result = self.integrate()
        self.assertEqual(result["state"], "delivered")
        self.assertEqual(result["delivery"]["acceptance"], "pending_native_judgment")
        self.assertEqual(result["delivery"]["publication"], "inapplicable")
        saved = factory.load_task(self.path)
        self.assertEqual(saved["schema_version"], 7)
        self.assertEqual(saved["assignments"]["qa"], original["assignments"]["qa"])
        self.assertEqual(saved["assignments"]["release"], original["assignments"]["release"])
        self.assertEqual(factory.task_view(saved)["allowances"], allowance)
        self.assertEqual(len(self.transport.merges), 1)
        before = self.path.read_bytes()
        self.assertEqual(self.integrate(), result)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(len(self.transport.merges), 1)
        with self.assertRaises(factory.FactoryError):
            factory.record_result(self.path, assignment_id="merge", candidate=self.head,
                requirements=self.record["inputs"]["requirements"], outcome="approved", artifacts=["invented"])

    def test_persisted_command_is_executable_for_read_only_reconciliation(self):
        self.assertEqual(self.integrate()["state"], "delivered")
        command = factory.load_task(self.path)["assignments"]["merge"]["request"]["command"]
        with patch.object(release_evidence, "GitHub", return_value=self.transport), redirect_stdout(StringIO()) as output:
            self.assertEqual(integration.main(command[4:]), 0)
        self.assertEqual(json.loads(output.getvalue())["state"], "delivered")
        self.assertEqual(len(self.transport.merges), 1)

    def test_original_process_and_host_identity_are_revalidated(self):
        EvidenceRex.running = True
        with self.assertRaisesRegex(factory.FactoryError, "has not exited"):
            self.integrate()
        EvidenceRex.running = False
        FakeRex.host = "different-host"
        with self.assertRaisesRegex(factory.FactoryError, "host differs"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_stale_requirements_refuse_before_mutation(self):
        factory.set_inputs(self.path, candidate=self.head, requirements="changed")
        with self.assertRaises(factory.FactoryError):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_same_native_context_cannot_supply_two_approvals(self):
        self.review("duplicate", "security", context="context-release")
        with self.assertRaisesRegex(factory.FactoryError, "distinct original native contexts"):
            self.integrate(required=["duplicate"])
        self.assertEqual(self.transport.merges, [])

    def test_contributor_cannot_approve_own_work(self):
        self.review("writer", "backend", frozen=False, writer=True, context="context-qa", outcome="completed")
        with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_completed_is_not_an_approval(self):
        self.review("finished", "security", outcome="completed")
        with self.assertRaisesRegex(factory.FactoryError, "approved reports"):
            self.integrate(required=["finished"])

    def test_hold_preserves_attempts_and_blocks_mutation(self):
        factory.hold_task(self.path, "Operator hold")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "held"):
            self.integrate()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.transport.merges, [])

    def test_changed_review_body_or_base_requires_reassessment(self):
        for change in ("body", "base"):
            with self.subTest(change=change):
                if change == "body":
                    self.transport.snapshot_value["review_bodies"][0]["body"] += " edited"
                else:
                    self.transport.snapshot_value = copy.deepcopy(self.frozen["snapshot"])
                    self.transport.pr["base"]["sha"] = self.head
                with self.assertRaisesRegex(factory.FactoryError, "changed"):
                    self.integrate()
                self.assertEqual(self.transport.merges, [])

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
                        self.integrate()
                else:
                    target = Path(command[command.index("--" + mode) + 1])
                    data = target.read_bytes()
                    target.write_text("{}\n")
                    try:
                        with self.assertRaises(factory.FactoryError):
                            self.integrate()
                    finally:
                        target.write_bytes(data)
                factory.save(self.path, original)
        self.assertEqual(self.transport.merges, [])

    def test_quality_outage_never_becomes_approval(self):
        self.transport.watcher_result = "pending_at_budget"
        result = self.integrate()
        self.assertEqual(result["state"], "held")
        self.assertEqual(self.transport.merges, [])
        self.assertEqual(self.integrate(), result)

    def test_changes_during_watch_hold(self):
        self.transport.on_watch = lambda: self.transport.snapshot_value["ci"].update(status="failure")
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])

    def test_actual_merge_requirements_block(self):
        self.transport.pr["mergeable_state"] = "blocked"
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])

    def test_rejected_merge_is_not_delivery(self):
        self.transport.rejected = True
        result = self.integrate()
        self.assertEqual(result["state"], "rejected")
        self.assertEqual(self.integrate(), result)
        self.assertEqual(len(self.transport.merges), 1)

    def test_nonzero_native_http_rejection_finishes_task_result_and_replays_without_resend(self):
        native = object.__new__(release_evidence.GitHub)
        native.gh, native.repository = "/installed/gh", str(self.repo)
        native.binding, native.environment = lambda: {}, lambda: {}
        def rejected(identity):
            self.transport.merges.append(copy.deepcopy(identity))
            response = subprocess.CompletedProcess([], 1,
                'HTTP/2.0 409 Conflict\nContent-Type: application/json\n\n{"message":"Head changed"}', "HTTP 409")
            with patch.object(release_evidence.subprocess, "run", return_value=response):
                return native.merge(identity)
        with patch.object(self.transport, "merge", side_effect=rejected):
            result = self.integrate()
            self.assertEqual(result["state"], "rejected")
            self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["result"]["outcome"], "blocked")
            self.assertEqual(self.integrate(), result)
        self.assertEqual(len(self.transport.merges), 1)

    def test_lost_merge_reply_reconciles_confirmed_delivery(self):
        self.transport.lost = True
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(len(self.transport.merges), 1)

    def test_unknown_open_reconciliation_and_new_id_cannot_resend(self):
        self.transport.lost, self.transport.merge_lands = True, False
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.unavailable = True
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        with self.assertRaisesRegex(factory.FactoryError, "pending integration"):
            self.integrate(assignment_id="another")
        self.assertEqual(len(self.transport.merges), 1)
        self.transport.unavailable = False
        self.transport.pr.update(state="closed", merged=True, merge_commit_sha=self.transport.merge_sha)
        factory.hold_task(self.path, "Read-only recovery remains available")
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(len(self.transport.merges), 1)
        self.assertEqual(factory.load_task(self.path)["hold"], "Read-only recovery remains available")

    def test_restart_before_any_merge_response_is_read_only(self):
        def stop(identity):
            raise KeyboardInterrupt("Crash after durable intent")
        self.transport.merge = stop
        with self.assertRaises(KeyboardInterrupt):
            self.integrate()
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(self.transport.merges, [])

    def test_failed_missing_ambiguous_wrong_attempt_and_unbound_push_ci_are_pending(self):
        # Each refusal keeps the same original integration and reconciles read-only.
        self.transport.run["conclusion"] = "failure"
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.runs = []
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.runs = [self.transport.run, copy.deepcopy(self.transport.run)]
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.runs = [self.transport.run]
        self.transport.run["conclusion"] = "success"
        self.transport.on_run_watch = lambda: self.transport.run.update(run_attempt=self.transport.run["run_attempt"] + 1)
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.on_run_watch = lambda: None
        for key, wrong in (("event", "workflow_dispatch"), ("head_sha", self.head), ("head_branch", "other"), ("path", "other.yml")):
            before = self.transport.run[key]
            self.transport.run[key] = wrong
            self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
            self.transport.run[key] = before
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(len(self.transport.merges), 1)

    def test_final_acceptance_requires_original_delivered_input_and_native_judgment(self):
        self.assertEqual(self.integrate()["state"], "delivered")
        self.review("accept", "release", delivery="merge")
        original = self.path.read_bytes()
        record = factory.load_task(self.path)
        verdict = integration.accepted(record, "merge", "accept", self.transport)
        self.assertEqual(verdict["outcome"], "approved")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(len(self.transport.merges), 1)
        with self.assertRaises(factory.FactoryError):
            integration.accepted(record, "merge", "release", self.transport)
        self.transport.run["run_attempt"] += 1
        with self.assertRaisesRegex(factory.FactoryError, "changed"):
            integration.accepted(record, "merge", "accept", self.transport)
        delivered = record["assignments"]["merge"]
        receipt_path = Path(delivered["result"]["artifacts"][0])
        original_receipt = receipt_path.read_bytes()
        original_acceptance = copy.deepcopy(record["assignments"]["accept"])
        self.review("accept-rerun", "release", delivery="merge")
        record = factory.load_task(self.path)
        frozen = release_evidence.from_delivery_assignment(record, record["assignments"]["accept-rerun"])
        self.assertEqual(frozen["schema_version"], 2)
        self.assertEqual(frozen["receipt"]["delivery"]["ci"]["run_attempt"], 1)
        self.assertEqual(frozen["observation"]["ci"]["run_attempt"], 2)
        self.assertEqual(integration.accepted(record, "merge", "accept-rerun", self.transport)["outcome"], "approved")
        self.assertEqual(record["assignments"]["merge"], delivered)
        self.assertEqual(record["assignments"]["accept"], original_acceptance)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.assertEqual(len(self.transport.merges), 1)

    def test_pending_delivery_cannot_become_final_acceptance(self):
        self.transport.lost, self.transport.merge_lands = True, False
        self.integrate()
        with self.assertRaisesRegex(factory.FactoryError, "current original delivered"):
            release_evidence.delivery(factory.load_task(self.path), "merge")

    def test_legacy_delivery_brief_stays_immutable_and_new_packets_validate(self):
        self.assertEqual(self.integrate()["state"], "delivered")
        record = factory.load_task(self.path)
        legacy = release_evidence.delivery(record, "merge")
        with patch.object(release_evidence, "capture_delivery", return_value=legacy):
            assignment = self.review("legacy-acceptance", "release", delivery="merge")
        record = factory.load_task(self.path)
        self.assertEqual(release_evidence.from_delivery_assignment(record, assignment), legacy)
        self.assertEqual(integration.accepted(record, "merge", "legacy-acceptance", self.transport)["outcome"], "approved")
        fresh = release_evidence.capture_delivery(record, "merge", self.transport)
        for damaged in ({**fresh, "schema_version": 3}, {key: value for key, value in fresh.items() if key != "observation"},
                        {**fresh, "observation": {**fresh["observation"], "publication": "invented"}},
                        {**fresh, "observation": {**fresh["observation"], "head": self.base}}):
            with self.assertRaises(factory.FactoryError):
                release_evidence.validate_delivery(damaged, record)

    def test_unreadable_or_changing_server_guard_refuses(self):
        required = self.transport.snapshot_value["ci"]["actions"]["runs"][0]
        api = self.transport.api
        def unavailable(endpoint):
            if endpoint.endswith("/protection"):
                raise factory.FactoryError("HTTP 404 or missing administration read permission")
            return api(endpoint)
        with patch.object(self.transport, "api", side_effect=unavailable):
            with self.assertRaisesRegex(factory.FactoryError, "configure/verify.*read access"):
                release_evidence.server_guard(self.transport, self.transport.identity, required)
        reads = []
        def changed(endpoint):
            result: dict = api(endpoint)
            if endpoint.endswith("/protection"):
                assert isinstance(result, dict) and isinstance(result["enforce_admins"], dict)
                reads.append(endpoint)
                if len(reads) > 1:
                    result["enforce_admins"]["enabled"] = False
            return result
        with patch.object(self.transport, "api", side_effect=changed):
            with self.assertRaises(factory.FactoryError):
                release_evidence.server_guard(self.transport, self.transport.identity, required)
        self.assertEqual(self.transport.merges, [])

    def test_missing_required_ci_and_actual_review_requirement_hold(self):
        self.transport.snapshot_value["ci"]["actions"]["runs"] = []
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])
        self.transport.snapshot_value = copy.deepcopy(self.frozen["snapshot"])
        original = self.transport.requirements
        self.transport.requirements = lambda identity: {**original(identity), "reviewDecision": "REVIEW_REQUIRED"}
        self.assertEqual(self.integrate(assignment_id="review-held")["state"], "held")
        self.assertEqual(self.transport.merges, [])

    def test_changed_policy_or_inline_bodies_during_watch_cannot_merge(self):
        original = self.transport.binding()
        self.transport.on_watch = lambda: setattr(self.transport, "binding", lambda: {**original, "gh": "changed"})
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])
        self.transport.binding = lambda: original
        self.transport.on_watch = lambda: self.transport.snapshot_value["inline_comment_bodies"][0].update(body="changed reply")
        self.assertEqual(self.integrate(assignment_id="inline-changed")["state"], "held")
        self.assertEqual(self.transport.merges, [])

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
                self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_origin_identity_cannot_be_substituted(self):
        factory.git_run(str(self.repo), "remote", "set-url", "origin", "https://github.com/different/project.git")
        with self.assertRaisesRegex(factory.FactoryError, "origin"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])

    def test_original_consumed_correction_survives_delivery(self):
        self.review("prior-correction", "qa", purpose="correction")
        before = factory.task_view(factory.load_task(self.path))["allowances"]
        self.assertEqual(before["correction"]["used"], 1)
        self.assertEqual(self.integrate()["state"], "delivered")
        self.integrate()
        self.assertEqual(factory.task_view(factory.load_task(self.path))["allowances"], before)

    def test_required_issue_checks_cannot_be_bypassed(self):
        original = self.transport.issues
        self.transport.issues = lambda identity, **kwargs: {**original(identity, **kwargs), "verdict": "unlinked"}
        with self.assertRaisesRegex(factory.FactoryError, "issue declaration"):
            self.integrate()
        self.transport.issues = lambda identity, **kwargs: {**original(identity, **kwargs),
            "verdict": "still_open" if kwargs.get("merged") else "ok"}
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(len(self.transport.merges), 1)

    def test_malformed_output_holds_before_effect_and_reconciles_after_submission(self):
        failure = release_evidence.output_failure("fixture-helper")
        original = self.transport.snapshot
        self.transport.snapshot = lambda *args, **kwargs: (_ for _ in ()).throw(failure)
        with self.assertRaisesRegex(factory.FactoryError, "reconcile the same integration ID"):
            self.integrate()
        self.assertEqual(self.transport.merges, [])
        self.transport.snapshot = original
        original_issues = self.transport.issues
        def issues(identity, *, merged=False):
            if merged:
                raise failure
            return original_issues(identity, merged=merged)
        self.transport.issues = issues
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.issues = original_issues
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(len(self.transport.merges), 1)

    def test_malformed_watcher_output_is_a_terminal_pre_effect_hold(self):
        original = self.transport.snapshot
        def snapshot(identity, *, watch=False):
            if watch:
                raise release_evidence.output_failure("fixture-helper")
            return original(identity, watch=watch)
        self.transport.snapshot = snapshot
        receipt = self.integrate()
        self.assertEqual(receipt["state"], "held")
        self.assertIn("pre-effect hold requires reassessment", receipt["reason"])
        self.assertEqual(self.transport.merges, [])
        self.transport.snapshot = original
        self.assertEqual(self.integrate()["state"], "held")
        self.assertEqual(self.transport.merges, [])

    def test_terminal_receipt_survives_crash_before_task_collection(self):
        original = factory.save
        def fail_collection(path, value, **kwargs):
            if path == self.path and value["assignments"].get("merge", {}).get("result") is not None:
                raise OSError("Lost terminal collection write")
            return original(path, value, **kwargs)
        with patch.object(factory, "save", side_effect=fail_collection):
            with self.assertRaises(OSError):
                self.integrate()
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["state"], "launched")
        receipt_path = self.tasks / "deliver.merge.integration.json"
        original_receipt = receipt_path.read_bytes()
        self.transport.run.update(run_attempt=2, conclusion="failure")
        with self.assertRaisesRegex(factory.FactoryError, "terminal success"):
            self.integrate()
        self.assertIsNone(factory.load_task(self.path)["assignments"]["merge"]["result"])
        self.transport.run["conclusion"] = "success"
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["result"]["outcome"], "completed")
        self.assertEqual(len(self.transport.merges), 1)
        self.assertEqual(receipt_path.read_bytes(), original_receipt)
        self.review("recovered-acceptance", "release", delivery="merge")
        self.assertEqual(integration.accepted(factory.load_task(self.path), "merge", "recovered-acceptance", self.transport)["outcome"], "approved")

    def test_absent_loose_bypassable_and_untrusted_server_guards_hold(self):
        protected = copy.deepcopy(self.transport.protection)
        assert isinstance(protected, dict)
        unsafe = [None, {}, {**protected, "required_status_checks": dict(strict=False, checks=[dict(context="verify", app_id=15368)])},
            {**protected, "enforce_admins": dict(enabled=False)},
            {**protected, "allow_force_pushes": dict(enabled=True)},
            {**protected, "required_status_checks": dict(strict=True, checks=[dict(context="verify", app_id=-1)])},
            {**protected, "required_status_checks": dict(strict=True, contexts=["verify"])},
            {**protected, "required_pull_request_reviews": dict(bypass_pull_request_allowances=dict(users=[dict(login="owner")]))}]
        for index, guard in enumerate(unsafe):
            with self.subTest(guard=guard):
                self.transport.protection = guard
                receipt = self.integrate(assignment_id="unsafe-" + str(index))
                self.assertEqual(receipt["state"], "held")
                self.assertIn("repository owner configure/verify", receipt["reason"])
                self.assertEqual(self.transport.merges, [])
        self.transport.protection = protected
        for key, wrong in (("app", dict(id=7, slug="untrusted")), ("head_sha", self.base), ("conclusion", "skipped")):
            saved = self.transport.check[key]
            self.transport.check[key] = wrong
            self.assertEqual(self.integrate(assignment_id="untrusted-" + key.replace("_", "-"))["state"], "held")
            self.transport.check[key] = saved
        self.transport.job["name"] = "trigger"
        self.assertEqual(self.integrate(assignment_id="wrong-job")["state"], "held")
        self.assertEqual(self.transport.merges, [])

    def test_strict_server_rejects_base_advance_between_last_read_and_merge(self):
        self.transport.on_merge = lambda: setattr(self.transport, "server_base", "f" * 40)
        receipt = self.integrate()
        self.assertEqual(receipt["state"], "rejected")
        self.assertIn("server_guard", receipt["gates"])
        self.assertFalse(self.transport.pr["merged"])
        self.assertEqual(self.transport.pr["state"], "open")
        self.assertEqual(len(self.transport.merges), 1)
        self.assertEqual(self.integrate()["state"], "rejected")
        self.assertEqual(len(self.transport.merges), 1)

    def test_final_acceptance_option_cannot_start_an_external_effect(self):
        with patch.object(integration, "integrate", side_effect=AssertionError("Must not execute")), redirect_stdout(StringIO()):
            from contextlib import redirect_stderr
            with redirect_stderr(StringIO()):
                rc = factory.main(["--state-dir", str(self.tasks), "integrate", "--task", "deliver", "--id", "new",
                    "--pr", self.transport.identity["url"], "--review", "release", "--after", "qa", "--acceptance", "accept",
                    "--gh", "/installed/gh", "--release-scripts", "/installed/acr__jbaruch__coding-policy__release"])
        self.assertEqual(rc, 1)

    def test_malformed_native_merge_commit_remains_pending_without_resend(self):
        original = self.transport.api
        self.transport.api = lambda endpoint: [] if "/commits/" in endpoint else original(endpoint)
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(len(self.transport.merges), 1)

    def test_missing_or_changed_terminal_evidence_cannot_replace_original_result(self):
        self.assertEqual(self.integrate()["state"], "delivered")
        record = factory.load_task(self.path)
        result = copy.deepcopy(record["assignments"]["merge"]["result"])
        path = Path(result["artifacts"][0])
        content = path.read_text()
        path.unlink()
        with self.assertRaisesRegex(factory.FactoryError, "Restore the original terminal"):
            self.integrate()
        path.write_text(content)
        receipt = json.loads(content)
        receipt["delivery"]["merge_commit"] = "a" * 40
        factory.save(path, receipt)
        with self.assertRaisesRegex(factory.FactoryError, "result content changed"):
            self.integrate()
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["result"], result)
        self.assertEqual(len(self.transport.merges), 1)

    def test_source_parent_or_target_ancestry_mismatch_cannot_deliver(self):
        self.transport.parents = [self.base, self.base]
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.transport.parents = [self.base, self.head]
        self.transport.compare["status"] = "diverged"
        self.assertEqual(self.integrate()["state"], "merge_outcome_unknown")
        self.assertEqual(len(self.transport.merges), 1)


class CommandAuthority(unittest.TestCase):
    def installed_transport(self, root):
        repo = root / "repo"
        repo.mkdir()
        gh = root / "gh"
        gh.write_text("#!/bin/sh\nprintf '%s\\n' pinned-gh\n")
        gh.chmod(0o700)
        scripts = root / "acr__jbaruch__coding-policy__release"
        scripts.mkdir()
        for name in ("poll-pr-reviews.sh", "watch-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
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
            self.assertEqual(len(transport.binding()), 7)

    def test_spawn_and_timeout_diagnostics_require_same_id_reconciliation(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = self.installed_transport(Path(folder).resolve())
            for failure in (FileNotFoundError("removed helper"), subprocess.TimeoutExpired("gh", 60)):
                for action in (lambda: transport.api("repos/fixture/project"),
                               lambda: transport.watch_run("fixture/project", 81)):
                    with self.subTest(failure=type(failure).__name__), patch.object(release_evidence.subprocess, "run", side_effect=failure):
                        with self.assertRaises(factory.FactoryError) as caught:
                            action()
                        self.assertIn("restore the installed command", str(caught.exception))
                        self.assertIn("reconcile the same integration ID", str(caught.exception))
                        self.assertIn("never resend", str(caught.exception))
            Path(transport.poll).unlink()
            with self.assertRaisesRegex(factory.FactoryError, "restore.*reconcile the same integration ID"):
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
                    self.assertIn("reconcile the same integration ID", message)
                    self.assertIn("pre-effect hold requires reassessment", message)
                    self.assertIn("never resend a merge", message)
                    self.assertNotIn(output if output else "secret", message)
            invalid = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")
            for action in (lambda: transport.api("repos/fixture/project"),
                           lambda: transport.watch_run("fixture/project", 81)):
                with patch.object(release_evidence.subprocess, "run", side_effect=invalid):
                    with self.assertRaisesRegex(factory.FactoryError, "output contract.*reconcile the same integration ID"):
                        action()

    def test_fixed_transport_has_one_conditional_merge_no_admin_or_shell(self):
        transport = object.__new__(release_evidence.GitHub)
        transport.gh = "/installed/gh"
        calls = []
        transport.run = lambda argv, **kwargs: calls.append((argv, kwargs)) or dict(merged=True)
        transport.merge(dict(repository="fixture/project", number=7, head="a" * 40))
        self.assertEqual(calls, [(["/installed/gh", "api", "--hostname", "github.com", "--include", "--method", "PUT",
            "repos/fixture/project/pulls/7/merge", "-f", "sha=" + "a" * 40, "-f", "merge_method=merge"],
            dict(accepted=(0, 1), merge_response=True))])

    def test_nonzero_http_merge_rejection_is_definitive_but_transport_failure_is_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            transport = self.installed_transport(Path(folder).resolve())
            for status in (405, 409):
                output = f'HTTP/2.0 {status} Rejected\nContent-Type: application/json\n\n{{"message":"Head/protection rejected"}}'
                result = subprocess.CompletedProcess([], 1, output, "native HTTP rejection")
                with self.subTest(status=status), patch.object(release_evidence.subprocess, "run", return_value=result):
                    self.assertEqual(transport.merge(dict(repository="fixture/project", number=7, head="a" * 40)),
                        dict(merged=False, http_status=status, message="Head/protection rejected"))
            for output in ('', 'HTTP/2.0 500 Failed\n\n{"message":"unknown"}',
                           'HTTP/2.0 403 Denied\n\n{"message":"permissions"}',
                           'HTTP/2.0 409 Rejected\n\nmalformed'):
                result = subprocess.CompletedProcess([], 1, output, "fixture")
                with self.subTest(output=output), patch.object(release_evidence.subprocess, "run", return_value=result):
                    with self.assertRaises(factory.FactoryError):
                        transport.merge(dict(repository="fixture/project", number=7, head="a" * 40))

    def test_canonical_watch_is_unmodified_and_cannot_use_an_environment_fake(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo = root / "repo"
            repo.mkdir()
            scripts = root / "acr__jbaruch__coding-policy__release"
            scripts.mkdir()
            gh = root / "gh"
            gh.write_text("fixture binary")
            gh.chmod(0o700)
            for name in ("watch-pr-reviews.sh", "poll-pr-reviews.sh", "check-closing-issues.py", "review-lanes.sh"):
                (scripts / name).write_text("fixture installed helper")
            transport = release_evidence.GitHub(gh, scripts, repo)
            before = transport.binding()
            self.assertIn("factory-issue-checker:v1", before)
            (scripts / "check-closing-issues.py").unlink()
            self.assertEqual(transport.binding(), before)
            process = subprocess.CompletedProcess([], 0, json.dumps(dict(watch=dict(result="ready"))), "")
            overrides = dict(WATCH_PR_REVIEWS_POLL_CMD="/untrusted/fake", WATCH_PR_REVIEWS_BUDGET_SEC="1",
                GH_HOST="evil.example", GH_REPO="evil/project", GH_CONFIG_DIR=str(root / "auth"), BASH_ENV="/untrusted/startup")
            with patch.dict(os.environ, overrides), patch.object(release_evidence.subprocess, "run", return_value=process) as run:
                transport.snapshot(dict(repository="fixture/project", number=7), watch=True)
            argv = run.call_args.args[0]
            self.assertEqual(argv, [transport.bash, str(scripts / "watch-pr-reviews.sh"), "fixture", "project", "7"])
            self.assertIsNone(run.call_args.kwargs["timeout"])
            environment = run.call_args.kwargs["env"]
            self.assertNotIn("WATCH_PR_REVIEWS_POLL_CMD", environment)
            self.assertNotIn("WATCH_PR_REVIEWS_BUDGET_SEC", environment)
            self.assertNotIn("BASH_ENV", environment)
            self.assertNotIn("GH_REPO", environment)
            self.assertEqual(environment["GH_HOST"], "github.com")
            self.assertEqual(environment["GH_CONFIG_DIR"], str(root / "auth"))
            self.assertEqual(transport.binding(), before)

    def test_managed_ignored_helpers_are_allowed_but_source_and_replacements_refuse(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            repo = root / "repo"
            subprocess.run(["git", "init", "--quiet", str(repo)], check=True)
            factory.git_run(str(repo), "config", "core.excludesFile", "/dev/null")
            scripts = repo / ".codex" / "skills" / "acr__jbaruch__coding-policy__release"
            scripts.mkdir(parents=True)
            (repo / ".git" / "info" / "exclude").write_text(".codex/\n")
            gh = root / "gh"
            gh.write_text("fixture binary")
            gh.chmod(0o700)
            for name in ("watch-pr-reviews.sh", "poll-pr-reviews.sh", "review-lanes.sh", "check-closing-issues.py"):
                (scripts / name).write_text("installed helper")
                (scripts / name).chmod(0o600)
            transport = release_evidence.GitHub(gh, scripts, repo)
            self.assertIn(str(scripts / "watch-pr-reviews.sh"), transport.binding())
            target = scripts / "watch-pr-reviews.sh"
            target.chmod(0o666)
            with self.assertRaisesRegex(factory.FactoryError, "owner-controlled"):
                transport.binding()
            target.chmod(0o600)
            target.unlink()
            target.symlink_to(scripts / "poll-pr-reviews.sh")
            with self.assertRaises(factory.FactoryError):
                transport.binding()
            target.unlink()
            target.write_text("installed helper")
            factory.git_run(str(repo), "add", "-f", ".codex")
            with self.assertRaisesRegex(factory.FactoryError, "protected .codex"):
                transport.binding()

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
        self.pr["state"] = "MERGED"
        with self.assertRaises(factory.FactoryError):
            self.transport.issues(self.identity, merged=True)
        issue["state"] = "CLOSED"
        self.assertEqual(self.transport.issues(self.identity, merged=True)["state"], "MERGED")

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
