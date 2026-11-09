"""Controller contracts use fake Rex and native processes, never providers."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
import copy
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
from typing import Callable
import unittest
from unittest.mock import patch

import codex_worker
import factory
import native_codex
import ready_runner as runner
from test_codex_worker import FakeProcess
from test_rex_launcher import FakeRex


class PlanRex(FakeRex):
    processes = {}

    def call(self, *args, **kwargs):
        if args[:2] == ("block", "call"):
            type(self).calls.append(args)
            key = (args[args.index("-s") + 1], args[args.index("-b") + 1])
            state = self.processes.get(key, "running")
            if state in {"exited", "failed"}:
                return dict(last_exit=dict(exit_code=0 if state == "exited" else 1))
            return dict(child={"pid": 42} if state == "running" else None)
        return super().call(*args, **kwargs)


class ReadyRunner(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.directory, self.repo = self.root / "state", self.root / "repo"
        subprocess.run(["git", "init", "--quiet", "--initial-branch=main", str(self.repo)], check=True)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid")):
            subprocess.run(["git", "-C", str(self.repo), "config", key, value], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "--allow-empty", "-m", "Fixture"],
            check=True, env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
                             "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        PlanRex.sessions, PlanRex.calls, PlanRex.processes = [], [], {}
        PlanRex.fail_after_create, PlanRex.host = False, "test-host"
        self.patcher = patch.object(factory, "Rex", PlanRex)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.now, self.probes = 1000.0, 0
        self.capacity = dict(schema_version=1, observed_at=1000.0, binary=str(Path(sys.executable).resolve()),
            route="codex-native", account=dict(type="chatgpt", plan="fixture"), ordinaryUsageAllowed=True,
            pools=[dict(limit_id="codex", primary=dict(remaining_percent=80), secondary=None,
                        rate_limit_reached_type=None, spend_control_reached=False)],
            models=[dict(model="fixture-model", effort_ids=["high"])])
        self.admit("task")

    def admit(self, task, *, corrections=1):
        with factory.task_lock(self.directory, task) as path:
            factory.admit(path, task=task, goal="Exercise explicit controller transitions",
                repository=str(self.repo), acceptance=["fixture/acceptance.md"], corrections=corrections)
            factory.set_inputs(path, candidate=self.candidate, requirements="requirements-1")

    def job(self, identity="first", *, task="task", role=None, after=(), **changes) -> dict:
        return {**dict(task=task, id=identity, role=role or ("backend" if changes.get("write") else "qa"), goal="Inspect the selected fixture candidate",
            cwd=str(self.repo), candidate=self.candidate, requirements="requirements-1",
            model="fixture-model", effort="high", seconds=30, purpose="work", write=False, commit_message=None,
            after=list(after)), **changes}

    def plan(self, jobs, *, workers=2, reserve=1, tasks=4) -> dict:
        return dict(schema_version=1, limits=dict(workers=workers, tasks=tasks,
            reserve_workers=reserve, minimum_remaining_percent=15), jobs=jobs)

    async def probe(self, binary):
        self.probes += 1
        return copy.deepcopy(self.capacity)

    def controller(self, plan, **options):
        return runner.Runner(self.directory, plan, codex=sys.executable, rex=sys.executable,
                             server="unix:///test.sock", probe=self.probe, clock=lambda: self.now, **options)

    def tick(self, controller):
        return runner.run(controller, seconds=30, once=True)

    def finish(self, task, identity, outcome="approved", *, edit: bool | Callable[[], None] = False):
        record = factory.load_task(self.directory / (task + ".json"))
        assignment = record["assignments"][identity]
        request = assignment["request"]
        replacements = {"{repository}": str(self.repo), "{cwd}": request["cwd"],
            "{brief}": assignment["brief"], "{brief_sha256}": request["instructions_sha256"],
            "{candidate}": request["inputs"]["candidate"],
            "{requirements}": request["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        argv = [replacements.get(part, part) for part in request["command"]]
        args = codex_worker.parser().parse_args(argv[4:])
        events = [dict(type="thread.started", thread_id="fixture-thread"), dict(type="turn.started"),
            dict(type="item.completed", item=dict(type="agent_message", text=json.dumps(
                dict(outcome=outcome, artifacts=["fixture/evidence.txt"])))),
            dict(type="turn.completed", usage=dict(input_tokens=10, output_tokens=5))]
        async def spawn(*command, **options):
            if callable(edit):
                edit()
            elif edit:
                (self.repo / "delivery.txt").write_text("Useful fixture delivery\n")
            return FakeProcess(events)
        before = Path.cwd()
        try:
            os.chdir(self.repo)
            with redirect_stdout(StringIO()):
                receipt = asyncio.run(codex_worker.execute(args, spawn=spawn,
                    clock=lambda: 1000.0, timer=lambda: 10.0, git_scope=lambda *args, **kwargs: nullcontext(), scan=lambda *args, **kwargs: None))
            self.assertEqual(receipt["state"], "completed")
        finally:
            os.chdir(before)
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "exited"

    def states(self, result):
        return {(item["task"], item["id"]): item["state"] for item in result["jobs"]}

    def launches(self):
        """Rex windows ever created; finished tabs are tidied, so live windows undercount."""
        return sum(call[0] == "new" or call[:2] == ("window", "new") for call in PlanRex.calls)

    def keep_tabs(self):
        """Keep finished tabs open so a test can vary their observed process afterwards."""
        patcher = patch.object(factory, "tidy", return_value=dict(closed=[], running=[], session_closed=False))
        patcher.start()
        self.addCleanup(patcher.stop)

    def completed_writer(self, *, collect=True):
        job = self.job("implement", write=True, commit_message="feat(fixture): Deliver candidate")
        controller = self.controller(self.plan([job]))
        self.assertEqual(self.tick(controller)["metrics"]["launched"], 1)
        with patch.dict(os.environ, GIT_AUTHOR_DATE="2000-01-02T00:00:00Z",
                        GIT_COMMITTER_DATE="2000-01-02T00:00:00Z"):
            self.finish("task", "implement", outcome="completed", edit=True)
        if collect:
            self.tick(controller)
        return factory.load_task(self.directory / "task.json")

    def verification_plan(self, **changes):
        return self.plan([self.job("verify", **{"after": ["implement"], **changes})])

    def handoff(self, plan=None):
        return self.tick(self.controller(plan or self.verification_plan(), from_writer="implement"))

    def test_from_writer_dispatches_exact_commit_and_preserves_original_artifacts(self):
        before = self.completed_writer()
        target = before["assignments"]["implement"]["result"]["candidate"]
        plan = self.plan([self.job(role=role, identity=role, after=["implement"])
                          for role in ("qa", "code-review", "test-automation")], workers=3, reserve=0)
        supplied = copy.deepcopy(plan)
        plan_file = self.root / "verification-plan.json"
        plan_file.write_text(json.dumps(plan))
        originals = {path: path.read_bytes() for path in self.directory.iterdir() if path.suffix in {".md", ".jsonl"}}
        receipt = self.directory / "task.implement.codex.json"
        originals[receipt], originals[plan_file] = receipt.read_bytes(), plan_file.read_bytes()
        result = self.handoff(plan)
        self.assertEqual(result["metrics"]["launched"], 3)
        selected = factory.load_task(self.directory / "task.json")
        self.assertNotEqual(target, self.candidate)
        self.assertEqual(selected["inputs"], dict(candidate=target, requirements="requirements-1"))
        self.assertEqual(selected["assignments"]["implement"], before["assignments"]["implement"])
        self.assertEqual(selected["admission"], before["admission"])
        self.assertEqual(plan, supplied)
        for identity in ("qa", "code-review", "test-automation"):
            request = selected["assignments"][identity]["request"]
            self.assertEqual(request["inputs"], selected["inputs"])
            self.assertEqual(request["dependencies"], ["implement"])
            self.assertNotIn("--write", request["command"])
            self.finish("task", identity)
        self.assertEqual(self.handoff(plan)["result"], "complete")
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_from_writer_restart_observes_running_jobs_without_duplicate_or_reset(self):
        self.completed_writer()
        plan = self.verification_plan(purpose="correction")
        self.assertEqual(self.handoff(plan)["metrics"]["launched"], 1)
        path = self.directory / "task.json"
        saved, sessions = path.read_bytes(), copy.deepcopy(PlanRex.sessions)
        controller = self.controller(plan, from_writer="implement")
        for _ in range(2):
            result = self.tick(controller)
            self.assertEqual(result["jobs"][0]["state"], "running")
            self.assertEqual(result["metrics"]["launched"], 0)
            self.assertEqual(path.read_bytes(), saved)
            self.assertEqual(PlanRex.sessions, sessions)
        self.finish("task", "verify")
        self.assertEqual(self.handoff(plan)["result"], "complete")
        saved = path.read_bytes()
        self.assertEqual(self.handoff(plan)["result"], "complete")
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(factory.task_view(factory.load_task(path))["allowances"]["correction"],
                         dict(limit=1, used=1, remaining=0))

    def test_from_writer_restart_after_selection_before_dispatch(self):
        before = self.completed_writer()
        controller = self.controller(self.verification_plan(), from_writer="implement")
        with patch.object(controller, "tick", side_effect=RuntimeError("interrupted after selection")):
            with self.assertRaisesRegex(RuntimeError, "interrupted"):
                self.tick(controller)
        selected = factory.load_task(self.directory / "task.json")
        self.assertEqual(selected["inputs"]["candidate"], before["assignments"]["implement"]["result"]["candidate"])
        self.assertEqual(selected["assignments"], before["assignments"])
        self.assertEqual(self.handoff()["metrics"]["launched"], 1)
        self.assertEqual(self.handoff()["metrics"]["launched"], 0)

    def test_from_writer_cli_replays_original_plan_and_evidence(self):
        self.completed_writer()
        plan_file = self.root / "verification.json"
        plan_file.write_text(json.dumps(self.verification_plan()))
        self.handoff()
        self.finish("task", "verify")
        self.assertEqual(self.handoff()["result"], "complete")
        before, sessions = (self.directory / "task.json").read_bytes(), copy.deepcopy(PlanRex.sessions)
        output = StringIO()
        with redirect_stdout(output):
            code = runner.main(["--state-dir", str(self.directory), "--plan", str(plan_file),
                "--from-writer", "implement", "--codex", sys.executable, "--rex", sys.executable,
                "--server", "unix:///test.sock", "--once"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["result"], "complete")
        self.assertEqual(json.loads(plan_file.read_text()), self.verification_plan())
        self.assertEqual((self.directory / "task.json").read_bytes(), before)
        self.assertEqual(PlanRex.sessions, sessions)

    def test_from_writer_selection_requires_both_existing_locks(self):
        self.completed_writer()
        path = self.directory / "task.json"
        before, calls = path.read_bytes(), list(PlanRex.calls)
        for lock in (factory.execution_lock(self.directory), factory.task_lock(self.directory, "task")):
            with lock, self.assertRaisesRegex(factory.FactoryError, "Another"):
                self.handoff()
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(PlanRex.calls, calls)

    def test_from_writer_stale_original_pair_and_changed_selection_refuse(self):
        before = self.completed_writer()
        target = before["assignments"]["implement"]["result"]["candidate"]
        for changes in (dict(candidate=target), dict(requirements="requirements-2")):
            with self.subTest(plan=changes), self.assertRaisesRegex(factory.FactoryError, "frozen original pair"):
                self.handoff(self.verification_plan(**changes))
        # A real third commit is neither the frozen input nor the produced candidate.
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Third party", "--allow-empty"], check=True)
        third = factory.git_commit(str(self.repo), "HEAD")
        subprocess.run(["git", "-C", str(self.repo), "checkout", "--quiet", "--detach", target], check=True)
        for candidate, requirements in ((third, "requirements-1"), (self.candidate, "requirements-2"),
                                        (target, "requirements-2")):
            with factory.task_lock(self.directory, "task") as path:
                factory.set_inputs(path, candidate=candidate, requirements=requirements)
            saved = path.read_bytes()
            with self.subTest(selected=candidate, requirements=requirements):
                with self.assertRaisesRegex(factory.FactoryError, "Selected task inputs changed"):
                    self.handoff()
                self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.launches(), 1)

    def test_from_writer_hold_refuses_selection_and_preserves_controls(self):
        self.completed_writer()
        with factory.task_lock(self.directory, "task") as path:
            factory.hold_task(path, "Operator hold", request_id="hold", clock=lambda: 1000.0)
        before = path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "paused"):
            self.handoff()
        self.assertEqual(path.read_bytes(), before)

    def test_from_writer_running_verification_remains_observable_during_hold(self):
        self.completed_writer()
        self.handoff()
        with factory.task_lock(self.directory, "task") as path:
            factory.hold_task(path, "Operator hold", clock=lambda: 1000.0)
        self.assertEqual(self.handoff()["jobs"][0]["state"], "running")
        self.finish("task", "verify")
        self.assertEqual(self.handoff()["result"], "complete")
        self.assertEqual(factory.load_task(path)["hold"], "Operator hold")

    def test_from_writer_other_process_must_be_observed_terminal_even_with_result(self):
        self.keep_tabs()
        # Launch before the writer moves HEAD; retain a completed result with a live process.
        other = self.controller(self.plan([self.job("other")]))
        self.tick(other)
        self.finish("task", "other")
        self.tick(other)
        self.completed_writer()
        path = self.directory / "task.json"
        before = path.read_bytes()
        record = factory.load_task(path)
        key = (record["session_id"], record["assignments"]["other"]["block_id"])
        for state in ("running", "unknown"):
            PlanRex.processes[key] = state
            with self.subTest(state=state), self.assertRaisesRegex(factory.FactoryError, "active or unknown"):
                self.handoff()
            self.assertEqual(path.read_bytes(), before)
        PlanRex.processes[key] = "failed"
        self.assertEqual(self.handoff()["metrics"]["launched"], 1)

    def test_from_writer_requires_collected_source_before_observation(self):
        self.completed_writer(collect=False)
        before = (self.directory / "task.json").read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "collected, completed"):
            self.handoff()
        self.assertEqual((self.directory / "task.json").read_bytes(), before)

    def test_from_writer_manually_recorded_completion_without_native_evidence_refuses(self):
        self.tick(self.controller(self.plan([self.job("implement", write=True, commit_message="Fixture")])))
        with factory.task_lock(self.directory, "task") as path:
            record = factory.record_result(path, assignment_id="implement", candidate=self.candidate,
                requirements="requirements-1", outcome="completed", artifacts=["manual-report"])
        PlanRex.processes[(record["session_id"], record["assignments"]["implement"]["block_id"])] = "exited"
        before = path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "receipt is missing"):
            self.handoff()
        self.assertEqual(path.read_bytes(), before)

    def test_from_writer_failed_or_unknown_source_process_refuses_saved_success(self):
        self.keep_tabs()
        record = self.completed_writer()
        key = (record["session_id"], record["assignments"]["implement"]["block_id"])
        for state in ("failed", "running", "unknown"):
            PlanRex.processes[key] = state
            with self.subTest(state=state), self.assertRaisesRegex(factory.FactoryError, "not exited successfully"):
                self.handoff()
        self.assertEqual(factory.load_task(self.directory / "task.json")["inputs"], record["inputs"])

    def test_from_writer_non_writer_and_non_native_source_refuse(self):
        self.tick(self.controller(self.plan([self.job("implement")])))
        self.finish("task", "implement", outcome="completed")
        self.tick(self.controller(self.plan([self.job("implement")])))
        with self.assertRaisesRegex(factory.FactoryError, "collected, completed native writer"):
            self.handoff()
        path = self.directory / "task.json"
        record = factory.load_task(path)
        record["assignments"]["implement"]["request"]["command"] = [sys.executable, "--write"]
        factory.save(path, record)
        with self.assertRaisesRegex(factory.FactoryError, "collected, completed native writer"):
            self.handoff()

    def test_from_writer_no_explicit_commit_refuses(self):
        plan = self.plan([self.job("implement", write=True)])
        self.tick(self.controller(plan))
        self.finish("task", "implement", outcome="completed")
        self.tick(self.controller(plan))
        with self.assertRaisesRegex(factory.FactoryError, "explicit validated factory commit"):
            self.handoff()

    def test_from_writer_tampered_result_receipt_events_or_binding_refuse(self):
        record = self.completed_writer()
        path = self.directory / "task.json"
        receipt_path = self.directory / "task.implement.codex.json"
        events_path = self.directory / "task.implement.codex.jsonl"
        original_receipt, original_events = receipt_path.read_bytes(), events_path.read_bytes()
        variants = [dict(outcome="findings"), dict(candidate=self.candidate), dict(artifacts=["manual-report"]),
                    dict(inputs=dict(candidate=record["inputs"]["candidate"], requirements="requirements-2"))]
        for change in variants:
            altered = copy.deepcopy(record)
            altered["assignments"]["implement"]["result"].update(change)
            factory.save(path, altered)
            before = path.read_bytes()
            with self.subTest(result=change), self.assertRaises(factory.FactoryError):
                self.handoff()
            self.assertEqual(path.read_bytes(), before)
        factory.save(path, record)
        receipt = json.loads(original_receipt)
        for change in (dict(report=dict(outcome="approved", artifacts=["forged"])),
                       dict(commit={**receipt["commit"], "staged_tree": self.candidate}),
                       dict(worktree_git_dir=str(self.root / "foreign")),
                       dict(repository_identity={**receipt["repository_identity"], "repository_id": "f" * 32})):
            receipt_path.write_text(json.dumps({**receipt, **change}))
            with self.subTest(receipt=change), self.assertRaises(factory.FactoryError):
                self.handoff()
        receipt_path.write_bytes(original_receipt)
        events_path.write_bytes(original_events.replace(b"fixture/evidence.txt", b"forged/evidence.txt"))
        with self.assertRaisesRegex(factory.FactoryError, "original events"):
            self.handoff()
        self.assertEqual(factory.load_task(path), record)
        self.assertEqual(self.launches(), 1)

    def test_from_writer_invalid_verification_plan_refuses_without_effects(self):
        self.completed_writer()
        valid = self.verification_plan()
        changes = [lambda d: d.update(jobs=[]),
            lambda d: d["jobs"].append(self.job("elsewhere", task="other", after=["implement"])),
            lambda d: d["jobs"][0].update(role="backend"),
            lambda d: d["jobs"][0].update(role="qa", write=True, commit_message="Write"),
            lambda d: d["jobs"][0].update(id="implement", after=[]),
            lambda d: d["jobs"][0].update(after=[]),
            lambda d: d["jobs"].append(self.job("other", requirements="requirements-2", after=["implement"]))]
        before, calls = (self.directory / "task.json").read_bytes(), list(PlanRex.calls)
        for change in changes:
            plan = copy.deepcopy(valid)
            change(plan)
            with self.subTest(plan=plan), self.assertRaises(factory.FactoryError):
                self.handoff(plan)
        self.assertEqual((self.directory / "task.json").read_bytes(), before)
        self.assertEqual(PlanRex.calls, calls)

    def test_from_writer_keeps_capacity_checkout_dependency_and_attempt_gates(self):
        record = self.completed_writer()
        target = record["assignments"]["implement"]["result"]["candidate"]
        self.capacity["ordinaryUsageAllowed"] = False
        result = self.handoff()
        self.assertEqual(result["jobs"][0]["state"], "waiting")
        self.assertEqual(result["metrics"]["launched"], 0)
        self.capacity["ordinaryUsageAllowed"] = True
        (self.repo / "untracked.txt").write_text("Dirty checkout\n")
        self.assertEqual(self.handoff()["jobs"][0]["state"], "blocked")
        (self.repo / "untracked.txt").unlink()
        self.assertEqual(self.handoff(self.verification_plan(after=["implement", "absent"]))["jobs"][0]["state"], "blocked")
        with factory.task_lock(self.directory, "task") as path:
            # Consume the existing cumulative allowance through an ordinary factory assignment.
            command = factory.codex_command(path, task="task", assignment_id="prior", codex=sys.executable,
                model="fixture-model", effort="high", cwd=str(self.repo), seconds=30, role="qa")
            factory.launch(path, task="task", assignment_id="prior", role="qa", goal="Prior correction",
                cwd=str(self.repo), command=command, binary=sys.executable, server="unix:///test.sock", purpose="correction")
        self.finish("task", "prior")
        for _ in range(2):
            result = self.handoff(self.verification_plan(purpose="correction"))
            self.assertEqual(result["jobs"][0]["state"], "blocked")
            self.assertIn("exhausted", result["jobs"][0]["reason"])
            self.assertEqual(result["metrics"]["launched"], 0)
        self.assertEqual(factory.load_task(path)["inputs"]["candidate"], target)

    def test_readonly_verifier_cannot_request_writer_mode(self):
        before = (self.directory / "task.json").read_bytes()
        plan = self.plan([self.job(role="code-review", write=True, commit_message="Unauthorized edit")])
        result = self.tick(self.controller(plan))
        self.assertEqual(result["result"], "needs_action")
        self.assertEqual(result["jobs"][0]["state"], "blocked")
        self.assertIn("specialty does not permit writer", result["jobs"][0]["reason"])
        self.assertEqual((self.directory / "task.json").read_bytes(), before)
        self.assertEqual(result["metrics"]["launched"], 0)

    def test_writer_commit_is_collected_and_changed_message_cannot_replay_it(self):
        plan = self.plan([self.job(role="backend", write=True,
                                  commit_message="feat(fixture): Deliver an explicit change")])
        self.assertEqual(self.tick(self.controller(plan))["metrics"]["launched"], 1)
        self.finish("task", "first", outcome="completed", edit=True)
        candidate = factory.git_commit(str(self.repo), "HEAD")
        self.assertNotEqual(candidate, self.candidate)
        completed = self.tick(self.controller(plan))
        self.assertEqual(completed["result"], "needs_action")
        self.assertEqual(self.states(completed)[("task", "first")], "stale")
        saved = factory.load_task(self.directory / "task.json")["assignments"]["first"]
        self.assertEqual(saved["result"]["outcome"], "completed")
        self.assertEqual(saved["result"]["candidate"], candidate)
        self.assertIn("--commit-message", saved["request"]["command"])
        changed = copy.deepcopy(plan)
        changed["jobs"][0]["commit_message"] = "feat(fixture): Different intent"
        conflict = self.tick(self.controller(changed))
        self.assertEqual(self.states(conflict)[("task", "first")], "conflict")
        self.assertEqual(factory.git_commit(str(self.repo), "HEAD"), candidate)
        self.assertEqual(conflict["metrics"]["launched"], 0)

    def test_writer_to_verifier_handoff_never_runs_clean_filters_on_the_host(self):
        source = self.repo / "sample.audit"
        source.write_text("committed\n")
        (self.repo / ".gitattributes").write_text("*.audit filter=audit\n")
        helper = self.repo / "filter.py"
        helper.write_text("import sys\nsys.stdout.write(sys.stdin.read())\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "--all"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "commit.gpgsign=false",
                        "-c", "core.hooksPath=/dev/null", "commit", "-qm", "Filter fixture"], check=True)
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        with factory.task_lock(self.directory, "task") as path:
            factory.set_inputs(path, candidate=self.candidate, requirements="requirements-1")
        plan = self.plan([self.job("implement", role="backend", write=True,
                                  commit_message="feat(fixture): Update filter helper")])
        self.assertEqual(self.tick(self.controller(plan))["metrics"]["launched"], 1)
        effect = self.root / "callback-effect"
        def edit():
            helper.write_text("import sys\nfrom pathlib import Path\n"
                f"Path({str(effect)!r}).write_text('called')\n"
                "sys.stdout.write(sys.stdin.read())\n")
            source.write_text("updated\n")
        self.finish("task", "implement", outcome="completed", edit=edit)
        self.assertFalse(effect.exists())
        subprocess.run(["git", "-C", str(self.repo), "config", "filter.audit.clean",
                        shlex.quote(sys.executable) + " -B filter.py"], check=True)
        self.assertEqual(self.tick(self.controller(plan))["result"], "needs_action")
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        with factory.task_lock(self.directory, "task") as path:
            factory.set_inputs(path, candidate=self.candidate, requirements="requirements-1")
        os.utime(source, ns=(946684800000000000, 946684800000000000))
        verification = self.controller(self.plan([self.job("qa", after=["implement"])]))
        self.assertEqual(self.tick(verification)["metrics"]["launched"], 1)
        self.finish("task", "qa")
        self.assertEqual(self.tick(verification)["result"], "complete")
        self.assertFalse(effect.exists())
        self.assertEqual(factory.git_run(str(self.repo), "status", "--porcelain").stdout, "")
        self.assertTrue(effect.exists())

    def test_collected_dependency_starts_next_assignment_without_a_foreman_turn(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        first = self.tick(controller)
        self.assertEqual(self.states(first), {("task", "first"): "running", ("task", "second"): "waiting"})
        self.finish("task", "first")
        second = self.tick(controller)
        self.assertEqual(self.states(second), {("task", "first"): "approved", ("task", "second"): "running"})
        self.finish("task", "second")
        final = self.tick(controller)
        self.assertEqual(final["result"], "complete")
        self.assertEqual(final["metrics"]["launched"], 2)
        self.assertEqual(final["metrics"]["collected"], 2)
        self.assertEqual(self.probes, 1)

    def test_restart_reuses_existing_effect_and_collects_it(self):
        plan = self.plan([self.job(), self.job("second", after=["first"])])
        self.tick(self.controller(plan))
        before = copy.deepcopy(PlanRex.sessions)
        self.tick(self.controller(plan))
        self.assertEqual(PlanRex.sessions, before)
        self.finish("task", "first")
        result = self.tick(self.controller(plan))
        self.assertEqual(self.states(result)[("task", "second")], "running")
        self.assertEqual(self.launches(), 2)

    def test_uncertain_launch_reconciles_same_pane_and_reserves_a_slot(self):
        PlanRex.fail_after_create = True
        plan = self.plan([self.job()], workers=1, reserve=0)
        first = self.tick(self.controller(plan))
        self.assertEqual(first["active_workers"], 1)
        self.assertEqual(first["jobs"][0]["state"], "unknown")
        PlanRex.fail_after_create = False
        second = self.tick(self.controller(plan))
        self.assertEqual(second["jobs"][0]["state"], "running")
        self.assertEqual(len(PlanRex.sessions), 1)
        self.assertEqual(len(PlanRex.sessions[0]["windows"]), 1)

    def test_missing_uncertain_pane_is_never_relaunched(self):
        PlanRex.fail_after_create = True
        plan = self.plan([self.job()], workers=1, reserve=0)
        self.tick(self.controller(plan))
        PlanRex.fail_after_create, PlanRex.sessions = False, []
        result = self.tick(self.controller(plan))
        self.assertEqual(result["jobs"][0]["state"], "unknown")
        self.assertEqual(result["active_workers"], 1)
        self.assertEqual(PlanRex.sessions, [])

    def test_pause_allows_collection_but_holds_new_launches_until_resume(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        self.tick(controller)
        with factory.task_lock(self.directory, "task") as path:
            factory.hold_task(path, "Operator hold", clock=lambda: 1000.0)
        self.finish("task", "first")
        held = self.tick(controller)
        self.assertEqual(self.states(held)[("task", "first")], "approved")
        self.assertEqual(self.states(held)[("task", "second")], "waiting")
        self.assertIn("paused", held["jobs"][1]["reason"])
        with factory.task_lock(self.directory, "task") as path:
            factory.hold_task(path, None, clock=lambda: 1001.0)
        self.assertEqual(self.states(self.tick(controller))[("task", "second")], "running")

    def test_fairness_interleaves_tasks_and_verification_keeps_its_reserved_slot(self):
        self.admit("other")
        self.admit("verify")
        jobs = [self.job(role="backend"), self.job("second", role="backend"),
                self.job(task="other", role="backend"), self.job(task="verify")]
        reserved = self.tick(self.controller(self.plan(jobs)))
        self.assertEqual(reserved["active_workers"], 2)
        self.assertEqual(self.states(reserved)[("verify", "first")], "running")
        self.assertEqual(self.states(reserved)[("other", "first")], "waiting")
        self.assertIn("reserve", reserved["jobs"][2]["reason"])

    def test_equal_priority_tasks_share_initial_launch_slots(self):
        self.admit("other")
        jobs = [self.job(), self.job("second"), self.job(task="other")]
        result = self.tick(self.controller(self.plan(jobs, reserve=0)))
        self.assertEqual(self.states(result), {("task", "first"): "running",
            ("task", "second"): "waiting", ("other", "first"): "running"})

    def test_task_wip_counts_tasks_instead_of_assignment_slots(self):
        self.admit("other")
        jobs = [self.job(), self.job("second"), self.job(task="other")]
        result = self.tick(self.controller(self.plan(jobs, workers=4, tasks=1)))
        self.assertEqual(self.states(result), {("task", "first"): "running",
            ("task", "second"): "running", ("other", "first"): "waiting"})
        self.assertIn("task limit", result["jobs"][2]["reason"])

    def test_native_writer_prevents_concurrent_verification_in_its_checkout(self):
        docs = self.document_subdirectory()
        jobs = [self.job(role="backend", write=True, cwd=str(docs)), self.job("second")]
        result = self.tick(self.controller(self.plan(jobs)))
        self.assertEqual(result["jobs"][0]["state"], "running")
        self.assertEqual(result["jobs"][1]["state"], "waiting")
        self.assertIn("checkout", result["jobs"][1]["reason"])

    def test_active_native_reader_prevents_a_writer_in_the_same_checkout(self):
        docs = self.document_subdirectory()
        jobs = [self.job(), self.job("second", role="backend", write=True, cwd=str(docs))]
        result = self.tick(self.controller(self.plan(jobs)))
        self.assertEqual(result["jobs"][0]["state"], "running")
        self.assertEqual(result["jobs"][1]["state"], "waiting")
        self.assertIn("checkout", result["jobs"][1]["reason"])

    def document_subdirectory(self):
        docs = self.repo / "docs"
        docs.mkdir()
        (docs / "contract.md").write_text("Fixture contract\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "docs"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "-c", "commit.gpgsign=false",
            "-c", "core.hooksPath=/dev/null", "commit", "--quiet", "-m", "Fixture docs"], check=True,
            env={**os.environ, "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z"})
        self.candidate = factory.git_commit(str(self.repo), "HEAD")
        with factory.task_lock(self.directory, "task") as path:
            factory.set_inputs(path, candidate=self.candidate, requirements="requirements-1")
        return docs

    def test_unmanaged_unknown_process_counts_and_unknown_adapter_is_a_writer(self):
        self.admit("other")
        with factory.task_lock(self.directory, "other") as path:
            record, assignment = factory.launch(path, task="other", assignment_id="manual", role="qa",
                goal="Explicit foreign adapter fixture", cwd=str(self.repo), command=[sys.executable, "{instructions}"],
                purpose="work", binary=sys.executable, server="unix:///test.sock")
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "unknown"
        limited = self.tick(self.controller(self.plan([self.job()], workers=1, reserve=0)))
        self.assertEqual(limited["active_workers"], 1)
        self.assertIn("occupied", limited["jobs"][0]["reason"])
        checkout = self.tick(self.controller(self.plan([self.job()], workers=2, reserve=0)))
        self.assertIn("checkout", checkout["jobs"][0]["reason"])
        self.assertEqual(self.probes, 0)

    def test_failed_receipt_does_not_reserve_an_already_exited_process(self):
        self.admit("other")
        controller = self.controller(self.plan([self.job(), self.job(task="other")], workers=1, reserve=0))
        self.tick(controller)
        record = factory.load_task(self.directory / "task.json")
        PlanRex.processes[(record["session_id"], record["assignments"]["first"]["block_id"])] = "exited"
        result = self.tick(controller)
        self.assertEqual(self.states(result), {("task", "first"): "blocked", ("other", "first"): "running"})
        self.assertEqual(result["active_workers"], 1)

    def restart_rex(self):
        PlanRex.instance = "restarted-instance"
        self.addCleanup(setattr, PlanRex, "instance", "test-instance")

    def test_rex_restart_collects_completed_receipt_and_advances(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        self.tick(controller)
        self.finish("task", "first")
        record = factory.load_task(self.directory / "task.json")
        # The restored pane is a fresh login shell with no exit status.
        PlanRex.processes[(record["session_id"], record["assignments"]["first"]["block_id"])] = "running"
        self.restart_rex()
        result = self.tick(controller)
        self.assertEqual(self.states(result), {("task", "first"): "approved", ("task", "second"): "running"})
        self.assertEqual(result["metrics"]["collected"], 1)
        second = factory.load_task(self.directory / "task.json")["assignments"]["second"]
        self.assertEqual(second["rex_instance"], "restarted-instance")

    def test_rex_restart_without_receipt_frees_the_slot_and_blocks_dependents(self):
        self.admit("other")
        plan = self.plan([self.job(), self.job("second", after=["first"]), self.job(task="other")], workers=1, reserve=0)
        controller = self.controller(plan)
        first = self.tick(controller)
        self.assertEqual(first["active_workers"], 1)
        self.restart_rex()
        result = self.tick(controller)
        states = self.states(result)
        self.assertEqual(states[("task", "first")], "blocked")
        self.assertEqual(states[("task", "second")], "blocked")
        self.assertEqual(states[("other", "first")], "running")
        self.assertEqual(result["active_workers"], 1)

    def test_collected_tabs_close_and_the_idle_session_ends(self):
        controller = self.controller(self.plan([self.job()]))
        self.tick(controller)
        self.finish("task", "first")
        result = self.tick(controller)
        self.assertEqual(self.states(result), {("task", "first"): "approved"})
        self.assertEqual(result["metrics"]["tabs_closed"], 1)
        self.assertEqual(result["tidy_issues"], [])
        self.assertEqual(PlanRex.sessions, [])
        record = factory.load_task(self.directory / "task.json")
        self.assertIsNone(record["session_id"])
        closed = record["assignments"]["first"]["closed"]
        self.assertEqual((closed["state"], Path(closed["capture"]).read_text()), ("exited", "output of block-1\n"))
        again = self.tick(controller)
        self.assertEqual(again["result"], "complete")
        self.assertEqual(again["metrics"]["tabs_closed"], 1)

    def test_tidy_failure_is_reported_without_blocking_work(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        self.tick(controller)
        self.finish("task", "first")
        with patch.object(factory, "tidy", side_effect=factory.FactoryError("Fixture tidy failure")):
            result = self.tick(controller)
        self.assertEqual(self.states(result), {("task", "first"): "approved", ("task", "second"): "running"})
        self.assertEqual(result["tidy_issues"], [dict(task="task", reason="Fixture tidy failure")])
        self.assertEqual(result["issues"], [])

    def test_abandoned_launch_frees_its_slot_and_blocks_dependents(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])], workers=1, reserve=0))
        original = PlanRex.call
        def reject(rex, *args, **kwargs):
            if args[0] == "new":
                raise factory.FactoryError("Error: unknown flag: --focus")
            return original(rex, *args, **kwargs)
        with patch.object(PlanRex, "call", reject):
            first = self.tick(controller)
        self.assertEqual(first["active_workers"], 1)
        self.assertIn("factory.py abandon", self.tick(controller)["jobs"][0]["reason"])
        with factory.task_lock(self.directory, "task") as path:
            record = factory.load_task(path)
            factory.abandon_launch(path, factory.connect(record), assignment_id="first", reason="Rex rejected an unknown flag")
        result = self.tick(controller)
        self.assertEqual(self.states(result), {("task", "first"): "abandoned", ("task", "second"): "blocked"})
        self.assertEqual(result["active_workers"], 0)
        self.assertEqual(result["result"], "needs_action")

    def claude_job(self, identity, **changes):
        return self.job(identity, **{"harness": "claude", "model": "claude-sonnet-5-5", "effort": "high", **changes})

    def claude_controller(self, plan, **options):
        return runner.Runner(self.directory, plan, codex=sys.executable, claude=sys.executable, rex=sys.executable,
                             server="unix:///test.sock", probe=self.probe, clock=lambda: self.now, **options)

    def finish_claude(self, task, identity, outcome="approved"):
        import claude_worker
        import native_claude
        record = factory.load_task(self.directory / (task + ".json"))
        assignment = record["assignments"][identity]
        request = assignment["request"]
        replacements = {"{repository}": str(self.repo), "{cwd}": request["cwd"],
            "{brief}": assignment["brief"], "{brief_sha256}": request["instructions_sha256"],
            "{candidate}": request["inputs"]["candidate"], "{requirements}": request["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = claude_worker.parser().parse_args([replacements.get(part, part) for part in request["command"]][4:])
        report = dict(outcome=outcome, artifacts=["fixture/evidence.txt"])
        session, model = args.native_session, args.model
        events = [dict(type="system", subtype="init", cwd=str(args.cwd), session_id=session, model=model,
                       tools=list(native_claude.READ_TOOLS), mcp_servers=[], permissionMode="dontAsk", slash_commands=[],
                       apiKeySource="none", claude_code_version="2.1.319", additional_directories=[]),
                  dict(type="assistant", session_id=session, parent_tool_use_id=None,
                       message=dict(model=model, content=[dict(type="tool_use", id="output-1", name="StructuredOutput", input=report)])),
                  dict(type="user", session_id=session, parent_tool_use_id=None,
                       message=dict(content=[dict(type="tool_result", tool_use_id="output-1", content="Structured output provided successfully")])),
                  dict(type="result", session_id=session, subtype="success", is_error=False, terminal_reason="completed",
                       api_error_status=None, safety_stops=0, permission_denials=[], subagent_stats=dict(spawned=0), num_turns=1,
                       result=json.dumps(report), structured_output=report, usage=dict(input_tokens=2, output_tokens=20),
                       modelUsage={model: dict(provider="firstParty", canonicalModel=model, costBasis="list", costUSD=0.01,
                                               inputTokens=2, outputTokens=20)})]
        async def spawn(*command, **options):
            return FakeProcess(events)
        def probe(binary, cwd):
            return dict(schema_version=1, kind="claude-native-auth", harness="claude", binary=str(binary), cwd=str(cwd),
                cli_version="2.1.319 (Claude Code)", started_at=1000, observed_at=1000, probe_seconds=0,
                auth=dict(loggedIn=True, authMethod="claude.ai", apiProvider="firstParty"))
        environment = {key: value for key, value in os.environ.items() if not key.startswith(native_claude.OVERRIDE_PREFIXES)}
        before = Path.cwd()
        try:
            os.chdir(self.repo)
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(StringIO()):
                receipt = asyncio.run(claude_worker.execute(args, spawn=spawn, probe=probe, clock=lambda: 1000.0, timer=lambda: 10.0))
        finally:
            os.chdir(before)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "exited"

    def test_claude_jobs_launch_collect_and_advance_without_codex_capacity(self):
        controller = self.claude_controller(self.plan([self.claude_job("first", role="qa"),
                                                       self.claude_job("second", role="qa", after=["first"])]))
        first = self.tick(controller)
        self.assertEqual(self.states(first), {("task", "first"): "running", ("task", "second"): "waiting"})
        command = factory.load_task(self.directory / "task.json")["assignments"]["first"]["request"]["command"]
        self.assertEqual(Path(command[3]).name, "claude_worker.py")
        self.finish_claude("task", "first")
        second = self.tick(controller)
        self.assertEqual(self.states(second), {("task", "first"): "approved", ("task", "second"): "running"})
        self.assertEqual(second["metrics"]["collected"], 1)
        self.assertEqual(self.probes, 0)

    def test_claude_jobs_need_their_binary_effort_and_writer_commit_message(self):
        with self.assertRaisesRegex(factory.FactoryError, "--claude executable"):
            self.controller(self.plan([self.claude_job("first", role="qa")]))
        for changes in (dict(effort="turbo"), dict(write=True, role="backend"), dict(harness="grok")):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                self.claude_controller(self.plan([self.claude_job("first", **{"role": "qa", **changes})]))

    def test_findings_never_advance_a_dependency_or_repeat_the_worker(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        self.tick(controller)
        self.finish("task", "first", "findings")
        result = self.tick(controller)
        self.assertEqual(self.states(result)[("task", "first")], "findings")
        self.assertEqual(self.states(result)[("task", "second")], "blocked")
        self.assertEqual(result["metrics"]["launched"], 1)

    def test_new_phase_can_depend_on_an_existing_assignment_without_rebinding_it(self):
        self.tick(self.controller(self.plan([self.job()])))
        self.finish("task", "first")
        phase = self.plan([self.job("second", after=["first"])])
        result = self.tick(self.controller(phase))
        self.assertEqual(result["jobs"][0]["state"], "running")
        record = factory.load_task(self.directory / "task.json")
        self.assertEqual(record["assignments"]["first"]["request"]["inputs"]["candidate"], self.candidate)
        missing = self.plan([self.job("third", after=["absent"])])
        self.assertEqual(self.tick(self.controller(missing))["jobs"][0]["state"], "blocked")

    def test_changed_inputs_or_plan_never_rebind_an_existing_assignment(self):
        plan = self.plan([self.job()])
        self.tick(self.controller(plan))
        changed = copy.deepcopy(plan)
        changed["jobs"][0]["goal"] = "Different scope"
        conflict = self.tick(self.controller(changed))
        self.assertEqual(conflict["jobs"][0]["state"], "conflict")
        with factory.task_lock(self.directory, "task") as path:
            factory.set_inputs(path, candidate=self.candidate, requirements="requirements-2")
        stale = self.tick(self.controller(plan))
        self.assertEqual(stale["jobs"][0]["state"], "stale")
        self.assertEqual(len(PlanRex.sessions[0]["windows"]), 1)

    def test_quota_unknowns_blocks_staleness_and_model_mismatch_wait_without_launch(self):
        good = copy.deepcopy(self.capacity)
        mutations = [lambda d: d.update(observed_at=900), lambda d: d.update(observed_at=1001),
            lambda d: d.update(ordinaryUsageAllowed=False), lambda d: d.update(pools=None),
            lambda d: d["pools"][0].update(spend_control_reached=None),
            lambda d: d["pools"][0].update(spend_control_reached=True),
            lambda d: d["pools"][0].update(rate_limit_reached_type="workspace_member_usage_limit_reached"),
            lambda d: d["pools"][0]["primary"].update(remaining_percent=15),
            lambda d: d["pools"][0]["primary"].update(remaining_percent=None),
            lambda d: d["pools"][0].update(secondary=dict(remaining_percent=None)),
            lambda d: d.update(models=[])]
        for mutate in mutations:
            self.capacity = copy.deepcopy(good)
            mutate(self.capacity)
            with self.subTest(mutation=mutate):
                result = self.tick(self.controller(self.plan([self.job()])))
                self.assertEqual(result["jobs"][0]["state"], "waiting")
                self.assertEqual(result["metrics"]["launched"], 0)
        self.assertEqual(PlanRex.sessions, [])

    def test_attempt_exhaustion_remains_in_owner_store(self):
        self.admit("limited", corrections=0)
        job = self.job(task="limited")
        job["purpose"] = "correction"
        plan = self.plan([job])
        result = self.tick(self.controller(plan))
        self.assertEqual(result["jobs"][0]["state"], "blocked")
        self.assertIn("exhausted", result["jobs"][0]["reason"])
        again = self.tick(self.controller(plan))
        self.assertEqual(again["jobs"][0]["state"], "blocked")
        record = factory.task_view(factory.load_task(self.directory / "limited.json"))
        self.assertEqual(record["allowances"]["correction"], dict(limit=0, used=0, remaining=0))

    def test_new_launch_wave_refreshes_aged_capacity(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        self.tick(controller)
        self.finish("task", "first")
        self.now = self.capacity["observed_at"] = 1061.0
        result = self.tick(controller)
        self.assertEqual(result["jobs"][1]["state"], "running")
        self.assertEqual(self.probes, 2)

    def test_metadata_failures_have_a_finite_retry_allowance(self):
        controller = self.controller(self.plan([self.job()]))
        async def unavailable(binary):
            self.probes += 1
            raise native_codex.CodexProbeError("Fixture metadata service unavailable")
        controller.probe = unavailable
        for now in (1000, 1061, 1122, 1183, 1244):
            self.now = now
            result = self.tick(controller)
            self.assertEqual(result["jobs"][0]["state"], "waiting")
        self.assertEqual(self.probes, 3)  # Initial observation plus the configured two retries.
        self.assertIn("retry allowance exhausted", result["capacity_error"])
        self.assertEqual(PlanRex.sessions, [])

    def test_preflight_failure_needs_an_explicit_controller_restart_after_repair(self):
        self.admit("other")
        checkout = self.root / "missing-worktree"
        jobs = [self.job(cwd=str(checkout)), self.job(task="other")]
        plan = self.plan(jobs, reserve=0)
        controller = self.controller(plan)
        first = self.tick(controller)
        self.assertEqual(first["jobs"][0]["state"], "blocked")
        self.assertEqual(first["jobs"][1]["state"], "running")
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(checkout), self.candidate], check=True)
        self.addCleanup(subprocess.run, ["git", "-C", str(self.repo), "worktree", "remove", str(checkout)], check=True)
        self.assertEqual(self.tick(controller)["jobs"][0]["state"], "blocked")
        resumed = self.tick(self.controller(plan))
        self.assertEqual(resumed["jobs"][0]["state"], "running")
        self.assertEqual(resumed["active_workers"], 2)

    def test_invalid_plan_refuses_before_any_external_call(self):
        valid = self.plan([self.job()])
        changes = [lambda d: d.update(schema_version=True), lambda d: d.update(schema_version=2),
            lambda d: d.update(extra="unknown"), lambda d: d["limits"].update(workers=True),
            lambda d: d["limits"].update(reserve_workers=2),
            lambda d: d["jobs"][0].update(write="yes"), lambda d: d["jobs"][0].update(candidate="HEAD"),
            lambda d: d["jobs"][0].update(commit_message="feat: Unapproved read-only commit"),
            lambda d: d["jobs"][0].update(write=True, commit_message=""),
            lambda d: d["jobs"][0].update(after=["first"]),
            lambda d: d["jobs"].append(copy.deepcopy(d["jobs"][0]))]
        for mutate in changes:
            value = copy.deepcopy(valid)
            mutate(value)
            with self.assertRaises(factory.FactoryError):
                self.controller(value)
        self.assertEqual(PlanRex.calls, [])
        self.assertEqual(self.probes, 0)

    def test_execution_lock_excludes_another_runner_and_manual_cli_launch(self):
        controller = self.controller(self.plan([self.job()]))
        original = (self.directory / "task.json").read_bytes()
        with factory.execution_lock(self.directory):
            with self.assertRaisesRegex(factory.FactoryError, "controller"):
                self.tick(controller)
            error = StringIO()
            with redirect_stderr(error):
                code = factory.main(["--state-dir", str(self.directory), "codex-start", "--task", "task",
                    "--id", "manual", "--role", "qa", "--goal", "Fixture", "--cwd", str(self.repo),
                    "--purpose", "work", "--codex", sys.executable, "--model", "fixture-model", "--effort", "high"])
            self.assertEqual(code, 1)
            self.assertIn("controller", error.getvalue())
        self.assertEqual((self.directory / "task.json").read_bytes(), original)
        self.assertEqual(PlanRex.calls, [])

    def test_run_loop_advances_dependencies_and_budget_return_keeps_live_jobs(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        sleeps = []
        def sleep(seconds):
            sleeps.append(seconds)
            self.finish("task", "first" if len(sleeps) == 1 else "second")
        times = iter([0.0, 0.0, 5.0, 5.0, 10.0, 10.0])
        result = runner.run(controller, seconds=30, timer=lambda: next(times), sleep=sleep)
        self.assertEqual(result["result"], "complete")
        self.assertEqual(sleeps, [5, 5])
        controller = self.controller(self.plan([self.job("third")]))
        times = iter([0.0, 0.0, 5.0])
        pending = runner.run(controller, seconds=5, timer=lambda: next(times), sleep=lambda _: None)
        self.assertEqual(pending["result"], "pending_at_budget")
        self.assertEqual(pending["jobs"][0]["state"], "running")
        self.assertEqual(pending["active_workers"], 1)

    def test_budget_expiry_never_dispatches_a_newly_ready_dependency(self):
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        now = [0.0]
        def sleep(seconds):
            now[0] += seconds
            self.finish("task", "first")
        pending = runner.run(controller, seconds=5, timer=lambda: now[0], sleep=sleep)
        self.assertEqual(pending["result"], "pending_at_budget")
        saved = factory.load_task(self.directory / "task.json")
        self.assertEqual(list(saved["assignments"]), ["first"])
        self.assertEqual(pending["metrics"]["launched"], 1)


if __name__ == "__main__":
    unittest.main()
