"""Antigravity execution/collection uses fake native processes and fake Rex only."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
from contextlib import nullcontext, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any
import unittest
from unittest.mock import patch

import antigravity_worker
import factory
import integration
import native_antigravity
from codex_worker import outcome_schema
from test_codex_worker import FakeProcess
from test_rex_launcher import FakeRex

CONVERSATION = "ec43bd74-84de-47ea-abd3-b6ab8e7b5c8e"


def stream(cwd, *, model="gemini-3.8-flash-high", outcome="approved", conversation=CONVERSATION, tool=True):
    """Observed 1.5.3 stream-json shapes, including a bypass-mode tool step."""
    def step(index, state, category, **extra):
        return dict(event="step_update", step_update=dict(conversation_id=conversation, step_index=index,
                                                          state=state, step_type=category, **extra))
    steps = [step(0, "DONE", "user_input"), step(1, "DONE", "agent_response", usage=dict(input_tokens=7, output_tokens=2))]
    if tool:
        steps += [step(2, "ACTIVE", "tool", tool_name="run_command",
                       tool_info=dict(name="run_command", parameters=dict(CommandLine="python3 -B -m unittest"))),
                  step(2, "DONE", "tool", tool_name="run_command", duration_seconds=0.1)]
    steps.append(step(3 if tool else 2, "DONE", "agent_response"))
    steps.append(step(4 if tool else 3, "DONE", "finish"))
    report = dict(outcome=outcome, artifacts=["fixture evidence"])
    return ([dict(event="init", conversation_id=conversation,
                  init=dict(model=model, cwd=str(cwd), tools=["read_file", "run_command"],
                            permission_mode="always-proceed", json_schema=outcome_schema()))]
            + steps
            + [dict(event="result", result=dict(conversation_id=conversation, status="SUCCESS",
                    response=json.dumps(report), duration_seconds=3.5, num_turns=1, structured_output=report,
                    json_schema=outcome_schema(), usage=dict(input_tokens=9, output_tokens=4)))])


def echo_cwd(events, options):
    """agy reports the directory it runs in; readers now run in a disposable checkout."""
    events = json.loads(json.dumps(events))
    if events and "init" in events[0] and options.get("cwd", ".") != ".":
        events[0]["init"]["cwd"] = options["cwd"]
    return events


def usage_probe(binary, cwd):
    return dict(schema_version=1, kind="antigravity-native-usage", binary=str(Path(binary).resolve()), cwd=str(cwd),
                cli_version="1.5.3", observed_at=1000, probe_seconds=0.2,
                groups=[dict(group="Gemini Models", window="weekly", percent_remaining=99.0, resets_at="2026-11-14T09:20:42Z")],
                native_output="", readiness="unverified", note="fixture")


class AntigravityWorker(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        environment = {key: value for key, value in os.environ.items() if key not in native_antigravity.API_ENVIRONMENT}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                           GIT_AUTHOR_DATE="2000-01-01T00:00:00Z", GIT_COMMITTER_DATE="2000-01-01T00:00:00Z")
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        self.base, self.repo = self.root / "base", self.root / "assigned"
        self.git("init", "-q", "--initial-branch=main", str(self.base), directory=self.root)
        for key, value in (("user.name", "Fixture"), ("user.email", "fixture@example.invalid"),
                           ("core.hooksPath", os.devnull), ("commit.gpgsign", "false")):
            self.git("config", key, value)
        (self.base / "README.md").write_text("Owned fixture\n")
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture")
        self.candidate = self.git("rev-parse", "HEAD").stdout.strip()
        self.git("worktree", "add", "--quiet", "--detach", str(self.repo), self.candidate)
        self.path = self.root / "task.json"
        factory.admit(self.path, task="task", goal="Inspect fixture", repository=str(self.base), acceptance=["README.md"])
        factory.set_inputs(self.path, candidate=self.candidate, requirements="requirements-1")
        FakeRex.sessions, FakeRex.calls = [], []
        FakeRex.fail_after_create, FakeRex.host = False, "test-host"
        self.args = self.launch("inspect", role="qa")
        previous = Path.cwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        self.spawns: list[tuple[tuple, dict]] = []
        self.rex = FakeRex(sys.executable, "unix:///test.sock")
        self.rex.instance_id = FakeRex.instance

    def git(self, *command, directory=None):
        result = subprocess.run([factory.git_binary(), "-C", str(directory or self.base), *command],
                                capture_output=True, text=True, env=os.environ)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def make_command(self, identity, **changes):
        return factory.antigravity_command(self.path, **{**dict(task="task", assignment_id=identity, agy=sys.executable,
            model="gemini-3.8-flash-high", effort="high", cwd=str(self.repo), role="qa", seconds=30), **changes})

    def launch(self, identity, **changes):
        command = self.make_command(identity, **changes)
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id=identity, role=changes.get("role", "qa"),
                goal="Fixture " + identity, cwd=str(self.repo), command=command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        return antigravity_worker.parser().parse_args(argv[4:])

    def execute(self, args, *, events=None, edit=False, exit_code=0, act=None):
        async def spawn(*command, **options):
            options["prompt"] = options["stdin"].read().decode()
            options["stdin"].seek(0)
            self.spawns.append((command, options))
            if edit:
                (self.repo / "NOTES.md").write_text("Notes\n")
            if act is not None:
                act(Path(options["cwd"]))
            return FakeProcess(echo_cwd(events if events is not None else stream(self.repo), options), exit_code=exit_code)
        with redirect_stdout(StringIO()):
            return asyncio.run(antigravity_worker.execute(args, spawn=spawn, probe=usage_probe, clock=lambda: 1000,
                timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))

    def collect(self, identity):
        record = factory.load_task(self.path)
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            return factory.collect_antigravity(self.path, identity, self.rex, record, record["assignments"][identity])

    def test_reviewer_runs_unprompted_with_the_brief_on_stdin_and_collects(self):
        receipt = self.execute(self.args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["conversation_id"], receipt["candidate"]),
                         (3, CONVERSATION, self.candidate))
        command, options = self.spawns[0]
        checkout = factory.reader_checkout_path(self.args.receipt)
        self.assertEqual((options["cwd"], receipt["checkout"]), (str(checkout), str(checkout)))
        self.assertFalse(checkout.exists(), "the disposable reader checkout outlived its run")
        self.assertIn("--dangerously-skip-permissions", command)
        self.assertEqual(command[command.index("--input-format") + 1], "stream-json")
        self.assertEqual((command[command.index("--model") + 1], command[command.index("--effort") + 1]),
                         ("gemini-3.8-flash-high", "high"))
        message = json.loads(options["prompt"])
        self.assertEqual((message["event"], message["message"]["role"]), ("user", "user"))
        self.assertTrue(message["message"]["content"][0]["text"].startswith("# Assignment: qa"))
        self.assertNotIn("Factory commit mode", message["message"]["content"][0]["text"])
        result = self.collect("inspect")["assignments"]["inspect"]
        self.assertEqual((result["result"]["outcome"], result["native_collected"]), ("approved", True))
        self.assertEqual(antigravity_worker.read_events(self.args.events),
                         [json.dumps(e) + "\n" for e in echo_cwd(stream(self.repo), dict(cwd=str(checkout)))])

    def test_writer_edits_are_committed_by_the_factory_and_collected(self):
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["writer"], receipt["commit"]["state"]), (2, True, "created"))
        self.assertEqual(self.git("show", receipt["candidate"] + ":NOTES.md").stdout, "Notes\n")
        self.assertIn("do not stage or commit", json.loads(self.spawns[-1][1]["prompt"])["message"]["content"][0]["text"])
        result = self.collect("implement")["assignments"]["implement"]["result"]
        self.assertEqual((result["outcome"], result["candidate"]), ("completed", receipt["candidate"]))

    def test_failed_process_or_foreign_stream_cannot_supply_a_result(self):
        self.assertEqual(self.execute(self.args, exit_code=3)["state"], "failed")
        args = self.launch("other", role="qa")
        receipt = self.execute(args, events=stream(self.repo, model="claude-opus-4-6-thinking"))
        self.assertEqual(receipt["state"], "failed")
        self.assertIn("model/cwd/schema", receipt["error"])
        with self.assertRaises(factory.FactoryError):
            self.collect("other")

    def test_command_validation_refuses_before_any_effect(self):
        for changes, message in ((dict(effort="turbo"), "supported effort"),
                                 (dict(role="qa", write=True, commit_message="x", sandbox=sys.executable), "does not permit writer"),
                                 (dict(role="backend", write=True, sandbox=sys.executable), "--commit-message"),
                                 (dict(role="backend", write=True, commit_message="x", sandbox=sys.executable,
                                       base=self.candidate), "--base is for code review")):
            with self.subTest(changes=changes), self.assertRaisesRegex(factory.FactoryError, message):
                self.make_command("refused", **changes)

    def test_models_that_refuse_effort_run_without_the_flag_and_collect(self):
        # Observed (#50): agy refuses --effort for claude-opus-4-6-thinking; Gemini models accept it.
        args = self.launch("opus", model="claude-opus-4-6-thinking", effort=None)
        self.assertIsNone(args.effort)
        receipt = self.execute(args, events=stream(self.repo, model="claude-opus-4-6-thinking"))
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertIsNone(receipt["requested_effort"])
        command, _ = self.spawns[-1]
        self.assertNotIn("--effort", command)
        self.assertEqual(command[command.index("--model") + 1], "claude-opus-4-6-thinking")
        result = self.collect("opus")["assignments"]["opus"]
        self.assertEqual((result["result"]["outcome"], result["native_collected"]), ("approved", True))
        self.assertNotIn("--effort", result["request"]["command"])
        gemini = self.make_command("gemini")
        self.assertEqual(gemini[gemini.index("--effort") + 1], "high")
        import dashboard
        record = factory.load_task(self.path)
        self.assertEqual(dashboard.requested_native(record, "opus", record["assignments"]["opus"])["effort"],
                         "None (model default)")

    def test_readers_work_in_disposable_checkouts_so_a_shared_checkout_stays_collectable(self):
        # Observed (#53): an unconfined Gemini QA reader ran npm ci and pytest in a checkout it
        # shared with a Grok review, and the leftovers made the review uncollectable.
        def install_and_test(cwd):
            self.assertNotEqual(cwd, self.repo)
            (cwd / "node_modules" / "left-pad").mkdir(parents=True)
            (cwd / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 0\n")
            (cwd / ".pytest_cache").mkdir()
            (cwd / "README.md").unlink()
        first = self.execute(self.args, act=install_and_test)
        self.assertEqual(first["state"], "completed", first["error"])
        factory.check_checkout(str(self.base), str(self.repo), self.candidate)
        self.assertEqual((self.repo / "README.md").read_text(), "Owned fixture\n")
        second_args = self.launch("second", role="code-review", base=self.candidate)
        second = self.execute(second_args)
        self.assertEqual(second["state"], "completed", second["error"])
        for identity, args in (("inspect", self.args), ("second", second_args)):
            with self.subTest(identity=identity):
                result = self.collect(identity)["assignments"][identity]
                self.assertEqual((result["result"]["outcome"], result["native_collected"]), ("approved", True))
                self.assertFalse(factory.reader_checkout_path(args.receipt).exists())
        worktrees = self.git("worktree", "list", "--porcelain").stdout
        self.assertNotIn(".checkout", worktrees)

    def test_reader_receipts_bind_their_disposable_checkout(self):
        self.assertEqual(self.execute(self.args)["state"], "completed")
        receipt = json.loads(self.args.receipt.read_text())
        for damaged in ({**receipt, "checkout": str(self.repo)}, {k: v for k, v in receipt.items() if k != "checkout"},
                        {**receipt, "schema_version": 1}):
            self.args.receipt.write_text(json.dumps(damaged))
            with self.subTest(damaged={k: damaged.get(k) for k in ("schema_version", "checkout")}), \
                    self.assertRaisesRegex(factory.FactoryError, "differs from its frozen"):
                self.collect("inspect")

    def test_reset_keeps_a_failed_writers_edits_as_a_patch_and_restores_the_checkout(self):
        # A failed writer that edited its checkout left the task's worktree dirty, and every
        # later assignment refused it; reset keeps the work for a new writer to cite.
        self.assertEqual(self.execute(self.args)["state"], "completed")
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        failed = self.execute(args, edit=True, exit_code=3)
        self.assertEqual(failed["state"], "failed")
        (self.repo / "scratch.txt").write_text("untracked work\n")
        with self.assertRaisesRegex(factory.FactoryError, "uncommitted inputs"):
            factory.check_checkout(str(self.base), str(self.repo), self.candidate)
        for identity, message in (("inspect", "Only a native writer"), ("missing", "existing writer")):
            with self.subTest(identity=identity), self.assertRaisesRegex(factory.FactoryError, message):
                factory.reset_writer(self.path, assignment_id=identity)
        result = factory.reset_writer(self.path, assignment_id="implement")
        patch = Path(result["patch"])
        self.assertEqual((result["checkout"], result["candidate"]), (str(self.repo), self.candidate))
        self.assertIn("new writer ID", result["next"])
        self.assertEqual(patch.stat().st_mode & 0o777, 0o600)
        self.assertIn("+Notes", patch.read_text())
        self.assertIn("+untracked work", patch.read_text())
        factory.check_checkout(str(self.base), str(self.repo), self.candidate)
        self.assertEqual(self.git("-C", str(self.repo), "apply", "--check", str(patch), directory=self.repo).returncode, 0)
        with self.assertRaisesRegex(factory.FactoryError, "already reset"):
            factory.reset_writer(self.path, assignment_id="implement")

    def test_reset_keeps_a_failed_writers_own_factory_commit(self):
        # Observed (#68): writer-4 committed through the factory, then failed afterwards;
        # reset refused because HEAD had moved to that commit.
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        committed = receipt["candidate"]
        args.receipt.write_text(json.dumps({**receipt, "state": "failed", "error": "post-commit check failed"}))
        (self.repo / "groups" / "fresh").mkdir(parents=True)
        (self.repo / "leftover.txt").write_text("leftover\n")
        result = factory.reset_writer(self.path, assignment_id="implement")
        self.assertEqual(result["candidate"], committed)
        self.assertIn("select it with factory.py inputs", result["next"])
        self.assertIn("+leftover", Path(result["patch"]).read_text())
        factory.check_checkout(str(self.base), str(self.repo), committed)
        self.assertEqual(self.git("-C", str(self.repo), "show", "HEAD:NOTES.md", directory=self.repo).stdout, "Notes\n")

    def test_reset_refuses_a_writer_whose_receipt_did_not_fail(self):
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with self.assertRaisesRegex(factory.FactoryError, "receipt failed"):
            factory.reset_writer(self.path, assignment_id="implement")

    def test_api_overrides_refuse_before_native_artifacts(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}), self.assertRaisesRegex(factory.FactoryError, "Gemini API"):
            self.execute(self.args)
        self.assertFalse(self.args.receipt.exists())

    def test_writer_and_reviewer_receipts_never_substitute_for_each_other(self):
        self.assertEqual(self.execute(self.args)["state"], "completed")
        receipt = json.loads(self.args.receipt.read_text())
        self.args.receipt.write_text(json.dumps({**receipt, "schema_version": 2, "writer": True}))
        with self.assertRaisesRegex(factory.FactoryError, "differs from its frozen"):
            self.collect("inspect")

    def test_dashboard_reads_the_requested_native_route(self):
        import dashboard
        record = factory.load_task(self.path)
        requested = dashboard.requested_native(record, "inspect", record["assignments"]["inspect"])
        self.assertEqual(requested, dict(adapter="antigravity_worker", harness="antigravity", route="antigravity-native",
                                         model="gemini-3.8-flash-high", effort="high"))


class AntigravityApprovals(unittest.TestCase):
    """Antigravity reports count toward integration under their native conversation identity."""

    def setUp(self):
        environment = {key: value for key, value in os.environ.items() if key not in native_antigravity.API_ENVIRONMENT}
        cleanup = patch.dict(os.environ, environment, clear=True)
        cleanup.start()
        self.addCleanup(cleanup.stop)
        from test_integration import Integration
        self.fixture = Integration()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        record, assignment, receipt = self.run_agy("agy-qa", "qa", "11111111-1111-4111-8111-111111111111")
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            self.record = factory.collect_antigravity(self.fixture.path, "agy-qa", None, record, assignment)

    def run_agy(self, identity, role, conversation, *, write=False, exit_code=0):
        f = self.fixture
        writer: dict[str, Any] = dict(write=True, commit_message="Add fixture notes", sandbox=sys.executable) if write else {}
        command = factory.antigravity_command(f.path, task="deliver", assignment_id=identity, agy=sys.executable,
            model="gemini-3.8-flash-high", effort="high", cwd=str(f.repo), role=role, **writer)
        record, assignment = factory.launch(f.path, task="deliver", assignment_id=identity, role=role,
            goal="Fixture " + role, cwd=str(f.repo), command=command, purpose="work",
            binary=sys.executable, server="unix:///fixture.sock")
        replacements = {"{repository}": str(f.repo), "{cwd}": str(f.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": record["inputs"]["candidate"],
            "{requirements}": record["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = antigravity_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        events = stream(f.repo, outcome="completed" if write else "approved", conversation=conversation)
        async def spawn(*command, **options):
            return FakeProcess([] if exit_code else echo_cwd(events, options), exit_code=exit_code)
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(antigravity_worker.execute(args, spawn=spawn, probe=usage_probe, clock=lambda: 1000,
                timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        return record, assignment, receipt

    def test_collected_antigravity_approval_counts_beside_the_codex_release(self):
        selected = integration.approvals(self.record, "release", ["agy-qa"])
        self.assertEqual(selected["agy-qa"]["context"], "11111111-1111-4111-8111-111111111111")

    def test_writer_contributes_and_a_failed_unstarted_writer_still_counts(self):
        record, assignment, receipt = self.run_agy("agy-writer", "backend", "22222222-2222-4222-8222-222222222222", write=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_antigravity(self.fixture.path, "agy-writer", None, record, assignment)
            self.assertEqual(integration.contributor_context("agy-writer", record, record["assignments"]["agy-writer"]),
                             "22222222-2222-4222-8222-222222222222")
            self.assertIn("agy-qa", integration.approvals(record, "release", ["agy-qa"]))
        record, assignment, receipt = self.run_agy("agy-failed", "backend", "unused", write=True, exit_code=3)
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(integration.contributor_context("agy-failed", record, assignment),
                         "antigravity-unstarted:deliver/agy-failed")

    def test_antigravity_cannot_be_the_release_judgment(self):
        with self.assertRaises(factory.FactoryError):
            integration.approvals(self.record, "agy-qa", ["qa"])


if __name__ == "__main__":
    unittest.main()
