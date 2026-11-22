"""Controller contracts use fake Rex and native processes, never providers."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import contextlib
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
import threading
from typing import Any, Callable
import unittest
from unittest.mock import patch

import codex_worker
import factory
import native_antigravity
import native_claude
import native_codex
import native_grok
import ready_runner as runner
from test_codex_worker import FakeProcess
from test_native_claude import CLAUDE_USAGE
from test_native_grok import GROK_USAGE
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
        servers = patch.object(codex_worker, "configured_mcp_servers", return_value=[])  # No real `codex mcp list` (#153).
        servers.start()
        self.addCleanup(servers.stop)
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
        self.capacity: dict[str, Any] = dict(schema_version=1, observed_at=1000.0, binary=str(Path(sys.executable).resolve()),
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
            dict(type="item.started", item=dict(id="command-1", type="command_execution", command="cat fixture/evidence.txt",
                                                status="in_progress")),
            dict(type="item.completed", item=dict(id="command-1", type="command_execution", command="cat fixture/evidence.txt",
                                                  exit_code=0, status="completed")),
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
            os.chdir(request["cwd"])
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
                          for role in ("qa", "security", "test-automation")], workers=3, reserve=0)
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
        for identity in ("qa", "security", "test-automation"):
            request = selected["assignments"][identity]["request"]
            self.assertEqual(request["inputs"], selected["inputs"])
            self.assertEqual(request["dependencies"], ["implement"])
            self.assertNotIn("--write", request["command"])
            self.finish("task", identity)
        self.assertEqual(self.handoff(plan)["result"], "complete")
        for path, content in originals.items():
            self.assertEqual(path.read_bytes(), content)

    def test_a_writer_whose_commit_is_the_selection_reports_its_result(self):
        # The canary's writer committed, its commit became the selection, and its plan still said stale (#139).
        self.completed_writer()
        self.assertEqual(self.handoff()["metrics"]["launched"], 1)
        self.finish("task", "verify")
        writer = self.job("implement", write=True, commit_message="feat(fixture): Deliver candidate")
        self.assertEqual(self.states(self.tick(self.controller(self.plan([writer]))))[("task", "implement")], "completed")
        # The verifier's result is current too, but this plan names the original pair it never ran on.
        result = self.tick(self.controller(self.verification_plan()))
        self.assertEqual(result["jobs"][0]["state"], "stale")

    def test_a_reader_with_a_recorded_result_for_the_selection_still_reports_stale(self):
        # Review of #180: a read-only job assessed its planned pair; a result recorded for another candidate
        # through `factory.py result` must not make that plan current once the other candidate is selected.
        reader = self.plan([self.job("reader")])
        self.assertEqual(self.tick(self.controller(reader))["metrics"]["launched"], 1)
        other = subprocess.check_output([factory.git_binary(), "-C", str(self.repo), "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid", "commit-tree", self.candidate + "^{tree}", "-p", self.candidate,
            "-m", "Another candidate"], text=True, env={**factory.git_environment(),
            "GIT_AUTHOR_DATE": "2000-01-04T00:00:00Z", "GIT_COMMITTER_DATE": "2000-01-04T00:00:00Z"}).strip()
        factory.record_result(self.directory / "task.json", assignment_id="reader", candidate=other,
                              requirements="requirements-1", outcome="completed", artifacts=["report.md"])
        factory.set_inputs(self.directory / "task.json", candidate=other, requirements="requirements-1")
        self.assertEqual(self.tick(self.controller(reader))["jobs"][0]["state"], "stale")

    def second_writer(self, task):
        """A completed writer for another task, in its own worktree of the fixture repository."""
        checkout = self.root / (task + "-worktree")
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(checkout), self.candidate],
                       check=True)
        self.admit(task)
        job = self.job("implement", task=task, cwd=str(checkout), write=True, commit_message="feat(fixture): Deliver " + task)
        controller = self.controller(self.plan([job]))
        self.assertEqual(self.tick(controller)["metrics"]["launched"], 1)
        with patch.dict(os.environ, GIT_AUTHOR_DATE="2000-01-03T00:00:00Z",
                        GIT_COMMITTER_DATE="2000-01-03T00:00:00Z"):
            self.finish(task, "implement", outcome="completed",
                        edit=lambda: (checkout / "delivery.txt").write_text("Another fixture delivery\n"))
        self.tick(controller)
        return checkout

    def test_from_writer_verifies_several_tasks_and_security_in_one_plan(self):
        # With seven lines, one verification plan per task meant a round-robin of single-task runners.
        self.completed_writer()
        checkout = self.second_writer("other")
        plan = self.plan([self.job("qa", after=["implement"]),
                          self.job("security", role="security", after=["implement"]),
                          self.job("qa", task="other", cwd=str(checkout), after=["implement"])], workers=3, reserve=0)
        self.assertEqual(self.handoff(plan)["metrics"]["launched"], 3)
        for task, identities in (("task", ("qa", "security")), ("other", ("qa",))):
            record = factory.load_task(self.directory / (task + ".json"))
            target = record["assignments"]["implement"]["result"]["candidate"]
            self.assertNotEqual(target, self.candidate)
            self.assertEqual(record["inputs"], dict(candidate=target, requirements="requirements-1"))
            for identity in identities:
                request = record["assignments"][identity]["request"]
                self.assertEqual(request["inputs"], record["inputs"])
                self.assertNotIn("--write", request["command"])

    def test_from_writer_refused_task_moves_no_task_inputs(self):
        # "task" sorts before "zeta", so a selection that moved inputs task by task would have moved it already.
        before = self.completed_writer()
        self.admit("zeta")
        plan = self.plan([self.job("qa", after=["implement"]), self.job("qa", task="zeta", after=["implement"])])
        with self.assertRaisesRegex(factory.FactoryError, "existing writer assignment"):
            self.handoff(plan)
        self.assertEqual(factory.load_task(self.directory / "task.json")["inputs"], before["inputs"])

    def test_from_writer_plan_keeps_serving_after_its_task_moves_on_to_a_correction(self):
        # Observed (#130): one task's correction dirtied its writer worktree, and the next cycle of a
        # four-task verification plan refused the whole plan on that worktree.
        # Claude's reader re-checks the live worktree unless asked for collected evidence; only the run
        # that moves the inputs asks for the live one.
        self.completed_writer()
        plan = self.plan([self.job("verify", after=["implement"])])
        reader, asked = factory.evidence_reader, []
        def recording(harness):
            def read(*args, **options):
                asked.append(options.get("collected", False))
                return reader(harness)(*args, **options)
            return read
        with patch.object(factory, "evidence_reader", side_effect=recording):
            self.assertEqual(self.handoff(plan)["metrics"]["launched"], 1)
            (self.repo / "delivery.txt").write_text("A correction is rewriting the delivery\n")
            result = self.handoff(plan)
        self.assertEqual(asked, [False, True])
        self.assertEqual(result["jobs"][0]["state"], "running")
        self.assertEqual(result["metrics"]["launched"], 0)

    def test_from_writer_refuses_a_writer_that_committed_nothing(self):
        # #166: runtime 0.39.4 collected a completed writer with an unchanged commit, and verifying it re-checked
        # the old candidate. Its receipt has that shape; the runner refuses before moving any inputs.
        before = self.completed_writer()
        real = factory.evidence_reader
        def unchanged(harness):
            def read(*args, **kwargs):
                parsed, receipt, native = real(harness)(*args, **kwargs)
                commit = {**receipt["commit"], "state": "unchanged", "candidate": receipt["commit"]["parent"]}
                return parsed, {**receipt, "commit": commit, "candidate": commit["candidate"]}, native
            return read
        with patch.object(factory, "evidence_reader", side_effect=unchanged), \
                self.assertRaisesRegex(factory.FactoryError, "committed nothing"):
            self.handoff()
        self.assertEqual(factory.load_task(self.directory / "task.json")["inputs"], before["inputs"])

    def test_from_writer_launches_the_effect_specialists(self):
        # The release judgment asks for ui-design, data-engineering and sre where a change has those effects;
        # the SKILL verifies a replay onto a moved base with integration (#127).
        self.completed_writer()
        plan = self.plan([self.job(role=role, identity=role, after=["implement"])
                          for role in ("ui-design", "data-engineering", "sre", "integration")], workers=4, reserve=0)
        self.assertEqual(self.handoff(plan)["metrics"]["launched"], 4)
        with self.assertRaisesRegex(factory.FactoryError, "read-only verification jobs"):
            self.controller(self.plan([self.job("product", role="product", after=["implement"])]), from_writer="implement")

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

    def test_interpreter_takeover_refuses_both_paths_and_reuses_original_work(self):
        original_python = sys.executable
        record = self.completed_writer()
        plan = self.plan([self.job("approved", after=["implement"]),
                          self.job("running", after=["implement"], purpose="correction")])
        plan_file = self.root / "verification.json"
        plan_file.write_text(json.dumps(plan))
        original_plan = plan_file.read_bytes()
        argv = ["--state-dir", str(self.directory), "--plan", str(plan_file), "--from-writer", "implement",
                "--codex", original_python, "--rex", original_python, "--server", "unix:///test.sock", "--once"]
        alternate = "/fixture/alternate/python3.11"

        def assert_writer_refusal():
            stderr = StringIO()
            calls = list(PlanRex.calls)
            with patch.object(sys, "executable", alternate), redirect_stderr(stderr):
                self.assertEqual(runner.main(argv), 1)
            message = stderr.getvalue()
            self.assertIn("Python interpreter mismatch for 'task/implement'", message)
            self.assertIn(ascii(original_python), message)
            self.assertIn("retry the original plan with that exact interpreter", message)
            self.assertEqual(PlanRex.calls, calls)

        path = self.directory / "task.json"
        before_selection, launches = path.read_bytes(), self.launches()
        assert_writer_refusal()
        self.assertEqual(path.read_bytes(), before_selection)
        self.assertEqual(self.launches(), launches)

        self.assertEqual(self.handoff(plan)["metrics"]["launched"], 2)
        self.finish("task", "approved")
        self.assertEqual(self.handoff(plan)["metrics"]["collected"], 1)
        saved = {item: item.read_bytes() for item in self.directory.iterdir() if item.is_file()}
        sessions, launches = copy.deepcopy(PlanRex.sessions), self.launches()
        assert_writer_refusal()

        effective = {**plan, "jobs": [{**job, "candidate": record["assignments"]["implement"]["result"]["candidate"]}
                                     for job in plan["jobs"]]}
        replay = self.controller(effective)
        with patch.object(sys, "executable", alternate):
            refused = self.tick(replay)
        self.assertEqual(refused["result"], "needs_action")
        self.assertEqual(refused["metrics"]["launched"], 0)
        for item in refused["jobs"]:
            self.assertEqual(item["state"], "conflict")
            self.assertIn(f"Python interpreter mismatch for 'task/{item['id']}'", item["reason"])
            self.assertIn(ascii(original_python), item["reason"])
            self.assertIn("retry the original plan with that exact interpreter", item["reason"])

        changed = copy.deepcopy(effective)
        changed["jobs"][0]["model"] = "changed-model"
        changed["jobs"][1]["goal"] = "Changed goal"
        changed_replay = self.controller(changed)
        with patch.object(sys, "executable", alternate):
            conflicts = self.tick(changed_replay)
        for item in conflicts["jobs"]:
            self.assertEqual(item["state"], "conflict")
            self.assertIn("Plan differs from the saved immutable assignment", item["reason"])

        self.assertEqual({item: item.read_bytes() for item in self.directory.iterdir() if item.is_file()}, saved)
        self.assertEqual(PlanRex.sessions, sessions)
        self.assertEqual(self.launches(), launches)
        retry = self.handoff(plan)
        self.assertEqual(self.states(retry), {("task", "approved"): "approved", ("task", "running"): "running"})
        self.assertEqual(retry["metrics"]["launched"], 0)
        self.assertEqual({item: item.read_bytes() for item in self.directory.iterdir() if item.is_file()}, saved)
        self.assertEqual(plan_file.read_bytes(), original_plan)
        self.assertEqual(PlanRex.sessions, sessions)
        self.assertEqual(self.launches(), launches)
        self.assertEqual(factory.task_view(factory.load_task(path))["allowances"]["correction"],
                         dict(limit=1, used=1, remaining=0))

    def test_interpreter_replay_preserves_exited_verifier_until_original_retry(self):
        original_python = sys.executable
        writer = self.completed_writer()["assignments"]["implement"]
        plan = self.verification_plan(purpose="correction")
        self.assertEqual(self.handoff(plan)["metrics"]["launched"], 1)
        self.finish("task", "verify")
        effective = self.verification_plan(candidate=writer["result"]["candidate"], purpose="correction")
        path = self.directory / "task.json"
        before = factory.load_task(path)
        self.assertIsNone(before["assignments"]["verify"]["result"])
        self.assertIsNone(before["assignments"]["verify"].get("closed"))
        saved = {item: item.read_bytes() for item in self.directory.iterdir() if item.is_file()}
        sessions, launches = copy.deepcopy(PlanRex.sessions), self.launches()
        calls = len(PlanRex.calls)
        replay = self.controller(effective)

        with patch.object(sys, "executable", "/fixture/alternate/python3.11"), patch.object(
                factory, "save", wraps=factory.save) as save:
            refused = self.tick(replay)
            save.assert_not_called()
        self.assertEqual(refused["result"], "needs_action")
        self.assertEqual(refused["jobs"][0]["state"], "conflict")
        self.assertIn("Python interpreter mismatch for 'task/verify'", refused["jobs"][0]["reason"])
        self.assertIn(ascii(original_python), refused["jobs"][0]["reason"])
        self.assertEqual({key: refused["metrics"][key] for key in ("launched", "collected", "tabs_closed")},
                         dict(launched=0, collected=0, tabs_closed=0))
        self.assertFalse(any(call[:2] == ("block", "close") or call[0] == "kill" for call in PlanRex.calls[calls:]))
        self.assertEqual(PlanRex.sessions, sessions)
        self.assertEqual(self.launches(), launches)
        self.assertEqual({item: item.read_bytes() for item in self.directory.iterdir() if item.is_file()}, saved)

        for changes in (dict(goal="Changed goal"), dict(model="changed-model")):
            with self.subTest(changes=changes):
                changed = {**effective, "jobs": [{**effective["jobs"][0], **changes}]}
                changed_replay = self.controller(changed)
                calls = len(PlanRex.calls)
                with patch.object(sys, "executable", "/fixture/alternate/python3.11"):
                    conflict = self.tick(changed_replay)
                self.assertEqual(conflict["jobs"][0]["state"], "conflict")
                self.assertIn("Plan differs from the saved immutable assignment", conflict["jobs"][0]["reason"])
                self.assertEqual(conflict["active_workers"], 1)
                self.assertFalse(any(call[0] in {"block", "kill"} for call in PlanRex.calls[calls:]))
                self.assertEqual(PlanRex.sessions, sessions)
                self.assertEqual(self.launches(), launches)
                self.assertEqual({item: item.read_bytes() for item in saved}, saved)

        self.admit("other")
        for task, candidate in (("task", before["inputs"]["candidate"]), ("other", self.candidate)):
            with self.subTest(task=task):
                off_plan = self.controller(self.plan([self.job("next", task=task, candidate=candidate,
                                                               write=True, commit_message="Continue work")],
                                                     workers=2, reserve=0))
                with patch.object(sys, "executable", "/fixture/alternate/python3.11"):
                    pending = self.tick(off_plan)
                self.assertEqual({key: pending["metrics"][key] for key in ("launched", "collected", "tabs_closed")},
                                 dict(launched=0, collected=0, tabs_closed=0))
                self.assertEqual(pending["active_workers"], 1)
                self.assertEqual(self.states(pending), {(task, "next"): "waiting"})
                self.assertIn("owns this checkout", pending["jobs"][0]["reason"])
                self.assertEqual(PlanRex.sessions, sessions)
                self.assertEqual(self.launches(), launches)
                self.assertEqual({item: item.read_bytes() for item in saved}, saved)

        retry = self.tick(self.controller(effective))
        self.assertEqual(retry["result"], "complete")
        self.assertEqual(retry["metrics"]["collected"], 1)
        self.assertEqual(retry["metrics"]["tabs_closed"], 1)
        self.assertEqual(retry["metrics"]["launched"], 0)
        self.assertEqual(retry["active_workers"], 0)
        self.assertEqual(self.launches(), launches)
        after = factory.load_task(path)
        self.assertEqual(after["assignments"]["verify"]["result"]["outcome"], "approved")
        self.assertEqual(factory.task_view(after)["allowances"], factory.task_view(before)["allowances"])
        self.assertEqual(saved[self.directory / "task.verify.codex.json"],
                         (self.directory / "task.verify.codex.json").read_bytes())

    def test_from_writer_interpreter_hint_requires_unambiguous_collected_command(self):
        record = self.completed_writer()
        path = self.directory / "task.json"
        command = record["assignments"]["implement"]["request"]["command"]
        alternate = "/fixture/alternate/python3.11"
        plausible = "/fixture/saved Python; $unused"
        nul_command = command.copy()
        nul_command[nul_command.index("--codex") + 1] = "/fixture/codex\0invalid"
        refused = "needs a collected, completed native writer"
        calls = list(PlanRex.calls)
        controller = self.controller(self.verification_plan(), from_writer="implement")
        for label, saved_command, proven, message in (
            ("relative", ["python3.11", *command[1:]], True, "Invalid assignment identity or request"),
            ("other worker path", [*command[:3], "/fixture/codex_worker.py", *command[4:]], True, refused),
            ("reordered flags", [command[0], "-s", "-E", *command[3:]], True, refused),
            ("duplicated writer flag", [*command, "--write"], True, refused),
            ("abbreviated flag", [token if token != "--model" else "--mod" for token in command], True, refused),
            ("control character", ["/fixture/python\n3.11", *command[1:]], True, refused),
            ("NUL in provider path", nul_command, True, refused),
            ("unproven collection", command, False, refused),
            ("plausible path", [plausible, *command[1:]], True, ascii(plausible)),
            ("historical Codex prefix", [command[0], *command[3:]], True, ascii(command[0])),
        ):
            altered = copy.deepcopy(record)
            source = altered["assignments"]["implement"]
            source["request"]["command"], source["native_collected"] = saved_command, proven
            factory.save(path, altered)
            before = path.read_bytes()
            with self.subTest(label=label), patch.object(sys, "executable", alternate):
                with self.assertRaises(factory.FactoryError) as caught:
                    self.tick(controller)
                self.assertIn(message, str(caught.exception))
                if message == refused:
                    self.assertNotIn("retry the original plan", str(caught.exception))
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(PlanRex.calls, calls)

    def test_off_plan_legacy_worker_survives_while_same_interpreter_peers_tidy(self):
        original = self.controller(self.plan([self.job()]))
        self.assertEqual(self.tick(original)["metrics"]["launched"], 1)
        self.finish("task", "first")
        path = self.directory / "task.json"
        record = factory.load_task(path)
        protected = record["assignments"]["first"]
        del protected["request"]["command"][1:3]  # Historical Codex used no -E/-s prefix.
        factory.save(path, record)

        self.admit("other")
        peer = self.job("peer", task="other")
        alternate_plan = self.controller(self.plan([peer]))
        alternate = self.root / "alternate-python"
        alternate.symlink_to(sys.executable)
        with patch.object(sys, "executable", str(alternate)):
            # The peers were already dispatched outside this plan's scheduler.
            for task in ("task", "other"):
                with factory.task_lock(self.directory, task) as peer_path:
                    job = {**alternate_plan.plan["jobs"][0], "task": task}
                    factory.launch(peer_path, task=task, assignment_id="peer", role=job["role"], goal=job["goal"],
                                   cwd=job["cwd"], command=alternate_plan.command(peer_path, job),
                                   purpose=job["purpose"], binary=alternate_plan.rex, server=alternate_plan.server)
                self.finish(task, "peer")
            launches = self.launches()
            result = self.tick(alternate_plan)

        self.assertEqual(self.states(result), {("other", "peer"): "approved"})
        self.assertEqual({key: result["metrics"][key] for key in ("launched", "collected", "tabs_closed")},
                         dict(launched=0, collected=2, tabs_closed=2))
        self.assertEqual(result["active_workers"], 1)
        self.assertEqual(result["tidy_issues"], [])
        self.assertEqual(factory.load_task(path)["assignments"]["first"], protected)
        for task in ("task", "other"):
            assignment = factory.load_task(self.directory / (task + ".json"))["assignments"]["peer"]
            self.assertEqual(assignment["result"]["outcome"], "approved")
            self.assertEqual(assignment["closed"]["state"], "exited")

        retry = self.tick(self.controller(original.supplied_plan))
        self.assertEqual(self.states(retry), {("task", "first"): "approved"})
        self.assertEqual(retry["metrics"]["tabs_closed"], 1)
        self.assertEqual(retry["active_workers"], 0)
        self.assertEqual(self.launches(), launches)
        self.assertEqual(PlanRex.sessions, [])

    def test_interpreter_replay_compares_all_worker_prefixes_and_frozen_arguments(self):
        original_python = sys.executable
        plan = self.plan([self.job(harness, harness=harness) for harness in factory.NATIVE_HARNESSES]
                         + [self.job("historical")], workers=5, reserve=0)
        binaries = dict(claude=original_python, grok=original_python, antigravity=original_python)
        self.assertEqual(self.tick(self.controller(plan, **binaries))["metrics"]["launched"], 5)
        path = self.directory / "task.json"
        record = factory.load_task(path)
        del record["assignments"]["historical"]["request"]["command"][1:3]
        factory.save(path, record)
        saved, launches = path.read_bytes(), self.launches()
        changed = copy.deepcopy(plan)
        changed["jobs"][1]["seconds"] += 1
        replay, changed_replay = self.controller(plan, **binaries), self.controller(changed, **binaries)
        with patch.object(sys, "executable", "/fixture/alternate/python3.11"):
            refused = self.tick(replay)
            other_conflict = self.tick(changed_replay)["jobs"][1]
        self.assertEqual(refused["metrics"]["launched"], 0)
        self.assertEqual(other_conflict["state"], "conflict")
        self.assertNotIn("Python interpreter mismatch", other_conflict["reason"])
        for item in refused["jobs"]:
            self.assertEqual(item["state"], "conflict")
            self.assertIn(ascii(original_python), item["reason"])
        retry = self.tick(self.controller(plan, **binaries))
        self.assertEqual([item["state"] for item in retry["jobs"]], ["running"] * 5)
        self.assertEqual(path.read_bytes(), saved)
        self.assertEqual(self.launches(), launches)

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
        # Recorded at the selected candidate, it is checked as collected evidence, which a manual record never is.
        with self.assertRaisesRegex(factory.FactoryError, "lacks proven strict collection"):
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

    def test_cli_refuses_relative_or_blank_optional_harness_paths(self):
        plan_file = self.root / "harness-plan.json"
        argv = ["--state-dir", str(self.directory), "--plan", str(plan_file),
                "--codex", sys.executable, "--rex", sys.executable,
                "--server", "unix:///test.sock", "--once"]
        with (patch.object(factory, "relocate"),
              patch.object(factory, "launch") as dispatch,
              patch.object(runner.Runner, "refresh_capacity") as codex_probe,
              patch.object(native_claude, "probe_usage", return_value=[]) as claude_probe,
              patch.object(native_grok, "probe_usage", return_value=[]) as grok_probe,
              patch.object(native_antigravity, "probe", return_value=dict(groups=[])) as antigravity_probe):
            for jobs in ([], [self.job()]):
                plan_file.write_text(json.dumps(self.plan(jobs)))
                for flag in ("--claude", "--grok", "--antigravity"):
                    for binary in ("relative/harness", "", " "):
                        with self.subTest(jobs=len(jobs), flag=flag, binary=binary):
                            output, error = StringIO(), StringIO()
                            with redirect_stdout(output), redirect_stderr(error):
                                code = runner.main([*argv, flag, binary])
                            self.assertEqual(code, 1)
                            self.assertEqual(output.getvalue(), "")
                            self.assertIn(f"Supply an absolute path for {flag}", error.getvalue())
            for boundary in (dispatch, codex_probe, claude_probe, grok_probe, antigravity_probe):
                boundary.assert_not_called()
        self.assertEqual(PlanRex.calls, [])

    def test_host_markers_accepted_and_not_forwarded_to_harness_probe(self):
        # Test 1 (ruling): host markers pass through probe and environment; values not forwarded.
        host_markers = {
            "CLAUDE_CODE_CHILD_SESSION": "fixture-private-session",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_ENTRYPOINT": "fixture-private-entry",
            "CLAUDE_CODE_EXECPATH": "/fixture/not-the-binary",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/fixture/private-socket",
            "CLAUDE_CODE_MESSAGING_TOKEN": "fixture-private-token",
            "CLAUDE_CODE_SAFE_MODE": "1",
            "CLAUDE_CODE_SESSION_ATTENDED": "0",
            "CLAUDE_CODE_SESSION_ID": "fixture-private-session-id",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_PID": "99999",
            "CLAUDECODE": "1",
        }
        version_response = subprocess.CompletedProcess([], 0, "2.1.319 (Claude Code)\n", "")
        auth_response = subprocess.CompletedProcess([], 0,
            json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}), "")

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_binary = Path(tmpdir) / "fake-claude"
            fake_binary.write_text("#!/bin/sh\nexit 1\n")
            fake_binary.chmod(0o755)
            config_dir = Path(tmpdir) / "config"
            config_dir.mkdir()

            controller = self.controller(self.plan([]), claude=str(fake_binary))

            cases = [
                {},  # clean
                host_markers,  # all 13 with synthetic values
                {k: "" for k in host_markers},  # all 13 empty
                {**host_markers, native_claude.CONFIG_DIRECTORY: str(config_dir)},  # + CLAUDE_CONFIG_DIR
            ]

            clean_windows = None
            real_probe_usage = native_claude.probe_usage

            for case_index, extra_env in enumerate(cases):
                stage_runs = []
                stage_interactives = []

                def make_run(stages):
                    def recording_run(cmd, **kwargs):
                        stages.append(kwargs.get("env", {}))
                        return version_response if "--version" in cmd else auth_response
                    return recording_run

                # Quota above reserve: 0% used → 100% remaining, above the plan's 15% minimum.
                above_reserve_usage = CLAUDE_USAGE.replace("100% 100% used", "0% 0% used")

                def make_interactive(stages, usage=above_reserve_usage):
                    def recording_interactive(command, cwd, env, **kwargs):
                        stages.append(env)
                        return usage
                    return recording_interactive

                _run = make_run(stage_runs)
                _interactive = make_interactive(stage_interactives)

                def injected_probe_usage(binary, cwd, _r=_run, _i=_interactive,
                                         _real=real_probe_usage):
                    return _real(binary, cwd,
                        auth_probe=lambda b, c: native_claude.probe(b, c, run=_r),
                        interactive=_i)

                with (patch.dict(os.environ, extra_env, clear=True),
                      patch.object(native_claude, "probe_usage", side_effect=injected_probe_usage)):
                    result = controller.harnesses()

                # Available is null (above reserve) for all four cases.
                self.assertIsNone(result["claude"]["available"], f"case {case_index}: {result['claude']}")
                # Exactly three child stages per case: two from probe(), one from interactive.
                self.assertEqual(len(stage_runs), 2, f"case {case_index}: unexpected subprocess stage count")
                self.assertEqual(len(stage_interactives), 1, f"case {case_index}: unexpected interactive stage count")
                # Same weekly window in all cases.
                windows = result["claude"]["windows"]
                if clean_windows is None:
                    clean_windows = windows
                else:
                    self.assertEqual(windows, clean_windows, f"case {case_index}: window differs from clean")

                for env in stage_runs + stage_interactives:
                    # Synthetic marker values must be absent from child environments.
                    for key, value in host_markers.items():
                        if value and value not in ("0", "1"):
                            self.assertNotIn(value, env.values(), f"case {case_index}: {key} value leaked to child")
                    # EXECPATH marker must never replace the supplied executable.
                    self.assertNotEqual(env.get("CLAUDE_CODE_EXECPATH"), "/fixture/not-the-binary",
                                        f"case {case_index}: EXECPATH leaked to child")

                # Case 4 (index 3): config dir is forwarded in all three stages.
                if case_index == 3:
                    for env in stage_runs + stage_interactives:
                        self.assertEqual(env.get(native_claude.CONFIG_DIRECTORY), str(config_dir),
                                         f"case {case_index}: config dir not forwarded to child")

    def test_harness_probe_refuses_override_markers_even_with_host_markers(self):
        # Test 2 (ruling): override markers refuse before any child starts, even with all host markers present.
        host_markers = {
            "CLAUDE_CODE_CHILD_SESSION": "fixture-private-session",
            "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "0",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_ENTRYPOINT": "fixture-private-entry",
            "CLAUDE_CODE_EXECPATH": "/fixture/not-the-binary",
            "CLAUDE_CODE_MESSAGING_SOCKET": "/fixture/private-socket",
            "CLAUDE_CODE_MESSAGING_TOKEN": "fixture-private-token",
            "CLAUDE_CODE_SAFE_MODE": "1",
            "CLAUDE_CODE_SESSION_ATTENDED": "0",
            "CLAUDE_CODE_SESSION_ID": "fixture-private-session-id",
            "CLAUDE_EFFORT": "high",
            "CLAUDE_PID": "99999",
            "CLAUDECODE": "1",
        }
        # Each existing explicit cloud or routing selector refuses before spawn.
        override_cases = [
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
            "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SUBAGENT_MODEL",  # synthetic unobserved CLAUDE_CODE_* marker
            "CLAUDE_EFFORT_LEVEL",  # suffixed lookalike of exempt CLAUDE_EFFORT
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_binary = Path(tmpdir) / "fake-claude"
            fake_binary.write_text("#!/bin/sh\nexit 1\n")
            fake_binary.chmod(0o755)

            controller = self.controller(self.plan([]), claude=str(fake_binary))

            for override_name in override_cases:
                with self.subTest(override=override_name):
                    run_calls = []

                    def intercepted_probe_usage(binary, cwd, _calls=run_calls):
                        def recording_run(cmd, **kwargs):
                            _calls.append(cmd)
                            raise AssertionError("child started unexpectedly")
                        native_claude.probe(binary, cwd, run=recording_run)
                        raise AssertionError("probe should have refused")

                    env = {**host_markers, override_name: ""}
                    with (patch.dict(os.environ, env, clear=True),
                          patch.object(native_claude, "probe_usage", side_effect=intercepted_probe_usage)):
                        result = controller.harnesses()

                    self.assertFalse(result["claude"]["available"], override_name)
                    self.assertEqual(result["claude"]["windows"], [], override_name)
                    self.assertEqual(run_calls, [], f"{override_name}: child started before refusal")

    def test_harnesses_report_each_passed_harness_capacity(self):
        # Antigravity reset and neither line noticed for hours; the operator had to relay it by hand.
        groups = [dict(group="Gemini", window="weekly", percent_remaining=100.0, resets_at="2026-11-01T00:00:00Z")]
        controller = self.controller(self.plan([self.job()]), claude=sys.executable, grok=sys.executable,
                                     antigravity=sys.executable)
        claude = [dict(pool="Current week (all models)", remaining_percent=0, resets_at="Nov 21 at 2pm"),
                  dict(pool="Current week (Fable)", remaining_percent=100, resets_at="Nov 21 at 2pm")]
        grok = [dict(pool="SuperGrok", remaining_percent=80, resets_at="November 15, 08:40")]
        with (patch.object(native_antigravity, "probe", return_value=dict(groups=groups)),
              patch.object(native_claude, "probe_usage", return_value=claude),
              patch.object(native_grok, "probe_usage", return_value=grok)):
            report = controller.harnesses()
        self.assertEqual(report["codex"], dict(available=True, reason=None, windows=[dict(pool="codex", remaining_percent=80, resets_at=None)]))
        self.assertEqual(report["antigravity"]["windows"],
                         [dict(pool="Gemini", remaining_percent=100.0, resets_at="2026-11-01T00:00:00Z")])
        self.assertIsNone(report["antigravity"]["available"])
        self.assertIn("unverified", report["antigravity"]["reason"])
        self.assertEqual((report["claude"]["available"], report["grok"]["available"]), (False, None))
        self.assertEqual((report["claude"]["windows"], report["grok"]["windows"]), (claude, grok))
        self.assertIn("Nov 21 at 2pm", report["claude"]["reason"])
        self.assertIn("Verify model eligibility", report["grok"]["reason"])
        with (patch.object(native_antigravity, "probe", side_effect=native_antigravity.UsageError("signed out")),
              patch.object(native_claude, "probe_usage", side_effect=native_claude.ClaudeProbeError("missing panel")),
              patch.object(native_grok, "probe_usage", side_effect=native_grok.GrokProbeError("unrecognized output"))):
            failed = controller.harnesses()
        for route, reason in (("claude", "missing panel"), ("grok", "unrecognized output"), ("antigravity", "signed out")):
            self.assertEqual(failed[route], dict(available=False, reason=reason, windows=[]))
        # The CLI result carries the report, and its metrics count the probes behind it (review of #154).
        fresh = self.controller(self.plan([self.job()]), antigravity=sys.executable)
        with patch.object(native_antigravity, "probe", return_value=dict(groups=groups)):
            result: dict[str, Any] = runner.run(fresh, seconds=30, once=True, harnesses=True)
        self.assertEqual(result["harnesses"]["antigravity"]["windows"][0]["pool"], "Gemini")
        self.assertEqual(result["metrics"]["quota_probes"], 2)  # Codex and Antigravity, inside the line's lock.
        # Quota shown but ordinary use refused: the launch gate says no, so does the report (review of #154).
        self.capacity["ordinaryUsageAllowed"] = False
        blocked = self.controller(self.plan([self.job()])).harnesses()["codex"]
        self.assertEqual((blocked["available"], blocked["windows"][0]["remaining_percent"]), (False, 80))
        self.assertIn("ordinary-usage", blocked["reason"])
        self.capacity.update(ordinaryUsageAllowed=True, models=[dict(model="fixture-model", effort_ids=[])])
        empty = self.controller(self.plan([self.job()])).harnesses()["codex"]
        self.assertEqual(empty["available"], False)
        self.assertIn("no model with a supported effort", empty["reason"])
        self.assertEqual(set(self.controller(self.plan([self.job()])).harnesses()), {"codex"})

    def test_cli_uses_resolved_claude_probe_folder_or_system_tmp(self):
        folder = self.root / "quota"
        folder.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(folder, target_is_directory=True)
        plan_file = self.root / "empty-plan.json"
        plan_file.write_text(json.dumps(self.plan([])))
        argv = ["--state-dir", str(self.directory), "--plan", str(plan_file), "--codex", sys.executable,
                "--claude", sys.executable, "--rex", sys.executable, "--server", "unix:///test.sock", "--once"]
        for flags, expected in (([], Path("/tmp").resolve()), (["--claude-usage-cwd", str(alias)], folder)):
            with (self.subTest(flags=flags), patch.object(factory, "relocate"),
                  patch.object(runner.Runner, "refresh_capacity"), redirect_stdout(StringIO()),
                  patch.object(native_claude, "probe_usage", return_value=[]) as observe):
                self.assertEqual(runner.main([*argv, *flags]), 0)
                observe.assert_called_once_with(Path(sys.executable).resolve(), expected)

    def test_cli_refuses_invalid_claude_probe_folder_before_dispatch_or_probes(self):
        plan_file = self.root / "harness-plan.json"
        plan_file.write_text(json.dumps(self.plan([self.job()])))
        denied = self.root / "denied"
        denied.mkdir()
        access = os.access
        argv = ["--state-dir", str(self.directory), "--plan", str(plan_file), "--codex", sys.executable,
                "--claude", sys.executable, "--rex", sys.executable, "--server", "unix:///test.sock", "--once"]
        with (patch.object(factory, "relocate"), patch.object(runner, "run") as dispatch,
              patch.object(runner.Runner, "harnesses") as probes,
              patch.object(native_claude.os, "access", side_effect=lambda path, mode: path != denied and access(path, mode))):
            for cwd in ("", "relative", str(self.root / "missing"), str(plan_file), str(denied)):
                with self.subTest(cwd=cwd), redirect_stderr(StringIO()) as error:
                    self.assertEqual(runner.main([*argv, "--claude-usage-cwd", cwd]), 1)
                    self.assertIn("absolute, existing, accessible directory for --claude-usage-cwd", error.getvalue())
            dispatch.assert_not_called()
            probes.assert_not_called()
        self.assertEqual(PlanRex.calls, [])

    def test_non_codex_report_distinguishes_exhaustion_from_unverified_pool_binding(self):
        controller = self.controller(self.plan([]), claude=sys.executable, grok=sys.executable,
                                     antigravity=sys.executable)
        claude = [dict(pool="Current week (all models)", remaining_percent=40, resets_at="Nov 21 at 2pm"),
                  dict(pool="Current week (Fable)", remaining_percent=0, resets_at="Nov 21 at 2pm")]
        groups = [dict(group="Gemini", window="weekly", percent_remaining=0, resets_at="2026-11-01T00:00:00Z")]
        grok = [dict(pool="SuperGrok", remaining_percent=0, resets_at="November 15, 08:40")]
        with (patch.object(native_antigravity, "probe", return_value=dict(groups=groups)),
              patch.object(native_claude, "probe_usage", return_value=claude),
              patch.object(native_grok, "probe_usage", return_value=grok)):
            report = controller.harnesses()
        self.assertIsNone(report["claude"]["available"])
        self.assertFalse(report["grok"]["available"])
        self.assertFalse(report["antigravity"]["available"])
        self.assertIn("November 15, 08:40", report["grok"]["reason"])
        self.assertEqual(controller.metrics["quota_probes"], 4)

    def test_harnesses_compare_native_fractional_quota_at_and_above_reserve(self):
        plan = self.plan([self.job()])
        plan["limits"]["minimum_remaining_percent"] = 0.2
        for used, remaining in (("99.8", 0.2), ("99.79", 0.21)):
            with self.subTest(used=used):
                raw_codex = dict(schema_version=1, observed_at=self.now, route="codex-native",
                    binary=str(Path(sys.executable).resolve()), account=dict(account=dict(type="chatgpt")),
                    rate_limits=dict(ordinaryUsageAllowed=True, rateLimits=dict(limitId="codex", spendControlReached=False,
                        primary=dict(usedPercent=json.loads(used)))),
                    models=[dict(model="fixture-model", supportedReasoningEfforts=[dict(reasoningEffort="high")])])
                groups = [dict(group="Gemini", window="weekly", percent_remaining=remaining,
                               resets_at="2026-11-01T00:00:00Z")]
                claude = CLAUDE_USAGE.replace("100% 100% used", f"{used}% {used}% used")
                grok = GROK_USAGE.replace("97%", f"{used}%")
                controller = runner.Runner(self.directory, plan, codex=sys.executable, rex=sys.executable,
                    server="unix:///test.sock", clock=lambda: self.now,
                    claude=sys.executable, grok=sys.executable, antigravity=sys.executable)
                launches_before = self.launches()
                with (patch.object(native_codex, "probe", return_value=raw_codex) as probe,
                      patch.object(native_antigravity, "probe", return_value=dict(groups=groups)),
                      patch.object(native_claude, "probe_usage", side_effect=lambda *_: native_claude.parse_usage(claude)),
                      patch.object(native_grok, "probe_usage", side_effect=lambda *_: native_grok.parse_usage(grok))):
                    result = runner.run(controller, seconds=30, once=True, harnesses=True)
                probe.assert_awaited_once()
                expected_launches = int(remaining > 0.2)
                self.assertEqual(self.launches() - launches_before, expected_launches)
                self.assertEqual(result["metrics"]["launched"], expected_launches)
                for harness, pool in (("claude", "Current week (all models)"), ("grok", "SuperGrok"),
                                      ("antigravity", "Gemini"), ("codex", "codex")):
                    observed = result["harnesses"][harness]
                    self.assertEqual(observed["windows"][0]["pool"], pool)
                    self.assertEqual(observed["windows"][0]["remaining_percent"], remaining)
                    if remaining == 0.2:
                        self.assertIs(observed["available"], False)
                        self.assertIn("reserve", observed["reason"])
                    else:
                        self.assertIs(observed["available"], True if harness == "codex" else None)
                        if harness in {"claude", "grok"}:
                            self.assertIn("above reserve", observed["reason"])

    def test_codex_refuses_a_code_review_it_cannot_scope(self):
        # Codex takes no --base, so a Codex code review saw the whole tree and guessed what changed.
        result = self.tick(self.controller(self.plan([self.job("review", role="code-review")])))
        self.assertEqual(result["jobs"][0]["state"], "blocked")
        self.assertIn("run code reviews on Claude, Grok or Antigravity with --base", result["jobs"][0]["reason"])
        self.assertEqual(result["metrics"]["launched"], 0)

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
        # The commit waits for its --from-writer verification to become the selection; nothing needs action (#123).
        completed = self.tick(self.controller(plan))
        self.assertEqual(completed["result"], "complete")
        self.assertEqual(self.states(completed)[("task", "first")], "completed")
        self.assertEqual(factory.load_task(self.directory / "task.json")["inputs"]["candidate"], self.candidate)
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

    def test_verifier_in_the_writer_plan_never_reviews_the_old_candidate(self):
        # Only --from-writer selects the writer's commit; a dependent job on the plan's inputs refuses to launch.
        plan = self.plan([self.job(role="backend", write=True, commit_message="feat(fixture): Deliver an explicit change"),
                          self.job("qa", after=["first"])])
        self.assertEqual(self.tick(self.controller(plan))["metrics"]["launched"], 1)
        self.finish("task", "first", outcome="completed", edit=True)
        result = self.tick(self.controller(plan))
        self.assertEqual(result["result"], "needs_action")
        self.assertEqual(self.states(result), {("task", "first"): "completed", ("task", "qa"): "blocked"})
        self.assertIn("current inputs", result["jobs"][1]["reason"])
        self.assertEqual(result["metrics"]["launched"], 0)
        self.assertNotIn("qa", factory.load_task(self.directory / "task.json")["assignments"])

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
        self.assertEqual(self.tick(self.controller(plan))["result"], "complete")
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

    def test_closed_interpreter_mismatch_allows_idle_session_cleanup(self):
        path = self.directory / "task.json"
        earlier, newer = self.job("earlier"), self.job("newer")
        first = self.controller(self.plan([earlier]))
        alternate = self.root / "alternate-python"
        alternate.symlink_to(sys.executable)
        with patch.object(sys, "executable", str(alternate)):
            self.assertEqual(self.tick(first)["metrics"]["launched"], 1)
            self.finish("task", "earlier")
            self.assertEqual(self.tick(first)["metrics"]["tabs_closed"], 1)
        closed = factory.load_task(path)["assignments"]["earlier"]
        self.assertIsNotNone(closed["closed"])

        self.assertEqual(self.tick(self.controller(self.plan([newer])))["metrics"]["launched"], 1)
        before = factory.load_task(path)
        self.assertIsNotNone(before["session_id"])
        self.finish("task", "newer")
        result = self.tick(self.controller(self.plan([earlier, newer])))
        self.assertEqual(self.states(result), {("task", "earlier"): "conflict", ("task", "newer"): "approved"})
        self.assertEqual({key: result["metrics"][key] for key in ("launched", "collected", "tabs_closed")},
                         dict(launched=0, collected=1, tabs_closed=1))
        self.assertEqual(result["tidy_issues"], [])
        after = factory.load_task(path)
        self.assertEqual(after["assignments"]["earlier"], closed)
        self.assertEqual(after["assignments"]["newer"]["closed"]["state"], "exited")
        self.assertEqual(factory.task_view(after)["allowances"], factory.task_view(before)["allowances"])
        self.assertIsNone(after["session_id"])
        self.assertEqual(PlanRex.sessions, [])

    def test_collected_alternate_worker_can_be_tidied_by_another_plan(self):
        path = self.directory / "task.json"
        original = self.controller(self.plan([self.job()]))
        self.assertEqual(self.tick(original)["metrics"]["launched"], 1)
        self.finish("task", "first")
        with patch.object(factory, "tidy", side_effect=factory.FactoryError("Fixture tidy interruption")):
            self.assertEqual(self.tick(original)["metrics"]["collected"], 1)
        before = factory.load_task(path)
        saved = before["assignments"]["first"]
        self.assertEqual(saved["result"]["outcome"], "approved")
        self.assertIsNone(saved.get("closed"))

        with factory.task_lock(self.directory, "task") as locked:
            factory.hold_task(locked, "Operator hold")
        peer = self.controller(self.plan([self.job("next")]))
        with patch.object(sys, "executable", "/fixture/alternate/python3.11"):
            result = self.tick(peer)
        self.assertEqual(result["metrics"]["launched"], 0)
        self.assertEqual(result["active_workers"], 0)
        self.assertEqual(result["metrics"]["collected"], 0)
        self.assertEqual(result["metrics"]["tabs_closed"], 1)
        after = factory.load_task(path)
        self.assertEqual(after["assignments"]["first"]["result"], saved["result"])
        self.assertEqual(after["assignments"]["first"]["request"], saved["request"])
        self.assertEqual(after["assignments"]["first"]["closed"]["state"], "exited")
        self.assertIsNone(after["session_id"])

    def test_closed_only_alternate_replay_retries_cleanup_on_pinned_host(self):
        path = self.directory / "task.json"
        plan = self.plan([self.job()], workers=1, reserve=0)
        original = self.controller(plan)
        alternate = self.root / "alternate-python"
        alternate.symlink_to(sys.executable)
        with patch.object(sys, "executable", str(alternate)):
            self.assertEqual(self.tick(original)["metrics"]["launched"], 1)
            self.finish("task", "first")
            with patch.object(factory, "close_idle_session", side_effect=factory.FactoryError("Fixture cleanup interruption")):
                interrupted = self.tick(original)
        self.assertEqual(interrupted["tidy_issues"], [dict(task="task", reason="Fixture cleanup interruption")])
        before = factory.load_task(path)
        self.assertIsNotNone(before["session_id"])
        self.assertEqual(before["assignments"]["first"]["closed"]["state"], "exited")
        self.assertEqual(before["assignments"]["first"]["result"]["outcome"], "approved")

        unrelated = dict(session_id="unrelated-session", label="other-line", windows=[])
        PlanRex.sessions.append(unrelated)
        original_sessions = copy.deepcopy(PlanRex.sessions)
        before_bytes = path.read_bytes()
        receipt = self.directory / "task.first.codex.json"
        capture = Path(before["assignments"]["first"]["closed"]["capture"])
        evidence = {file: file.read_bytes() for file in (receipt, capture)}
        call_start = len(PlanRex.calls)
        rex_error = "Connected Rex host differs from the recorded task host"
        # The other host has its own sessions. Only the peer may launch there.
        with patch.object(PlanRex, "host", "unexpected-host"), patch.object(PlanRex, "sessions", []):
            refused = self.tick(self.controller(plan))
            historical, = refused["jobs"]
            self.assertEqual(historical["state"], "conflict")
            self.assertIn("Python interpreter mismatch for 'task/first'", historical["reason"])
            self.assertEqual(refused["tidy_issues"], [dict(task="task", reason=rex_error)])
            self.assertEqual(refused["issues"], [])
            self.assertEqual(refused["active_workers"], 0)
            self.assertEqual({key: refused["metrics"][key] for key in ("launched", "collected", "tabs_closed")},
                             dict(launched=0, collected=0, tabs_closed=0))
            self.assertEqual(PlanRex.calls[call_start:], [("whoami", "--json")])
            self.assertEqual(PlanRex.sessions, [])

            self.admit("peer")
            peer_plan = self.plan([self.job(), self.job(task="peer")], workers=1, reserve=0)
            peer_controller = self.controller(peer_plan)
            admitted = self.tick(peer_controller)
            self.assertEqual(admitted["metrics"]["launched"], 1)
            self.assertEqual(self.states(admitted), {("task", "first"): "conflict", ("peer", "first"): "running"})
            self.finish("peer", "first")
            self.assertEqual(self.tick(peer_controller)["metrics"]["tabs_closed"], 1)
            self.assertEqual(PlanRex.sessions, [])
        self.assertEqual(PlanRex.sessions, original_sessions)
        self.assertEqual(path.read_bytes(), before_bytes)

        peer_path = self.directory / "peer.json"
        peer_before = factory.load_task(peer_path)
        retry = self.plan([self.job(), self.job("next", task="peer")], workers=1, reserve=0)
        call_start = len(PlanRex.calls)
        result = self.tick(self.controller(retry))
        self.assertEqual(self.states(result), {("task", "first"): "conflict", ("peer", "next"): "blocked"})
        self.assertEqual(result["jobs"][1]["reason"], rex_error)
        self.assertEqual(result["active_workers"], 0)
        self.assertEqual(result["metrics"]["launched"], 0)
        self.assertEqual(result["metrics"]["collected"], 0)
        self.assertEqual(result["tidy_issues"], [dict(task="peer", reason=rex_error)])
        self.assertEqual([call for call in PlanRex.calls[call_start:] if call[0] == "kill"],
                         [("kill", before["session_id"])])
        self.assertEqual(PlanRex.sessions, [unrelated])
        after = factory.load_task(path)
        self.assertEqual(after, {**before, "session_id": None})
        self.assertEqual({file: file.read_bytes() for file in evidence}, evidence)
        self.assertEqual(factory.load_task(peer_path), peer_before)

    def test_connection_error_counts_collected_worker_without_closed_evidence(self):
        self.admit("peer")
        original = self.controller(self.plan([self.job()]))
        self.assertEqual(self.tick(original)["metrics"]["launched"], 1)
        self.finish("task", "first")
        with patch.object(factory, "tidy", side_effect=factory.FactoryError("Fixture tidy interruption")):
            self.assertEqual(self.tick(original)["metrics"]["collected"], 1)
        saved = factory.load_task(self.directory / "task.json")["assignments"]["first"]
        self.assertEqual(saved["result"]["outcome"], "approved")
        self.assertIsNone(saved.get("closed"))

        plan = self.plan([self.job(), self.job(task="peer")], workers=1, reserve=0)
        call_start = len(PlanRex.calls)
        with patch.object(PlanRex, "host", "unexpected-host"):
            result = self.tick(self.controller(plan))
        self.assertEqual(self.states(result), {("task", "first"): "unknown", ("peer", "first"): "waiting"})
        self.assertEqual(result["jobs"][1]["reason"], "Worker limit is occupied, including unknown processes")
        self.assertEqual(result["active_workers"], 1)
        self.assertEqual(PlanRex.calls[call_start:], [("whoami", "--json")])

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

    def test_abandoned_launch_frees_its_slot_on_pinned_rex_host_mismatch(self):
        self.admit("peer")
        controller = self.controller(self.plan([self.job()], workers=1, reserve=0))
        call = PlanRex.call

        def reject(rex, *args, **kwargs):
            if args[0] == "new":
                raise factory.FactoryError("Error: unknown flag: --focus")
            return call(rex, *args, **kwargs)

        with patch.object(PlanRex, "call", reject):
            rejected, = self.tick(controller)["jobs"]
        self.assertIn("unknown flag: --focus", rejected["reason"])
        reason = "Rex rejected an unknown flag"
        with factory.task_lock(self.directory, "task") as path:
            record = factory.load_task(path)
            factory.abandon_launch(path, factory.connect(record), assignment_id="first", reason=reason)
        before = path.read_bytes()
        original_sessions = copy.deepcopy(PlanRex.sessions)
        peer_path = self.directory / "peer.json"
        self.assertIsNone(factory.load_task(peer_path)["rex"])

        plan = self.plan([self.job(), self.job("second", after=["first"]), self.job(task="peer")], workers=1, reserve=0)
        call_start = len(PlanRex.calls)
        with patch.object(PlanRex, "host", "unexpected-host"), patch.object(PlanRex, "sessions", []):
            result = self.tick(self.controller(plan))
            self.assertEqual(self.states(result), {("task", "first"): "abandoned", ("task", "second"): "blocked",
                                                   ("peer", "first"): "running"})
            self.assertEqual(result["jobs"][0]["reason"], reason)
            self.assertEqual(result["active_workers"], 1)
            self.assertEqual(result["metrics"]["launched"], 1)
            self.assertEqual(result["tidy_issues"], [dict(task="task", reason="Connected Rex host differs from the recorded task host")])
            peer = factory.load_task(peer_path)
            self.assertEqual(peer["rex"]["host_id"], "unexpected-host")
            session, = PlanRex.sessions
            self.assertEqual(session["session_id"], peer["session_id"])
            window, = session["windows"]
            self.assertEqual(window["label"], peer["assignments"]["first"]["window_label"])
            # The first whoami is the rejected pinned connection. All remaining calls launch the peer.
            pinned, peer_host, peer_list, peer_launch, peer_inspect = PlanRex.calls[call_start:]
            self.assertEqual(pinned, ("whoami", "--json"))
            self.assertEqual(peer_host, ("whoami", "--json"))
            self.assertEqual(peer_list, ("ls", "--json"))
            self.assertEqual(peer_launch[:4], ("new", session["label"], "--window", window["label"]))
            self.assertEqual(peer_inspect, ("session", "inspect", session["session_id"], "--json"))
        self.assertEqual(PlanRex.sessions, original_sessions)
        self.assertEqual(path.read_bytes(), before)

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
        # A reviewer runs in its throwaway checkout with the review tools.
        events = [dict(type="system", subtype="init", cwd=str(factory.reader_checkout_path(args.receipt)), session_id=session,
                       model=model, tools=list(native_claude.REVIEW_TOOLS), mcp_servers=[], permissionMode="dontAsk", slash_commands=[],
                       apiKeySource="none", claude_code_version="2.1.319", additional_directories=[]),
                  dict(type="assistant", session_id=session, parent_tool_use_id=None,
                       message=dict(model=model, content=[dict(type="tool_use", id="read-1", name="Read",
                                                               input=dict(file_path=str(Path(args.cwd) / "README.md")))])),
                  dict(type="user", session_id=session, parent_tool_use_id=None,
                       message=dict(content=[dict(type="tool_result", tool_use_id="read-1", content="Fixture readme")]),
                       tool_use_result=dict(type="text", file=dict(filePath=str(Path(args.cwd) / "README.md"), content="Fixture readme"))),
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
        for changes in (dict(effort="turbo"), dict(write=True, role="backend"), dict(harness="unknown")):
            with self.subTest(changes=changes), self.assertRaises(factory.FactoryError):
                self.claude_controller(self.plan([self.claude_job("first", **{"role": "qa", **changes})]))

    def grok_job(self, identity, **changes):
        return self.job(identity, **{"harness": "grok", **changes})

    def grok_controller(self, plan, **options):
        return runner.Runner(self.directory, plan, codex=sys.executable, grok=sys.executable, rex=sys.executable,
                             server="unix:///test.sock", probe=self.probe, clock=lambda: self.now, **options)

    def finish_grok(self, task, identity, outcome="approved", edit=False):
        import grok_worker
        from test_grok_worker import echo_cwd as grok_echo_cwd
        record = factory.load_task(self.directory / (task + ".json"))
        assignment = record["assignments"][identity]
        request = assignment["request"]
        replacements = {"{repository}": str(self.repo), "{cwd}": request["cwd"],
            "{brief}": assignment["brief"], "{brief_sha256}": request["instructions_sha256"],
            "{candidate}": request["inputs"]["candidate"], "{requirements}": request["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = grok_worker.parser().parse_args([replacements.get(part, part) for part in request["command"]][4:])
        events = [dict(type="tool_call", toolCallId="read-1", toolName="read_file", title="read_file",
                       rawInput=dict(target_file="README.md")),
                  dict(type="end", stopReason="end_turn", sessionId=args.native_session, requestId="fixture-request",
                       cwd=request["cwd"], num_turns=1, structuredOutput=dict(outcome=outcome, artifacts=["fixture/evidence.txt"]),
                       usage=dict(input_tokens=1, output_tokens=2), modelUsage=None)]
        async def spawn(*command, **options):
            if edit:
                (self.repo / "delivery.txt").write_text("Useful fixture delivery\n")
            return grok_echo_cwd(FakeProcess(events), options)
        async def configuration(binary, cwd):
            return dict(schema_version=2, kind="grok-subscription-configuration", cwd=request["cwd"],
                        grok_home=str(self.root / "grok-profile"), cli_version="fixture", layers=[],
                        observed_at=1000, probe_seconds=0.1, route="cached-session-without-routing-overrides",
                        sandbox="none", tools=["read_file"], no_executable_customizations=True)
        async def probe(binary, cwd, *, configuration):
            return dict(schema_version=1, kind="grok-native-metadata", route="grok-native", binary=str(binary), cwd=request["cwd"],
                        observed_at=1000, probe_seconds=0.25, configuration=await configuration(binary, cwd),
                        readiness="unverified", quota_observation=None, agent_version="fixture", capabilities={},
                        current_model="fixture-model",
                        authentication=dict(method="cached_token", auth_mode="Oidc", subscription_tier="Fixture", gate=None, backend_billed=False),
                        models=[dict(modelId="fixture-model", _meta=dict(reasoningEfforts=[dict(value="high")]))])
        environment = {key: value for key, value in os.environ.items() if key not in ("XAI_API_KEY", "GROK_CODE_XAI_API_KEY")}
        before = Path.cwd()
        try:
            os.chdir(self.repo)
            with patch.dict(os.environ, environment, clear=True), redirect_stdout(StringIO()):
                receipt = asyncio.run(grok_worker.execute(args, spawn=spawn, probe=probe, configuration=configuration,
                    clock=lambda: 1000.0, timer=lambda: 10.0, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        finally:
            os.chdir(before)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "exited"

    def test_grok_jobs_launch_collect_and_advance_without_codex_capacity(self):
        controller = self.grok_controller(self.plan([self.grok_job("first", role="qa"),
                                                     self.grok_job("second", role="qa", after=["first"])]))
        first = self.tick(controller)
        self.assertEqual(self.states(first), {("task", "first"): "running", ("task", "second"): "waiting"})
        command = factory.load_task(self.directory / "task.json")["assignments"]["first"]["request"]["command"]
        self.assertEqual(Path(command[3]).name, "grok_worker.py")
        self.finish_grok("task", "first")
        second = self.tick(controller)
        self.assertEqual(self.states(second), {("task", "first"): "approved", ("task", "second"): "running"})
        self.assertEqual(second["metrics"]["collected"], 1)
        self.assertEqual(self.probes, 0)

    def test_grok_writer_hands_its_commit_to_independent_verification(self):
        writer = self.grok_job("implement", write=True, commit_message="feat(fixture): Deliver candidate")
        controller = self.grok_controller(self.plan([writer]))
        self.assertEqual(self.tick(controller)["metrics"]["launched"], 1)
        with patch.dict(os.environ, GIT_AUTHOR_DATE="2000-01-02T00:00:00Z", GIT_COMMITTER_DATE="2000-01-02T00:00:00Z"):
            self.finish_grok("task", "implement", outcome="completed", edit=True)
        self.assertEqual(self.tick(controller)["metrics"]["collected"], 1)
        record = factory.load_task(self.directory / "task.json")
        target = record["assignments"]["implement"]["result"]["candidate"]
        self.assertNotEqual(target, self.candidate)
        self.assertEqual(self.handoff()["metrics"]["launched"], 1)
        self.assertEqual(factory.load_task(self.directory / "task.json")["inputs"],
                         dict(candidate=target, requirements="requirements-1"))

    def finish_antigravity(self, task, identity, outcome="approved"):
        import antigravity_worker
        from test_antigravity_worker import echo_cwd, stream, usage_probe
        record = factory.load_task(self.directory / (task + ".json"))
        assignment = record["assignments"][identity]
        request = assignment["request"]
        replacements = {"{repository}": str(self.repo), "{cwd}": request["cwd"],
            "{brief}": assignment["brief"], "{brief_sha256}": request["instructions_sha256"],
            "{candidate}": request["inputs"]["candidate"], "{requirements}": request["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = antigravity_worker.parser().parse_args([replacements.get(part, part) for part in request["command"]][4:])
        async def spawn(*command, **options):
            return FakeProcess(echo_cwd(stream(request["cwd"], outcome=outcome, model=args.model), options))
        before = Path.cwd()
        try:
            os.chdir(self.repo)
            with redirect_stdout(StringIO()):
                receipt = asyncio.run(antigravity_worker.execute(args, spawn=spawn, probe=usage_probe,
                    clock=lambda: 1000.0, timer=lambda: 10.0))
        finally:
            os.chdir(before)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        PlanRex.processes[(record["session_id"], assignment["block_id"])] = "exited"

    def test_antigravity_jobs_launch_collect_and_advance_without_codex_capacity(self):
        job = dict(harness="antigravity", model="gemini-3.8-flash-high", effort="high")
        controller = runner.Runner(self.directory, self.plan([self.job("first", **job), self.job("second", after=["first"], **job)]),
            codex=sys.executable, antigravity=sys.executable, rex=sys.executable, server="unix:///test.sock",
            probe=self.probe, clock=lambda: self.now)
        self.assertEqual(self.states(self.tick(controller)), {("task", "first"): "running", ("task", "second"): "waiting"})
        command = factory.load_task(self.directory / "task.json")["assignments"]["first"]["request"]["command"]
        self.assertEqual(Path(command[3]).name, "antigravity_worker.py")
        self.finish_antigravity("task", "first")
        self.assertEqual(self.states(self.tick(controller)), {("task", "first"): "approved", ("task", "second"): "running"})
        self.assertEqual(self.probes, 0)
        with self.assertRaisesRegex(factory.FactoryError, "--antigravity executable"):
            self.controller(self.plan([self.job("first", **job)]))
        with self.assertRaisesRegex(factory.FactoryError, "supported effort"):
            self.controller(self.plan([self.job("first", **{**job, "effort": "turbo"})]))

    def test_antigravity_jobs_without_effort_launch_and_collect_beside_gemini(self):
        # Observed (#50): agy refuses --effort for claude-opus-4-6-thinking, so its plan job sets effort null.
        opus = self.job("opus", harness="antigravity", model="claude-opus-4-6-thinking", effort=None)
        gemini = self.job("gemini", harness="antigravity", model="gemini-3.8-flash-high", effort="high")
        controller = runner.Runner(self.directory, self.plan([opus, gemini]),
            codex=sys.executable, antigravity=sys.executable, rex=sys.executable, server="unix:///test.sock",
            probe=self.probe, clock=lambda: self.now)
        self.assertEqual(self.tick(controller)["metrics"]["launched"], 2)
        assignments = factory.load_task(self.directory / "task.json")["assignments"]
        self.assertNotIn("--effort", assignments["opus"]["request"]["command"])
        command = assignments["gemini"]["request"]["command"]
        self.assertEqual(command[command.index("--effort") + 1], "high")
        self.finish_antigravity("task", "opus")
        self.assertEqual(self.states(self.tick(controller))[("task", "opus")], "approved")
        for job in (*(self.job("first", harness="antigravity", model="claude-opus-4-6-thinking", effort=effort)
                      for effort in ("", "turbo")),
                    self.claude_job("first", role="qa", effort=None), self.job("first", effort=None)):
            with self.subTest(job=job), self.assertRaises(factory.FactoryError):
                runner.Runner(self.directory, self.plan([job]), codex=sys.executable, claude=sys.executable,
                              antigravity=sys.executable, rex=sys.executable, server="unix:///test.sock",
                              probe=self.probe, clock=lambda: self.now)

    def test_grok_jobs_need_their_binary_and_writer_commit_message(self):
        with self.assertRaisesRegex(factory.FactoryError, "--grok executable"):
            self.controller(self.plan([self.grok_job("first", role="qa")]))
        with self.assertRaisesRegex(factory.FactoryError, "Grok and Antigravity writers need a commit message"):
            self.grok_controller(self.plan([self.grok_job("first", write=True)]))

    def every_harness(self, plan):
        return runner.Runner(self.directory, plan, codex=sys.executable, claude=sys.executable, grok=sys.executable,
                             antigravity=sys.executable, rex=sys.executable, server="unix:///test.sock",
                             probe=self.probe, clock=lambda: self.now)

    def test_long_claude_writers_freeze_their_planned_turns(self):
        # Observed (#47): a real Claude writer needed 100 turns; plans used to freeze the default 30.
        writer = self.claude_job("implement", write=True, commit_message="feat(fixture): Deliver candidate", turns=200)
        self.assertEqual(self.tick(self.claude_controller(self.plan([writer])))["metrics"]["launched"], 1)
        command = factory.load_task(self.directory / "task.json")["assignments"]["implement"]["request"]["command"]
        self.assertEqual(command[command.index("--turns") + 1], "200")
        self.assertNotIn("--base", command)

    def test_planned_code_reviews_freeze_their_base_and_omitted_turns_keep_the_default(self):
        agy = dict(harness="antigravity", model="gemini-3.8-flash-high")
        plan = self.plan([self.grok_job("review", role="code-review", base=self.candidate, turns=60),
                          self.job("second-review", role="code-review", base=self.candidate, **agy),
                          self.claude_job("qa", role="qa")], workers=3, reserve=0)
        self.assertEqual(self.tick(self.every_harness(plan))["metrics"]["launched"], 3)
        assignments = factory.load_task(self.directory / "task.json")["assignments"]
        grok, second, claude = (assignments[key]["request"]["command"] for key in ("review", "second-review", "qa"))
        self.assertEqual((grok[grok.index("--base") + 1], grok[grok.index("--turns") + 1]), (self.candidate, "60"))
        self.assertEqual(second[second.index("--base") + 1], self.candidate)
        self.assertNotIn("--turns", second)
        self.assertEqual(claude[claude.index("--turns") + 1], "30")
        self.assertNotIn("--base", claude)

    def test_invalid_turns_or_base_refuse_before_any_external_call(self):
        agy = dict(harness="antigravity", model="gemini-3.8-flash-high")
        for job in (self.job("first", turns=200), self.job("first", turns=200, **agy),
                    *(self.claude_job("first", role="qa", turns=value) for value in (0, 1001, True, "200", None)),
                    self.job("first", role="code-review", base=self.candidate),
                    self.claude_job("first", role="code-review"), self.grok_job("first", role="code-review", base="HEAD"),
                    self.job("first", role="code-review", base=None, **agy),
                    self.claude_job("first", write=True, commit_message="feat(fixture): Deliver candidate", base=self.candidate)):
            with self.subTest(job=job), self.assertRaises(factory.FactoryError):
                self.every_harness(self.plan([job]))
        self.assertEqual(PlanRex.calls, [])
        self.assertEqual(self.probes, 0)

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

    def test_outer_codex_timeout_reports_recovery_and_clears_stale_quota(self):
        controller = self.controller(self.plan([self.job()]))
        controller.refresh_capacity()
        self.now += runner.CAPACITY_MAX_AGE
        cancelled = []

        async def stalled(binary):
            try:
                await asyncio.Future()
            finally:
                cancelled.append(binary)

        controller.probe = stalled
        with patch.object(runner, "PROBE_TIMEOUT", 0.01):
            report = controller.harnesses()["codex"]
        self.assertEqual(cancelled, [controller.binary])
        self.assertEqual(report, dict(available=False, windows=[],
            reason="Timed out waiting for native Codex quota and model metadata. "
                   "Check the Codex connection and sign-in before refreshing."))
        result = self.tick(controller)
        self.assertEqual(result["jobs"][0]["state"], "waiting")
        self.assertEqual(result["jobs"][0]["reason"], report["reason"])
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

    def test_job_field_refusal_names_what_is_missing_or_unexpected(self):
        # The canary dropped commit_message from read-only jobs and got no hint which field was wrong.
        value = self.plan([self.job()])
        del value["jobs"][0]["commit_message"]
        value["jobs"][0]["model_name"] = "typo"
        with self.assertRaisesRegex(factory.FactoryError, "missing commit_message; unexpected model_name.*commit_message to null"):
            self.controller(value)
        value = self.plan([self.job()])
        value["jobs"][0]["harness"] = "gemini"
        with self.assertRaisesRegex(factory.FactoryError, "^Job task/first: harness must be one of"):
            self.controller(value)

    def test_any_job_refusal_names_the_job_and_a_long_subject_its_length(self):
        # In a seven-job plan the canary had to hunt for the two commit subjects over 72 characters.
        subject = "fix(telegram): " + "x" * 67
        value = self.plan([self.job(), self.job("second", task="other", write=True, commit_message=subject)])
        with self.assertRaisesRegex(factory.FactoryError,
                                    r"^Job other/second: .*at most 72 characters \(this subject has 82\)$"):
            self.controller(value)
        value = self.plan([self.job(), self.job("second", task="other", seconds=0)])
        with self.assertRaisesRegex(factory.FactoryError, "^Job other/second: seconds 0 must be a whole number from 1 to 86400$"):
            self.controller(value)

    def test_a_job_refusal_names_the_one_field_that_is_wrong(self):
        # The canary got a seven-field list for one bad purpose and checked each field by hand (#126).
        cases = (
            (dict(purpose="work", role="investigator"),
             r"purpose 'work' is not valid for role investigator \(expected \"investigation\"\)$"),
            (dict(purpose="investigation"), r"purpose \"investigation\" goes only with role investigator, not '\S+'$"),
            (dict(purpose="fix"), r"purpose 'fix' must be work, correction or investigation$"),
            (dict(role="intern"), r"role 'intern' is not in roles.json$"),
            (dict(cwd="relative/dir"), r"cwd 'relative/dir' must be an absolute path$"),
            (dict(candidate="abc123"), r"candidate 'abc123' must be a full lowercase commit id$"),
            (dict(write="yes"), r"write 'yes' must be true or false$"),
            (dict(id="Second"), r"id 'Second' must be a lowercase slug"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                job = {**self.job("second"), **changes}
                with self.assertRaisesRegex(factory.FactoryError, "^Job task/(second|Second): " + message):
                    self.controller(self.plan([job]))
        with self.assertRaisesRegex(factory.FactoryError, "^Job task/first: this task and id appear earlier in the plan"):
            self.controller(self.plan([self.job(), self.job()]))

    def test_a_failed_job_names_its_next_step(self):
        # Eight nanoclaw tasks idled for hours: failed assignments read as launched with no result.
        plan = self.plan([self.job()], workers=1, reserve=0)
        self.tick(self.controller(plan))
        record = factory.load_task(self.directory / "task.json")
        PlanRex.processes[(record["session_id"], record["assignments"]["first"]["block_id"])] = "failed"
        result = self.tick(self.controller(plan))
        self.assertEqual(result["result"], "needs_action")
        self.assertEqual(result["jobs"], [dict(task="task", id="first", state="failed", reason=runner.FAILED_NEXT)])
        self.assertIn("new ID", runner.FAILED_NEXT)
        self.assertEqual(factory.load_task(self.directory / "task.json")["assignments"]["first"]["closed"]["state"], "failed")

    def test_a_briefly_held_task_lock_does_not_idle_the_line(self):
        # A coordinator polling show/status every 10 s collided with the scan and two cycles launched nothing (#203).
        held, release = threading.Event(), threading.Event()
        def reader():
            with factory.task_lock(self.directory, "task"):
                held.set()
                release.wait(5)
        holder = threading.Thread(target=reader)
        holder.start()
        held.wait(5)
        threading.Timer(0.3, release.set).start()
        result = self.tick(self.controller(self.plan([self.job()], workers=1, reserve=0)))
        holder.join()
        self.assertEqual((result["metrics"]["launched"], result["issues"]), (1, []))
        # A lock that stays busy past the patience is still reported.
        with factory.task_lock(self.directory, "task"), patch.object(runner, "LOCK_PATIENCE", 0.2):
            result = self.tick(self.controller(self.plan([self.job("second")], workers=2, reserve=0)))
        self.assertEqual([issue["task"] for issue in result["issues"]], ["task"])
        self.assertEqual(result["metrics"]["launched"], 0)

    def test_a_runner_serves_its_own_line_only(self):
        # Two coordinators share one state directory; one line's agents and broken records must not stall the other.
        self.admit("other")
        factory.move_line(self.directory / "other.json", "acr")
        acr = self.controller(self.plan([self.job("busy", task="other")], workers=1, reserve=0))
        self.assertEqual(acr.line, "acr")
        self.assertEqual(self.tick(acr)["metrics"]["launched"], 1)
        (self.directory / "broken.json").write_text("{not json")
        mine = self.controller(self.plan([self.job()], workers=1, reserve=0))
        self.assertEqual(mine.line, "default")
        with factory.execution_lock(self.directory, "acr"):  # The other line holding its lock doesn't stop this one.
            result = self.tick(mine)
        self.assertEqual(result["metrics"]["launched"], 1)
        self.assertEqual([(issue["task"], issue["line"]) for issue in result["issues"]], [("broken", None)])
        self.assertNotIn(("other", "busy"), {(row["task"], row["id"]) for row in result["jobs"]})
        with factory.execution_lock(self.directory):
            with self.assertRaisesRegex(factory.FactoryError, "owns execution"):
                self.tick(self.controller(self.plan([self.job("second")])))
        with self.assertRaisesRegex(factory.FactoryError, "mixes acr, default"):
            self.controller(self.plan([self.job("third"), self.job("fourth", task="other")]))

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
            # A busy lock is a retry, not a failure: a rotation dropped 26 plans that treated it as one.
            self.assertEqual(code, factory.EXIT_BUSY)
            self.assertIn("controller", error.getvalue())
            plan_file = self.root / "busy-plan.json"
            plan_file.write_text(json.dumps(self.plan([self.job()])))
            error = StringIO()
            with redirect_stderr(error), redirect_stdout(StringIO()):
                code = runner.main(["--state-dir", str(self.directory), "--plan", str(plan_file), "--codex", sys.executable,
                                    "--rex", sys.executable, "--server", "unix:///test.sock", "--once"])
            self.assertEqual(code, factory.EXIT_BUSY)
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

    def test_the_line_lock_is_free_while_polling_and_a_held_tick_is_skipped(self):
        # The nanoclaw line ran about ten plans on one line; each slice held the lock through its polls, and admit,
        # inputs and provision waited 5+ minutes. The lock covers each tick only.
        controller = self.controller(self.plan([self.job(), self.job("second", after=["first"])]))
        now, free, other = [0.0], [], contextlib.ExitStack()
        def sleep(seconds):
            now[0] += seconds
            free.append(None)
            if len(free) == 1:
                with factory.try_execution_lock(self.directory) as held:
                    free[0] = held
                self.finish("task", "first")
                other.enter_context(factory.execution_lock(self.directory))  # Another controller's tick.
            elif len(free) == 2:
                # The runner skipped the tick the other controller held, and launched nothing.
                self.assertEqual(list(factory.load_task(self.directory / "task.json")["assignments"]), ["first"])
                other.close()
            else:
                self.finish("task", "second")
        result = runner.run(controller, seconds=60, timer=lambda: now[0], sleep=sleep)
        self.assertEqual(free[0], True)
        self.assertEqual(result["result"], "complete")
        self.assertEqual(result["metrics"]["launched"], 2)

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
