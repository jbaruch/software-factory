"""Antigravity execution/collection uses fake native processes and fake Rex only."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import asyncio
from contextlib import nullcontext, redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, cast
import unittest
from unittest.mock import patch

import antigravity_worker
import factory
import factory_commit
import integration
import native_antigravity
from codex_worker import outcome_schema
from test_abandoned_scratch import listing_cli
from test_codex_worker import FakeProcess, check_reader_stop_timeout
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
        self.receipt_at_spawn: dict = {}
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

    def launch(self, identity, *, goal=None, **changes):
        command = self.make_command(identity, **changes)
        with patch.object(factory, "Rex", FakeRex):
            factory.launch(self.path, task="task", assignment_id=identity, role=changes.get("role", "qa"),
                goal=goal or "Fixture " + identity, cwd=str(self.repo), command=command, purpose="work",
                binary=sys.executable, server="unix:///test.sock")
        actual = [call for call in FakeRex.calls if call[0] == "new" or call[:2] == ("window", "new")][-1]
        argv = actual[actual.index("--") + 1:]
        argv = argv[argv.index("--") + 1:]
        return antigravity_worker.parser().parse_args(argv[4:])

    def execute(self, args, *, events=None, edit=False, exit_code=0, act=None):
        async def spawn(*command, **options):
            self.receipt_at_spawn = json.loads(args.receipt.read_text())
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

    def test_reader_stop_timeout_retains_directories(self):
        check_reader_stop_timeout(self, lambda: self.execute(self.args))

    def test_reviewer_runs_unprompted_with_the_brief_on_stdin_and_collects(self):
        with patch.object(factory, "native_start_time", return_value=123456):
            receipt = self.execute(self.args)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["conversation_id"], receipt["candidate"]),
                         (8, CONVERSATION, self.candidate))
        command, options = self.spawns[0]
        self.assertEqual(self.receipt_at_spawn["scratch"], options["env"]["TMPDIR"])
        self.assertIsNone(self.receipt_at_spawn["native_process"])
        self.assertEqual(receipt["native_process"], dict(pid=FakeProcess.pid, start_time=123456, scope=factory.process_scope()))
        self.assertIs(receipt["scratch_removed"], True)
        self.assertFalse(Path(receipt["scratch"]).exists())
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

    def test_export_checks_original_brief_bytes(self):
        from test_writer_transfer import check_provider_export_brief
        factory.set_inputs(self.path, candidate=self.candidate, requirements=self.candidate)
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.collect("implement")
        check_provider_export_brief(self, args, b"substituted fixture payload")

    def test_writer_edits_are_committed_by_the_factory_and_collected(self):
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual((receipt["schema_version"], receipt["writer"], receipt["commit"]["state"]), (8, True, "created"))
        self.assertEqual(self.git("show", receipt["candidate"] + ":NOTES.md").stdout, "Notes\n")
        self.assertIn("do not stage or commit", json.loads(self.spawns[-1][1]["prompt"])["message"]["content"][0]["text"])
        result = self.collect("implement")["assignments"]["implement"]["result"]
        self.assertEqual((result["outcome"], result["candidate"]), ("completed", receipt["candidate"]))
        old = {key: value for key, value in receipt.items() if key not in factory.SCRATCH_RECEIPT_FIELDS | {"interpreter", "retained_files", "conversation_root"}}
        factory.save(args.receipt, {**old, "schema_version": 2})
        legacy = args.receipt.read_bytes()
        record = self.collect("implement")
        self.assertEqual(record["assignments"]["implement"]["result"], result)
        _, verified, _ = factory.antigravity_evidence("implement", record, record["assignments"]["implement"],
                                                     collected=True, current=False)
        self.assertEqual(verified["report"]["outcome"], "completed")
        self.assertEqual(args.receipt.read_bytes(), legacy)

    def test_failed_process_or_foreign_stream_cannot_supply_a_result(self):
        self.assertEqual(self.execute(self.args, exit_code=3)["state"], "failed")
        # The failure names Antigravity's own reason: a review ended on quota and only the events said so.
        quota = [*stream(self.repo)[:-1], dict(event="result", result=dict(
            conversation_id=CONVERSATION, status="ERROR", response="", num_turns=1,
            error="Individual quota reached. Resets in 118h38m0s."))]
        failed = self.execute(self.launch("quota", role="qa"), events=quota, exit_code=3)
        self.assertEqual(failed["state"], "failed")
        self.assertIn("exited unsuccessfully: Individual quota reached. Resets in 118h38m0s.", failed["error"])
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
        for identity, message in (("inspect", "receipt failed"), ("missing", "existing assignment")):
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

    def test_reconcile_interrupted_commit_against_its_original_tree(self):
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        git_run = factory.git_run
        def interrupted(repository, *command, **options):
            result = git_run(repository, *command, **options)
            if "commit" in command and "--message" in command and result.returncode == 0:
                raise factory.FactoryError("fixture lost commit response")
            return result
        with patch.object(factory, "git_run", side_effect=interrupted):
            receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual((receipt["state"], receipt["commit"]["state"]), ("failed", "committing"))
        before = [p.read_bytes() for p in (args.receipt, args.events)]
        head = factory.git_commit(".", "HEAD")
        with self.assertRaisesRegex(factory.FactoryError, f"HEAD {head} matches the interrupted commit"):
            factory.reset_writer(self.path, assignment_id="implement")
        self.assertEqual([p.read_bytes() for p in (args.receipt, args.events)], before)
        self.assertFalse(factory.reset_patch(args.receipt).exists())

    def test_reset_recovers_a_writer_lost_with_its_host(self):
        # Observed (#133): a reboot killed six writers mid-run; their receipts stayed "starting",
        # reset and collect both refused them, and the coordinator saved the patches by hand.
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        failed = self.execute(args, edit=True, exit_code=3)
        args.receipt.write_text(json.dumps({**failed, "state": "starting"}))  # The worker died before finishing it.
        with patch.object(factory, "Rex", FakeRex), self.assertRaisesRegex(factory.FactoryError, "does not report it lost"):
            factory.reset_writer(self.path, assignment_id="implement")
        FakeRex.instance = "restarted-instance"
        self.addCleanup(setattr, FakeRex, "instance", "test-instance")
        with patch.object(factory, "Rex", FakeRex):
            result = factory.reset_writer(self.path, assignment_id="implement")
        self.assertIn("+Notes", Path(result["patch"]).read_text())
        self.assertIn("new writer ID", result["next"])
        factory.check_checkout(str(self.base), str(self.repo), self.candidate)

    def test_reset_refuses_a_writer_whose_receipt_did_not_fail(self):
        args = self.launch("implement", role="backend", write=True, commit_message="Add fixture notes", sandbox=sys.executable)
        receipt = self.execute(args, events=stream(self.repo, outcome="completed"), edit=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with self.assertRaisesRegex(factory.FactoryError, "receipt failed"):
            factory.reset_writer(self.path, assignment_id="implement")

    def test_brain_directory_reports_are_retained_beside_the_receipt(self):
        # Observed (#74): Gemini returned only a path into agy's private brain directory.
        home = self.root / "home"
        brain = home / ".gemini" / "antigravity-cli" / "brain" / CONVERSATION
        report_file = brain / "nested/qa-verification-report.md"
        events = stream(self.repo)
        report = dict(outcome="approved", artifacts=["File: nested/../nested/qa-verification-report.md",
            "File: " + str(brain / "alias/qa-verification-report.md"), str(report_file)])
        cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))
        def write_report(cwd):
            report_file.parent.mkdir(parents=True)
            (brain / "alias").symlink_to(report_file.parent, target_is_directory=True)
            report_file.write_text("# QA\nAll six points verified.\n")
        with patch.dict(os.environ, HOME=str(home)):
            with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
                with patch.object(FakeProcess, "pid", child.pid), patch.object(factory, "remove_scratch", return_value=False):
                    receipt = self.execute(self.args, events=events, act=write_report)
                child.communicate()
            self.assertEqual(receipt["state"], "completed", receipt["error"])
            self.assertEqual(receipt["report"], report)
            self.collect("inspect")
            brain.rename(brain.with_name("expired"))
            kept = next(self.args.receipt.with_suffix(".artifacts").iterdir())
            self.assertEqual(kept.read_text(), "# QA\nAll six points verified.\n")
            self.assertEqual(kept.stat().st_mode & 0o777, 0o600)
            self.assertEqual(factory.abandoned_scratch(self.root),
                             [dict(scratch=receipt["scratch"], receipt=str(self.args.receipt))])
            with patch.dict(os.environ, HOME=str(self.root / "other-home")):
                for entry in report["artifacts"]:
                    binding = receipt["retained_files"][entry]
                    self.assertEqual((binding["root"], binding["relative"], binding["conversation"]),
                                     (str(brain), "nested/qa-verification-report.md", CONVERSATION))
                self.assertTrue(factory.abandoned_scratch(self.root))
            moved = kept.rename(kept.parent.parent / kept.name)
            self.assertEqual(factory.abandoned_scratch(self.root), [])
            factory.save(kept, "Different report, same name")
            self.assertEqual(factory.abandoned_scratch(self.root), [])
            kept.unlink()
            os.link(moved, kept)
            self.assertEqual(factory.abandoned_scratch(self.root), [])
            kept.unlink()
            kept.symlink_to(moved)
            self.assertEqual(factory.abandoned_scratch(self.root), [])

    def collect_report(self, args, entries, write_reports, *, conversation=CONVERSATION):
        events = stream(self.repo, conversation=conversation)
        report = dict(outcome="approved", artifacts=entries)
        cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            with patch.object(FakeProcess, "pid", child.pid), patch.object(factory, "remove_scratch", return_value=False):
                receipt = self.execute(args, events=events, act=write_reports)
            child.communicate()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["report"], report)
        identity = args.job.split("/")[1]
        result = self.collect(identity)["assignments"][identity]["result"]
        self.assertEqual(result["artifacts"], [str(args.receipt), *entries])
        self.assertEqual(args.receipt.with_suffix(".report.txt").read_bytes(), ("\n".join(entries) + "\n").encode())
        return receipt

    def test_historical_brace_filenames_copy_without_granting_inline_proof(self):
        generated = factory.brief
        typed_line = factory.TYPED_REPORT_INSTRUCTION
        def historical(role, goal, *, designs=False):
            text = generated(role, goal, designs=designs)
            # Preserve a quotation in the goal and append one after the generated instructions.
            start = text.index(goal) + len(goal)
            return text[:start] + text[start:].replace(typed_line, "Return the report verbatim.") + "\n" + typed_line
        locator = '{"observation":"saved result"}'
        with patch.object(factory, "brief", historical):
            args = self.launch("historical", role="qa", goal="Inspect the saved result.\n" + typed_line)
        entries = ["{proof.md", "{proof.md:12", locator]
        def write_files(cwd):
            for entry in ("{proof.md", locator):
                (cwd / entry).write_text("Original bytes of " + entry)
        receipt = self.collect_report(args, entries, write_files)
        self.assertEqual(set(receipt["retained_files"]), set(entries))
        for entry in entries:
            copy = factory.report_copy_path(args.receipt, receipt, receipt["retained_files"][entry])
            self.assertEqual(copy.read_text(), "Original bytes of " + entry.removesuffix(":12"))
        self.assertEqual(listing_cli(self.root), [dict(scratch=receipt["scratch"], receipt=str(args.receipt))])

        with patch.object(factory, "brief", historical):
            args = self.launch("expired", role="qa", goal="Inspect this citation.\n" + typed_line)
        expired = self.collect_report(args, [locator], lambda cwd: None)
        self.assertEqual(expired["retained_files"], {})
        self.assertNotIn(dict(scratch=expired["scratch"], receipt=str(args.receipt)), listing_cli(self.root))

    def test_conversation_and_checkout_keep_distinct_reports_with_the_same_relative_path(self):
        self.check_report_copies(self.root / "home")

    def check_report_copies(self, home):
        brain = home / factory.ANTIGRAVITY_BRAIN / CONVERSATION
        checkout = factory.reader_checkout_path(self.args.receipt)
        paths = [checkout / "nested/proof.md", brain / "nested/proof.md",
                 checkout / "prefix", brain / "prefix/details", checkout / "reverse/details", brain / "reverse",
                 checkout / "CASE/Proof.md", brain / "case/proof.md", brain / "case/Proof.md",
                 brain / "alias.dat"]
        entries = ["File: " + str(path) for path in paths]
        originals = {}

        def write_reports(cwd):
            self.assertEqual(cwd, checkout)
            for index, path in enumerate(paths):
                path.parent.mkdir(parents=True, exist_ok=True)
                if path == paths[-1]:
                    os.link(paths[1], path)
                elif not path.exists():  # The source volume determines whether the case variant is an alias.
                    path.write_bytes(bytes([index, 0, 255]) + b"\r\n")
                originals[path] = (path.stat(), path.read_bytes())

        with patch.dict(os.environ, HOME=str(home)):
            receipt = self.collect_report(self.args, entries, write_reports)
        self.assertFalse(checkout.exists())
        brain.rename(brain.with_name("expired"))
        copies = {}
        scratch = Path(receipt["scratch"]).resolve()
        scratch_root = Path(factory.SCRATCH_ROOT or "").absolute()
        for path, entry in zip(paths, entries):
            root = brain if path.is_relative_to(brain) else checkout
            binding = receipt["retained_files"][entry]
            self.assertEqual((binding["root"], binding["relative"], binding["conversation"]),
                             (str(root), str(path.relative_to(root)), CONVERSATION if root == brain else None))
            directory, name = binding["copy"].split("/")
            self.assertEqual(directory, "artifacts" if root == brain else "checkout-artifacts")
            copy = self.args.receipt.with_suffix("." + directory) / name
            for unbound in (copy, copy.parent.with_name(copy.parent.name.upper()) / name):
                self.assertFalse(factory.retained_scratch_evidence(str(unbound), checkout, receipt,
                                 self.args.receipt, scratch, scratch_root, self.args, None, True,
                                 factory.ReportHashBudget(128, 128 * 1024**2, 5)))
            info, payload = originals[path]
            key = (root, info.st_dev, info.st_ino)
            self.assertEqual(copies.setdefault(key, copy), copy)
            self.assertEqual(copy.read_bytes(), payload)
            self.assertEqual(binding["sha256"], hashlib.sha256(payload).hexdigest())
            dest = copy.stat()
            self.assertEqual((binding["st_dev"], binding["st_ino"]), (dest.st_dev, dest.st_ino))
            self.assertEqual((dest.st_mode & 0o777, dest.st_nlink, dest.st_uid), (0o600, 1, os.getuid()))
        self.assertEqual(len(set(copies.values())), len(copies))
        expected = [dict(scratch=receipt["scratch"], receipt=str(self.args.receipt))]
        with patch.dict(os.environ, HOME=str(self.root / "observer")):
            moved = [(copy, copy.rename(copy.with_suffix(".moved"))) for copy in copies.values()]
            self.assertEqual(listing_cli(self.root), [])
            for copy, parked in moved:
                parked.rename(copy)
                self.assertEqual(listing_cli(self.root), expected)
                copy.rename(parked)

    def test_checkout_citations_are_excluded_by_object_identity(self):
        for index, name in enumerate(("README.md", "README.MD", "linked.md", "independent.md")):
            with self.subTest(name=name):
                args = self.args if index == 0 else self.launch(f"citation-{index}", role="qa")
                eligible = False
                def write_report(cwd):
                    nonlocal eligible
                    tracked = cwd / "README.md"
                    target = cwd / name
                    if name == "linked.md":
                        os.link(tracked, target)
                    elif not target.exists():
                        target.write_bytes(tracked.read_bytes())
                    eligible = not target.samefile(tracked)
                receipt = self.collect_report(args, [name], write_report)
                self.assertEqual(list(receipt["retained_files"]), [name] if eligible else [])
                attempt = dict(scratch=receipt["scratch"], receipt=str(args.receipt))
                self.assertEqual(attempt in listing_cli(self.root), eligible)

    def test_conversation_copy_checks_the_execution_checkout_index(self):
        home = self.root / "home"
        brain = home / factory.ANTIGRAVITY_BRAIN / CONVERSATION
        brain.mkdir(parents=True)
        for write in (False, True):
            for linked in (True, False):
                with self.subTest(writer=write, linked=linked):
                    role = "backend" if write else "qa"
                    identity = role + ("-linked" if linked else "-independent")
                    target = brain / (identity + ".md")
                    entry = "File: " + str(target)
                    options = dict(write=True, commit_message="Record fixture result", sandbox=sys.executable) if write else {}
                    args = self.launch(identity, role=role, **options)

                    def write_report(cwd):
                        tracked = cwd / "README.md"
                        if linked:
                            os.link(tracked, target)
                        else:
                            target.write_bytes(tracked.read_bytes())

                    with patch.dict(os.environ, HOME=str(home)):
                        with patch.dict(os.environ, FakeRex.writer_environment() if write else {}):
                            receipt = self.collect_report(args, [entry], write_report)
                    target.unlink()
                    self.assertEqual(list(receipt["retained_files"]), [] if linked else [entry])
                    attempt = dict(scratch=receipt["scratch"], receipt=str(args.receipt))
                    self.assertEqual(attempt in listing_cli(self.root), not linked)

    def test_unresolvable_conversation_preserves_native_result_and_checkout_copy(self):
        home = self.root / "home"
        brain = home / factory.ANTIGRAVITY_BRAIN / CONVERSATION
        entries = ["File: " + str(brain / "proof.md"), "checkout-proof.md"]
        real_resolve = Path.resolve

        def resolve(path, *, strict=False):
            if path == brain:
                raise RuntimeError("Symlink loop in the conversation directory")
            return real_resolve(path, strict=strict)

        def write_report(cwd):
            (cwd / "checkout-proof.md").write_text("Independent checkout evidence")

        diagnostics = StringIO()
        with (patch.dict(os.environ, HOME=str(home)), patch.object(Path, "resolve", new=resolve),
              redirect_stderr(diagnostics)):
            receipt = self.collect_report(self.args, entries, write_report)
        self.assertIsNone(receipt["conversation_root"])
        self.assertEqual(list(receipt["retained_files"]), ["checkout-proof.md"])
        self.assertIn("optional Antigravity conversation directory", diagnostics.getvalue())

    def test_filesystem_invalid_conversation_id_preserves_the_native_verdict(self):
        proof = "checkout-proof.md"

        def write_report(cwd):
            (cwd / proof).write_text("Independent checkout evidence")

        receipt = self.collect_report(self.args, ["Original observation", proof], write_report,
                                      conversation="invalid\0id")
        self.assertIsNone(receipt["conversation_root"])
        self.assertEqual(list(receipt["retained_files"]), [proof])

    def test_unreadable_conversation_directory_keeps_checkout_evidence(self):
        home = self.root / "home"
        brain = home / factory.ANTIGRAVITY_BRAIN / CONVERSATION
        proof = "checkout-proof.md"
        real_is_dir = Path.is_dir

        def is_dir(path):
            if path == brain:
                raise PermissionError("Conversation directory cannot be read")
            return real_is_dir(path)

        def write_report(cwd):
            (cwd / proof).write_text("Independent checkout evidence")

        with patch.dict(os.environ, HOME=str(home)), patch.object(Path, "is_dir", new=is_dir):
            receipt = self.collect_report(self.args, [proof], write_report)
        self.assertEqual(list(receipt["retained_files"]), [proof])
        self.assertEqual(listing_cli(self.root), [dict(scratch=receipt["scratch"], receipt=str(self.args.receipt))])

    def test_reader_retains_only_independent_reports_after_a_valid_index_change(self):
        entries = ["README.md", "alias.md", "newly-indexed.md", "proof.md"]
        report = dict(outcome="approved", artifacts=entries)
        events = stream(self.repo)
        cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))

        def write_reports(cwd):
            self.git("rm", "--cached", "README.md", directory=cwd)
            os.link(cwd / "README.md", cwd / "alias.md")
            (cwd / "newly-indexed.md").write_text("Final index entry")
            self.git("add", "newly-indexed.md", directory=cwd)
            (cwd / "proof.md").write_text("Independent reader observation")

        receipt = self.execute(self.args, events=events, act=write_reports)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        self.assertEqual(receipt["report"], report)
        self.assertEqual(list(receipt["retained_files"]), ["proof.md"])
        kept = factory.report_copy_path(self.args.receipt, receipt, receipt["retained_files"]["proof.md"])
        self.assertEqual(kept.read_text(), "Independent reader observation")

    def test_damaged_reader_index_cannot_authorize_a_conversation_copy(self):
        home = self.root / "home"
        brain = home / factory.ANTIGRAVITY_BRAIN / CONVERSATION
        brain.mkdir(parents=True)
        target = brain / "proof.md"
        target.write_text("Independent fixture report")
        events = stream(self.repo)
        report = dict(outcome="approved", artifacts=["File: " + str(target)])
        cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))

        check_evidence = factory.check_tool_evidence
        for damaged in ("missing", "corrupt"):
            for expired in (False, True):
                with self.subTest(damaged=damaged, expired=expired):
                    args = (self.args if damaged == "missing" and not expired else
                            self.launch(f"{damaged}-reader-index-{expired}".lower(), role="qa"))
                    now = 10

                    def damage_index(cwd):
                        index = factory.git_dir(cwd) / "index"
                        if damaged == "missing":
                            index.unlink()
                        else:
                            index.write_bytes(b"Invalid test index")

                    def after_native(*values):
                        nonlocal now
                        if expired:
                            now += args.seconds
                        return check_evidence(*values)

                    with (patch.dict(os.environ, HOME=str(home)),
                          patch.object(factory, "native_clock", side_effect=lambda: now),
                          patch.object(factory, "check_tool_evidence", side_effect=after_native)):
                        receipt = self.execute(args, events=events, act=damage_index)
                    self.assertEqual(receipt["state"], "failed")
                    self.assertIn("checkout index", receipt["error"])
                    self.assertEqual(receipt["retained_files"], {})
                    self.assertFalse(args.receipt.with_suffix(".artifacts").exists())

    def test_foreign_conversation_file_is_excluded_while_readable(self):
        self.check_external_reports(self.root / "other-home")

    def check_external_reports(self, other):
        home = self.root / "worker-home"
        namespace = factory.ANTIGRAVITY_BRAIN
        denied = home / ".gemini"
        denied.mkdir(parents=True)
        for identity, target, eligible in (
                ("inspect", home / namespace / "other-conversation/proof.md", False),
                ("different-home", other / namespace / CONVERSATION / "proof.md", False),
                ("ordinary", other / "brain" / CONVERSATION / "proof.md", True),
                ("case-namespace", other / ".GEMINI/antigravity-cli/brain/independent/proof.md", True)):
            with self.subTest(identity=identity):
                target.parent.mkdir(parents=True)
                target.write_text("Inert external evidence")
                if identity == "case-namespace":
                    denied = other / ".gemini"
                    eligible = not denied.samefile(other / ".GEMINI")
                spelling = other / str(namespace).upper() / CONVERSATION / "proof.md"
                if identity == "different-home" and spelling.exists():
                    target = spelling
                args = self.args if identity == "inspect" else self.launch(identity, role="qa")
                with patch.dict(os.environ, HOME=str(home)):
                    receipt = self.collect_report(args, ["File: " + str(target)], None)
                self.assertEqual(receipt["retained_files"], {})
                attempt = dict(scratch=receipt["scratch"], receipt=str(args.receipt))
                self.assertEqual(attempt in listing_cli(self.root), eligible)
                denied.chmod(0)
                try:
                    if os.geteuid() != 0:
                        with self.assertRaises(PermissionError):
                            (denied / "antigravity-cli/brain").stat()
                    if eligible or identity == "different-home":
                        self.assertTrue(target.read_bytes())
                    self.assertEqual(attempt in listing_cli(self.root), eligible)
                finally:
                    denied.chmod(0o700)

    def test_a_report_file_in_the_disposable_checkout_is_retained_but_cited_files_are_not(self):
        # Observed: a review approved with only `review-retire-service-pins.md`, which went with its checkout.
        events = stream(self.repo)
        report = dict(outcome="approved", artifacts=["review-retire-service-pins.md", "README.md"])
        cast(dict, events[-1]["result"]).update(structured_output=report,
            response="Prose summary of the review." + json.dumps(report) + "\n")
        def write_report(cwd):
            (cwd / "review-retire-service-pins.md").write_text("# Review\nApproved.\n")
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE) as child:
            with patch.object(FakeProcess, "pid", child.pid), patch.object(factory, "remove_scratch", return_value=False):
                receipt = self.execute(self.args, events=events, act=write_report)
            child.communicate()
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        kept, = self.args.receipt.with_suffix(".checkout-artifacts").iterdir()
        self.assertEqual(set(receipt["retained_files"]), {"review-retire-service-pins.md"})
        self.assertEqual(kept.read_text(), "# Review\nApproved.\n")
        self.assertFalse(factory.reader_checkout_path(self.args.receipt).exists())
        self.collect("inspect")
        self.assertEqual(factory.abandoned_scratch(self.root),
                         [dict(scratch=receipt["scratch"], receipt=str(self.args.receipt))])
        kept.unlink()
        self.assertEqual(self.args.receipt.with_suffix(".report.txt").read_text().splitlines(), report["artifacts"])
        self.assertEqual(factory.abandoned_scratch(self.root), [])

    def test_a_bare_report_name_in_the_brain_directory_is_retained(self):
        # Observed: a Sonnet review wrote review-retire-service-pins.md into agy's brain directory
        # and named it without a directory.
        home = self.root / "home"
        brain = home / ".gemini" / "antigravity-cli" / "brain" / CONVERSATION
        events = stream(self.repo)
        report = dict(outcome="approved", artifacts=["review-retire-service-pins.md"])
        cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))
        def write_report(cwd):
            brain.mkdir(parents=True)
            (brain / "review-retire-service-pins.md").write_text("# Review\nApproved.\n")
        with patch.dict(os.environ, HOME=str(home)):
            receipt = self.execute(self.args, events=events, act=write_report)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        kept, = self.args.receipt.with_suffix(".artifacts").iterdir()
        self.assertEqual(kept.read_text(), "# Review\nApproved.\n")

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
    """Antigravity reports count toward delivery approvals under their native conversation identity."""

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

    def run_agy(self, identity, role, conversation, *, write=False, exit_code=0, events=None, effort: str | None = "high", frozen=None):
        f = self.fixture
        writer: dict[str, Any] = dict(write=True, commit_message="Add fixture notes", sandbox=sys.executable) if write else {}
        command = factory.antigravity_command(f.path, task="deliver", assignment_id=identity, agy=sys.executable,
            model="gemini-3.8-flash-high", effort=effort, cwd=str(f.repo), role=role, release_evidence=frozen, **writer)
        record, assignment = factory.launch(f.path, task="deliver", assignment_id=identity, role=role,
            goal="Fixture " + role, cwd=str(f.repo), command=command, purpose="work",
            binary=sys.executable, server="unix:///fixture.sock", frozen_release=frozen)
        replacements = {"{repository}": str(f.repo), "{cwd}": str(f.repo), "{brief}": assignment["brief"],
            "{brief_sha256}": assignment["request"]["instructions_sha256"], "{candidate}": record["inputs"]["candidate"],
            "{requirements}": record["inputs"]["requirements"],
            "{repository_identity}": json.dumps(record["admission"]["repository_identity"], sort_keys=True)}
        args = antigravity_worker.parser().parse_args([replacements.get(part, part) for part in command[4:]])
        if events is None:
            events = stream(f.repo, outcome="completed" if write else "approved", conversation=conversation)
        async def spawn(*command, **options):
            if write and not exit_code:
                (self.fixture.repo / "NOTES.md").write_text("Notes\n")  # A completed writer commits its change (#166).
            return FakeProcess(echo_cwd(events, options), exit_code=exit_code)
        with redirect_stdout(StringIO()):
            receipt = asyncio.run(antigravity_worker.execute(args, spawn=spawn, probe=usage_probe, clock=lambda: 1000,
                timer=lambda: 10, git_scope=lambda *a, **k: nullcontext(), scan=lambda *a, **k: None))
        return record, assignment, receipt

    def test_collected_antigravity_approval_counts_beside_the_codex_release(self):
        selected = integration.approvals(self.record, "release", ["agy-qa"])
        self.assertEqual(selected["agy-qa"]["context"], "11111111-1111-4111-8111-111111111111")
        with patch.object(factory, "ROOT", self.fixture.root / "another-install"):
            self.assertEqual(integration.approvals(self.record, "release", ["agy-qa"]), selected)

    def test_writer_contributes_and_a_failed_unstarted_writer_still_counts(self):
        # The failed writer runs first: the completed one commits and moves the checkout on (#166).
        record, failed, receipt = self.run_agy("agy-failed", "backend", "unused", write=True, exit_code=3, events=[])
        self.assertEqual(receipt["state"], "failed")
        self.assertEqual(integration.contributor_context("agy-failed", record, failed),
                         "antigravity-unstarted:deliver/agy-failed")
        self.fixture.release_skill_merges()
        self.assertEqual(self.fixture.delivered_cli(review="release", required="agy-qa")[0], 0)
        self.fixture.review("accept", "release", delivery="release")
        self.assertEqual(integration.delivered_accepted(self.fixture.path, "release", "accept")["outcome"], "approved")
        record, assignment, receipt = self.run_agy("agy-writer", "backend", "22222222-2222-4222-8222-222222222222", write=True)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited", exit=dict(exit_code=0))):
            record = factory.collect_antigravity(self.fixture.path, "agy-writer", None, record, assignment)
            with patch.object(factory, "ROOT", self.fixture.root / "other-install"):
                self.assertEqual(integration.contributor_context("agy-writer", record, record["assignments"]["agy-writer"]),
                                 "22222222-2222-4222-8222-222222222222")
            self.assertIn("agy-qa", integration.approvals(record, "release", ["agy-qa"]))

    def test_failed_writer_after_init_requires_original_stream_at_delivery_and_acceptance(self):
        f = self.fixture
        context = "11111111-1111-4111-8111-111111111111"  # agy-qa's original conversation
        failed = stream(f.repo, conversation=context)[:1] + [
            dict(event="result", result=dict(conversation_id=context, status="ERROR", error="fixture failure"))]
        _, assignment, receipt = self.run_agy("failed", "backend", context, write=True, exit_code=3, events=failed)
        self.assertEqual((receipt["schema_version"], receipt["state"], receipt["conversation_id"]), (8, "failed", None))
        f.release_skill_merges()
        self.assertIn("independent of contributors", f.delivered_cli(review="release", required="agy-qa")[2])
        select = dict(review="release", required="qa")
        events = Path(receipt["events"])
        saved = events.read_bytes()
        backup = events.with_suffix(".original")
        events.rename(backup)
        for damage in ("missing", "symlink", "unreadable", "corrupt", "emptied"):
            with self.subTest(damage=damage):
                if damage == "symlink":
                    events.symlink_to(backup)
                elif damage == "unreadable":
                    events.mkdir()
                elif damage in ("corrupt", "emptied"):
                    events.write_bytes(b"{}\n" if damage == "corrupt" else b"")
                code, output, error = f.delivered_cli(**select)
                self.assertEqual((code, output, len(error.splitlines())), (1, None, 1))
                self.assertIn("deliver/failed: cannot verify", error)
                self.assertIn("original Antigravity event stream", error)
                self.assertFalse((f.tasks / "deliver.release.delivered.json").exists())
                if damage == "unreadable":
                    events.rmdir()
                else:
                    events.unlink(missing_ok=True)
        backup.rename(events)
        command = assignment["request"]["command"]
        receipt_path = Path(command[command.index("--receipt") + 1])
        self.assertEqual(f.delivered_cli(**select)[0], 0)
        f.review("accept", "release", delivery="release")
        self.assertEqual(integration.delivered_accepted(f.path, "release", "accept")["outcome"], "approved")
        f.review("self-final", "release", delivery="release", context=context)
        with self.assertRaisesRegex(factory.FactoryError, "contributor cannot judge"):
            integration.delivered_accepted(f.path, "release", "self-final")

        replacement = saved.replace(context.encode(), CONVERSATION.encode())
        unsigned = {key: value for key, value in receipt.items() if key != "events_digest"}
        legacy = {**unsigned, "schema_version": 2}
        for original, data in ((receipt, replacement), (unsigned, saved), ({**unsigned, "conversation_id": context}, saved),
                               ({**receipt, "conversation_id": CONVERSATION}, saved),
                               (legacy, replacement), (legacy, saved), (legacy, b""),
                               ({**legacy, "conversation_id": context}, replacement),
                               ({**legacy, "conversation_id": context}, saved + saved)):
            with self.subTest(version=original["schema_version"], identity=original["conversation_id"], data=data[:30]):
                factory.save(receipt_path, original)
                before = receipt_path.read_bytes()
                events.write_bytes(data)
                self.assertIn("deliver/failed: cannot verify", f.delivered_cli(**select)[2])
                with self.assertRaisesRegex(factory.FactoryError, "deliver/failed: cannot verify"):
                    integration.delivered_accepted(f.path, "release", "accept")
                self.assertEqual(receipt_path.read_bytes(), before)
        factory.save(receipt_path, receipt)
        events.unlink()
        with self.assertRaisesRegex(factory.FactoryError, "deliver/failed: cannot verify"):
            integration.delivered_accepted(f.path, "release", "accept")

    def test_attested_init_only_failure_retains_its_conversation_across_receipt_versions(self):
        f = self.fixture
        context = "11111111-1111-4111-8111-111111111111"
        _, assignment, receipt = self.run_agy("failed", "backend", context, write=True, exit_code=3,
                                             events=stream(f.repo, conversation=context)[:1])
        command = assignment["request"]["command"]
        path = Path(command[command.index("--receipt") + 1])
        f.release_skill_merges()
        self.assertEqual(f.delivered_cli(review="release", required="qa")[0], 0)
        f.review("accept", "release", delivery="release")
        for version in (8, 7, 4, 2):
            with self.subTest(version=version):
                factory.save(path, {**receipt, "schema_version": version})
                original = path.read_bytes()
                self.assertIn("independent of contributors", f.delivered_cli(review="release", required="agy-qa")[2])
                self.assertEqual(integration.delivered_accepted(f.path, "release", "accept")["outcome"], "approved")
                self.assertEqual(path.read_bytes(), original)

    def test_legacy_failure_after_parsing_retains_its_receipt_bound_conversation(self):
        f = self.fixture
        context = "11111111-1111-4111-8111-111111111111"
        with patch.object(factory_commit, "commit_completed", side_effect=factory.FactoryError("fixture commit failed")):
            _, assignment, receipt = self.run_agy("failed", "backend", context, write=True)
        self.assertEqual((receipt["state"], receipt["conversation_id"]), ("failed", context))
        command = assignment["request"]["command"]
        path = Path(command[command.index("--receipt") + 1])
        receipt["schema_version"] = 2
        del receipt["events_digest"]
        factory.save(path, receipt)
        original = path.read_bytes()
        factory.reset_writer(f.path, assignment_id="failed")
        f.release_skill_merges()
        self.assertIn("independent of contributors", f.delivered_cli(review="release", required="agy-qa")[2])
        self.assertEqual(f.delivered_cli(review="release", required="qa")[0], 0)
        f.review("accept", "release", delivery="release")
        self.assertEqual(integration.delivered_accepted(f.path, "release", "accept")["outcome"], "approved")
        self.assertEqual(path.read_bytes(), original)

    def test_an_antigravity_qa_report_cannot_be_the_release_judgment(self):
        with self.assertRaises(factory.FactoryError):
            integration.approvals(self.record, "agy-qa", ["qa"])

    def test_antigravity_gives_the_release_judgment_only_at_max_effort(self):
        conversation = "33333333-3333-4333-8333-333333333333"
        record, assignment, receipt = self.run_agy("agy-release", "release", conversation, effort="max", frozen=self.fixture.frozen)
        self.assertEqual(receipt["state"], "completed", receipt["error"])
        with patch.object(factory, "status", return_value=dict(state="exited")):
            record = factory.collect_antigravity(self.fixture.path, "agy-release", None, record, assignment)
            selected = integration.approvals(record, "agy-release", ["qa", "agy-qa"])
        self.assertEqual(selected["agy-release"]["evidence"], self.fixture.frozen)
        with self.assertRaisesRegex(factory.FactoryError, "maximum effort: select max, not high"):
            self.run_agy("lazy-release", "release", "unused", effort="high", frozen=self.fixture.frozen)
        # Omitting --effort runs at the CLI's default, so it is refused too.
        with self.assertRaisesRegex(factory.FactoryError, "maximum effort: select max, not None"):
            self.run_agy("default-release", "release", "unused", effort=None, frozen=self.fixture.frozen)


if __name__ == "__main__":
    unittest.main()
