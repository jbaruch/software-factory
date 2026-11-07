"""Fixed GitHub/process transports and temporary Git only; no external services."""
import copy
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
        values = {"pulls/7": self.pr, "commits/" + self.merge_sha: dict(sha=self.merge_sha, parents=[dict(sha=p) for p in self.parents]),
            "git/ref/heads/main": dict(object=dict(sha=self.merge_sha)),
            "compare/" + self.merge_sha + "..." + self.merge_sha: self.compare,
            "actions/workflows/checks.yml": self.workflow, "actions/runs/81": self.run}
        if not endpoint.startswith(root) or endpoint[len(root):] not in values:
            raise AssertionError("Unexpected API call: " + endpoint)
        return copy.deepcopy(values[endpoint[len(root):]])

    def pages(self, endpoint):
        self.calls.append(endpoint)
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

    def call(self, *args, **kwargs):
        if args[:2] == ("block", "call") and args[-1] == "process":
            type(self).calls.append(args)
            return dict(child=dict(pid=123)) if self.running else dict(last_exit=dict(exit_code=0))
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
        self.review("qa", "qa")
        self.review("release", "release")

    def review(self, key, role, *, context=None, frozen=True, outcome="approved", writer=False, delivery=None, purpose="work"):
        command = factory.codex_command(self.path, task="deliver", assignment_id=key, codex=sys.executable,
            model="fixture-model", effort="high", cwd=str(self.repo), role=role, write=writer,
            release_evidence=self.frozen if frozen and delivery is None else None,
            delivery_evidence=release_evidence.delivery(factory.load_task(self.path), delivery) if delivery else None)
        with patch.object(factory, "Rex", FakeRex):
            record, assignment = factory.launch(self.path, task="deliver", assignment_id=key, role=role,
                goal="Judge the original complete evidence", cwd=str(self.repo), command=command, purpose=purpose,
                binary=sys.executable, server="unix:///fixture.sock")
        replacements = {"{repository}": str(self.repo), "{cwd}": str(self.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": self.head,
            "{requirements}": record["inputs"]["requirements"], "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        self.native.args = codex_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        events = fixtures.events(outcome)
        events[0]["thread_id"] = context or ("context-" + key)
        self.native.process = fixtures.FakeProcess(events)
        receipt = self.native.execute()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_codex(self.path, key, None, record, assignment)
        return self.record["assignments"][key]

    def integrate(self, **options):
        args = dict(assignment_id="merge", pr=self.transport.identity["url"], review="release", required=["qa"],
                    gh=Path("/installed/gh"), scripts=Path("/installed/acr__jbaruch__coding-policy__release"), transport=self.transport)
        args.update(options)
        with factory.execution_lock(self.tasks), factory.task_lock(self.tasks, "deliver"):
            return integration.integrate(self.path, **args)

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

    def test_pending_delivery_cannot_become_final_acceptance(self):
        self.transport.lost, self.transport.merge_lands = True, False
        self.integrate()
        with self.assertRaisesRegex(factory.FactoryError, "current original delivered"):
            release_evidence.delivery(factory.load_task(self.path), "merge")

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

    def test_terminal_receipt_survives_crash_before_task_collection(self):
        original = factory.save
        def fail_collection(path, value, **kwargs):
            if path == self.path and value["assignments"].get("merge", {}).get("result") is not None:
                raise OSError("Lost terminal collection write")
            return original(path, value, **kwargs)
        with patch.object(factory, "save", side_effect=fail_collection):
            with self.assertRaises(OSError):
                self.integrate()
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["state"], "launching")
        self.assertEqual(self.integrate()["state"], "delivered")
        self.assertEqual(factory.load_task(self.path)["assignments"]["merge"]["result"]["outcome"], "completed")
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
    def test_fixed_transport_has_one_conditional_merge_no_admin_or_shell(self):
        transport = object.__new__(release_evidence.GitHub)
        transport.gh = "/installed/gh"
        calls = []
        transport.run = lambda argv, **kwargs: calls.append((argv, kwargs)) or dict(merged=True)
        transport.merge(dict(repository="fixture/project", number=7, head="a" * 40))
        self.assertEqual(calls, [(["/installed/gh", "api", "--hostname", "github.com", "--method", "PUT",
            "repos/fixture/project/pulls/7/merge", "-f", "sha=" + "a" * 40, "-f", "merge_method=merge"], {})])

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

    def test_unvalidated_paths_and_urls_refuse(self):
        for url in ("https://evil.example/x/y/pull/1", "https://github.com/x/y/pull/1;echo", "https://github.com/x/y/pull/0"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.pr_url(url)
        for binary in ("gh", "/bin/sh", "/not-installed/gh"):
            with self.assertRaises(factory.FactoryError):
                release_evidence.GitHub(binary, Path("/not-installed/release"), "/tmp")


if __name__ == "__main__":
    unittest.main()
