"""Controller contracts use fake Rex and native processes, never providers."""
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

    def controller(self, plan):
        return runner.Runner(self.directory, plan, codex=sys.executable, rex=sys.executable,
                             server="unix:///test.sock", probe=self.probe, clock=lambda: self.now)

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
                    clock=lambda: 1000.0, timer=lambda: 10.0, git_scope=lambda *args, **kwargs: nullcontext()))
            self.assertEqual(receipt["state"], "completed")
        finally:
            os.chdir(before)
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "exited"

    def states(self, result):
        return {(item["task"], item["id"]): item["state"] for item in result["jobs"]}

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
        self.assertEqual(len(PlanRex.sessions[0]["windows"]), 2)

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
