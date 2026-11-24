"""Local process and receipt evidence for the read-only scratch listing."""
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, redirect_stdout
from io import StringIO
from typing import cast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import runtime_path  # noqa: F401
import factory


FIXTURE_INPUTS = dict(candidate="a" * 40, requirements="fixture-requirements")


def listing_cli(state):
    # The production CLI, with the same private scratch root as the fixture worker.
    cli = ("import factory, sys; factory.SCRATCH_ROOT = sys.argv.pop(1); "
           "raise SystemExit(factory.main(sys.argv[1:]))")
    result = subprocess.run([sys.executable, "-B", "-c", cli, str(factory.SCRATCH_ROOT),
                             "--state-dir", str(state), "abandoned-scratch"],
                            cwd=factory.ROOT, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def record_attempt(receipt, harness, inputs):
    task = receipt.name.split(".", 1)[0]
    task_path = receipt.parent / f"{task}.json"
    record = factory.new_task(task)
    record["inputs"] = inputs
    record["admission"] = dict(goal="Check the result", repository=str(receipt.parent), acceptance=["README.md"],
                               initial_candidate=inputs["candidate"], limits=dict(correction=1, investigation=1),
                               repository_identity=None)
    record["rex"] = dict(binary="/fixture/rex", server="unix:///fixture/rex.sock", host_id="fixture-host")
    record["session_id"] = "fixture-session"
    command = [sys.executable, "-E", "-s", str(factory.ROOT / f"{harness}_worker.py"),
               "--job", f"{task}/qa", "--receipt", str(receipt),
               "--" + ("agy" if harness == "antigravity" else harness), f"/fixture/{harness}",
               "--cwd", "{cwd}", "--repo", str(receipt.parent), "--worktree-git-dir", "/fixture/gitdir",
               "--brief", "{brief}", "--brief-sha256", "{brief_sha256}", "--events", str(receipt.with_suffix(".jsonl")),
               "--candidate", inputs["candidate"], "--requirements", inputs["requirements"], "--repo-identity", "{}",
               "--role", "qa", "--model", "fixture-model", "--effort", "high"]
    if harness in ("grok", "claude"):
        command.extend(["--native-session", "fixture-native-session"])
    brief = receipt.parent / f"{task}.qa.md"
    factory.save(brief, factory.brief("qa", "Check the result"))
    record["assignments"]["qa"] = dict(
        request=dict(role="qa", inputs=inputs, purpose="work", goal="Check the result", cwd=str(receipt.parent),
                     instructions_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(), command=command),
        brief=str(brief), window_label=f"{task}:qa-qa-00000000",
        state="launched", window_id="fixture-window", block_id="fixture-block",
        interpreter=dict(invocation=sys.executable, file=factory.interpreter_identity(sys.executable)),
        result=None, native_collected=False)
    factory.save(task_path, record)
    return task_path


class AbandonedScratch(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.base = Path(directory.name).resolve()
        self.root = self.base / "scratch"
        self.state = self.base / "state"
        self.root.mkdir()
        self.state.mkdir()
        scratch = patch.object(factory, "SCRATCH_ROOT", str(self.root))
        scratch.start()
        self.addCleanup(scratch.stop)

    def scratch(self, name="sf-candidate"):
        path = self.root / name
        path.mkdir()
        return path

    def receipt(self, scratch=None, *, harness="claude", task="task", **changes):
        if scratch is None:
            scratch = self.scratch()
        path = self.state / f"{task}.qa.{harness}.json"
        info = scratch.lstat()
        value = dict(schema_version=8, retained_files={}, interpreter=factory.interpreter_identity(sys.executable), kind=f"{harness}-execution", job=f"{task}/qa", inputs=FIXTURE_INPUTS,
                     scratch=str(scratch), scratch_removed=False,
                     scratch_identity=dict(st_dev=info.st_dev, st_ino=info.st_ino), state="completed", created_at=1000,
                     finished_at=1001, report=dict(outcome="approved", artifacts=['{"observation":"The fixture checks passed."}']),
                     native_process=dict(pid=201, start_time=400, scope="fixture-scope"))
        if harness == "antigravity":
            value.update(conversation_id="original", conversation_root=str(self.base / "brain/original"))
        record_attempt(path, harness, FIXTURE_INPUTS)
        value["brief_sha256"] = hashlib.sha256((self.state / f"{task}.qa.md").read_bytes()).hexdigest()
        factory.save(path, {**value, **changes})
        return path

    def listed(self, *, lookup: type[OSError] | Callable[[int], int] = ProcessLookupError):
        with patch.object(factory, "process_scope", return_value="fixture-scope"), \
                patch.object(factory.os, "getpgid", side_effect=lookup):
            return factory.abandoned_scratch(self.state)

    def test_cli_lists_retained_reports_without_writing_or_installing(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch, finished_at=1000)
        (scratch / "marker").write_text("Retained scratch contents")

        def snapshot():
            return {p.relative_to(self.base): (p.lstat().st_mode, p.lstat().st_ino, p.lstat().st_mtime_ns,
                                               p.read_bytes() if p.is_file() else None)
                    for p in self.base.rglob("*")}

        before = snapshot()
        output = StringIO()
        with patch.object(factory, "process_scope", return_value="fixture-scope"), \
                patch.object(factory.os, "getpgid", side_effect=ProcessLookupError), \
                patch.object(factory, "relocate", side_effect=AssertionError("listing attempted installation")), \
                redirect_stdout(output):
            self.assertEqual(factory.main(["--state-dir", str(self.state), "abandoned-scratch"]), 0)
        self.assertEqual(json.loads(output.getvalue()), [dict(scratch=str(scratch), receipt=str(receipt))])
        self.assertEqual(snapshot(), before)

    def test_accepts_only_provider_supported_empty_reports_and_rejects_blank_entries(self):
        scratch = self.scratch()
        for harness in factory.NATIVE_HARNESSES:
            with self.subTest(harness=harness):
                receipt = self.receipt(scratch, harness=harness, report=dict(outcome="approved", artifacts=[]))
                expected = [dict(scratch=str(scratch), receipt=str(receipt))] if harness in ("grok", "antigravity") else []
                self.assertEqual(self.listed(), expected)
                self.receipt(scratch, harness=harness, report=dict(outcome="approved", artifacts=["Valid evidence", " \t"]))
                self.assertEqual(self.listed(), [])

    def test_retained_evidence_survives_scratch_removal(self):
        scratch = self.scratch()
        local = scratch / "original.md"
        local.write_text("Result in scratch\n")
        external = self.base / "saved result.md"
        external.write_text("Independent result\n")
        alias = self.base / "alias.md"
        alias.symlink_to(local)
        for harness in factory.NATIVE_HARNESSES:
            for artifact, retained in (
                    ("Result: [advisory] Verified report retention under Python 3.15.2. missing.py:1", False),
                    ("Result: [defect] The worker preserves `scratch_removed=false`. a.py:1 b.py:2", False),
                    ('{"observation":"Verified `scratch_removed=false`","citations":["a.py:1","b.py:2"]}', True),
                    ('{"observation":"  "}', False), ('{"citations":["a.py:1"]}', False),
                    ('{"observation":"verified","file":"report.md"}', False),
                    ('{"observation":"verified","citations":"a.py:1"}', False),
                    ('{"observation":"verified","citations":[" "]}', False),
                    ('{"observation":"verified","observation":""}', False), ('{"observation":', False),
                    ("Result: [defect]", False), ("Result: [defect] missing.py:1 absent.py:2", False),
                    (f"Result: {local}", False), (f"Result: `{local}`", False),
                    ("Result: nested/report.md", False), ("Result: saved report.md", False),
                    (f"Result: {external}", True), (f"Result: {external}'", False),
                    ("Result: ", False), ("Result: [source](missing.py:2)", False),
                    ("[defect] Confirmed the report is saved as MISSING", False),
                    ("See the attached report. result.md:1", False), ("The fixture checks passed.", False),
                    ("File: missing result.md", False), (f"File: {local}", False), (f"File: {alias}", False),
                    (f"File: {external}", True), (str(external), True), (f"[Saved](<{external}>)", True),
                    (f"- [QA report](<{external}>)", True), (f"The report is at {external}.", True),
                    (f"Read {external} for the full report", True), (f"Consult the original at {external}.", True),
                    ("- [QA report](result.md)", False), (f"- [QA report]({local})", False)):
                with self.subTest(harness=harness, artifact=artifact):
                    receipt = self.receipt(scratch, harness=harness, report=dict(outcome="approved", artifacts=[artifact]))
                    self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))] if retained else [])

    def test_inline_requires_the_exact_private_brief_and_matching_receipt_digest(self):
        path = self.receipt()
        brief = self.state / "task.qa.md"
        original = brief.read_bytes()
        receipt = json.loads(path.read_bytes())
        for invalid in (original.replace(b"\n", b"\r\n"), b"Other instructions"):
            brief.write_bytes(invalid)
            self.assertEqual(self.listed(), [])
        brief.write_bytes(original)
        brief.chmod(0o644)
        self.assertEqual(self.listed(), [])
        brief.chmod(0o600)
        factory.save(path, {**receipt, "brief_sha256": "0" * 64})
        self.assertEqual(self.listed(), [])
        factory.save(path, receipt)
        self.assertEqual(self.listed(), [dict(scratch=receipt["scratch"], receipt=str(path))])
        brief.unlink()
        self.assertEqual(self.listed(), [])
        external = self.base / "external.md"
        external.write_text("Independent result")
        receipt["report"]["artifacts"].append("File: " + str(external))
        factory.save(path, receipt)
        self.assertEqual(self.listed(), [dict(scratch=receipt["scratch"], receipt=str(path))])

    def test_lists_durable_evidence_through_the_configured_scratch_root_alias(self):
        scratch = self.scratch()
        alias = self.base / "scratch-alias"
        alias.symlink_to(self.root, target_is_directory=True)
        evidence = self.root / "result.md"
        evidence.write_text("Retained result outside the candidate scratch\n")
        receipt = self.receipt(scratch, report=dict(outcome="approved", artifacts=[str(alias / evidence.name)]))
        with patch.object(factory, "SCRATCH_ROOT", str(alias)):
            self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])

    def test_does_not_substitute_unrelated_files_for_missing_relative_result(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch, report=dict(outcome="approved", artifacts=["review.md"]))
        (self.state / "review.md").write_text("Unrelated file in the launch directory\n")
        receipt.with_suffix(".artifacts").mkdir()
        (receipt.with_suffix(".artifacts") / "review.md").write_text("Unbound sidecar\n")
        self.assertEqual(self.listed(), [])

    def test_unrecorded_copy_at_the_exact_expected_path_is_insufficient(self):
        scratch = self.scratch()
        for harness in factory.NATIVE_HARNESSES:
            with self.subTest(harness=harness):
                receipt = self.receipt(scratch, harness=harness, report=dict(outcome="approved", artifacts=["nested/proof.md"]))
                copy = receipt.with_suffix(".artifacts") / "nested/proof.md"
                copy.parent.mkdir(parents=True)
                factory.save(copy, "Unrelated file")
                self.assertEqual(self.listed(), [])

    def test_historical_v8_bindings_verify_only_at_the_original_destination(self):
        scratch = self.scratch()
        path = self.receipt(scratch, harness="antigravity")
        checkout = factory.reader_checkout_path(path)
        receipt = json.loads(path.read_bytes())
        brain = Path(receipt["conversation_root"])
        flat = path.with_suffix(".artifacts")
        split = path.with_suffix(".checkout-artifacts")
        flat.mkdir()
        split.mkdir()
        entries = {}
        for root, relative, directory in ((brain, "conversation", flat), (checkout, "checkout", flat),
                                          (brain, "both", flat), (checkout, "both", split)):
            entry = str(root / relative)
            copy = directory / relative
            payload = entry.encode()
            copy.write_bytes(payload)
            copy.chmod(0o600)
            info = copy.stat()
            receipt["retained_files"][entry] = dict(
                root=str(root), relative=relative, conversation=brain.name if root == brain else None,
                st_dev=info.st_dev, st_ino=info.st_ino, sha256=hashlib.sha256(payload).hexdigest())
            entries[entry] = copy

        for entry, copy in entries.items():
            with self.subTest(entry=entry):
                receipt["report"]["artifacts"] = [entry]
                factory.save(path, receipt)
                before = path.read_bytes()
                self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(path))])
                other = (flat if copy.parent == split else split) / copy.name
                parked = other.rename(other.with_suffix(".parked")) if other.exists() else None
                copy.rename(other)
                self.assertEqual(self.listed(), [])
                other.rename(copy)
                if parked:
                    parked.rename(other)
                self.assertEqual(before, path.read_bytes())

    def test_saved_interpreter_and_worker_aliases_need_the_original_files(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch)
        task = self.state / "task.json"
        record = json.loads(task.read_text())
        command = record["assignments"]["qa"]["request"]["command"]
        interpreter = self.base / "custom-python"
        interpreter.symlink_to(sys.executable)
        installed = self.base / "installed/skill/runtime"
        installed.mkdir(parents=True)
        worker = installed / "claude_worker.py"
        worker.touch()
        equivalent = self.base / "CaseHome/alias.py"
        equivalent.parent.mkdir()
        os.link(worker, equivalent)
        record["assignments"]["qa"]["interpreter"]["invocation"] = str(interpreter)
        command[0], command[3] = str(interpreter), str(equivalent)
        factory.save(task, record)
        with patch.object(factory, "runtime_home", return_value=installed.parents[1]):
            self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])
            with patch.object(factory.sys, "executable", "/bin/sh"), patch.dict(os.environ, PATH=os.defpath), \
                    patch.object(subprocess, "Popen", side_effect=AssertionError("executed during listing")):
                self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])
            interpreter.unlink()
            interpreter.symlink_to("/bin/sh")
            self.assertEqual(self.listed(), [])
            interpreter.unlink()
            interpreter.symlink_to(sys.executable)
            equivalent.unlink()
            equivalent.touch()
            self.assertEqual(self.listed(), [])

    def test_excludes_receipt_outside_its_recorded_attempt_path(self):
        path = self.receipt()
        path.rename(self.state / "unrecorded.qa.claude.json")
        self.assertEqual(self.listed(), [])

    def test_excludes_receipt_with_mismatched_job(self):
        scratch = self.scratch()
        for job in ("other/qa", "task/other", None):
            with self.subTest(job=job):
                self.receipt(scratch, job=job)
                self.assertEqual(self.listed(), [])

    def test_requires_the_attempt_pins_in_the_worker_command_and_receipt(self):
        scratch = self.scratch()
        for field, other in (("candidate", "b" * 40), ("requirements", "another revision")):
            with self.subTest(field=field):
                receipt = self.receipt(scratch, inputs={**FIXTURE_INPUTS, field: other})
                self.assertEqual(self.listed(), [])
                task = self.state / "task.json"
                record = json.loads(task.read_text())
                command = record["assignments"]["qa"]["request"]["command"]
                command[command.index("--" + field) + 1] = other
                factory.save(task, record)
                self.assertEqual(self.listed(), [])
                self.assertEqual(json.loads(receipt.read_text())["inputs"][field], other)

    def test_requires_observed_launch(self):
        self.receipt()
        task_path = self.state / "task.json"
        record = json.loads(task_path.read_text())
        assignment = record["assignments"]["qa"]
        del assignment["window_id"], assignment["block_id"]
        assignment["state"] = "launching"
        factory.save(task_path, record)
        self.assertEqual(self.listed(), [])
        assignment.update(state="abandoned", abandoned=dict(reason="Rex rejected the command", at=1000))
        factory.save(task_path, record)
        self.assertEqual(self.listed(), [])

    def test_requires_effective_job_and_receipt_from_saved_worker_arguments(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch, harness="codex")
        task_path = self.state / "task.json"
        record = json.loads(task_path.read_text())
        request = record["assignments"]["qa"]["request"]
        command = request["command"]
        relative = [*command[:6], f"--receipt={receipt.name}", *command[8:]]
        request["command"] = relative
        factory.save(task_path, record)
        self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])
        for invalid in ([*relative[:5], "other/qa", *relative[6:]],
                        [*relative, "--job=task/qa"], [*relative, "--receipt", str(receipt)],
                        [*relative[:6], f"--receip={receipt.name}", *relative[7:]],
                        [*relative[:3], str(self.base / "codex_worker.py"), *relative[4:]],
                        ["/bin/sh", *relative[1:]]):
            with self.subTest(command=invalid):
                request["command"] = invalid
                factory.save(task_path, record)
                self.assertEqual(self.listed(), [])

    @patch.object(factory, "time", wraps=factory.time)
    def test_census_hash_budget_is_shared_across_attempts(self, clock):
        clock.monotonic.return_value = 10
        scratch = self.scratch()
        evidence = self.base / "small"
        evidence.write_text("retained")
        absent = [str(self.base / f"absent-{i}") for i in range(128)]
        self.receipt(scratch, report=dict(outcome="approved", artifacts=absent))
        second = self.scratch("sf-second")
        second_receipt = self.receipt(second, task="task2", report=dict(outcome="approved", artifacts=[str(evidence)]))
        receipt = json.loads(second_receipt.read_bytes())
        self.assertEqual(self.listed(), [])
        # A later assignment still qualifies through its own hash-bound inline observation.
        receipt["report"]["artifacts"].append('{"observation":"Independent verification passed."}')
        factory.save(second_receipt, receipt)
        self.assertEqual(self.listed(), [dict(scratch=str(second), receipt=str(second_receipt))])

    @patch.object(factory, "time", wraps=factory.time)
    def test_census_charges_complete_reads_with_a_mismatched_digest(self, clock):
        clock.monotonic.return_value = 10
        path = self.receipt()
        receipt = json.loads(path.read_bytes())
        checkout = factory.reader_checkout_path(path)
        copies = path.with_suffix(".artifacts")
        copies.mkdir()
        large = copies / "large"
        with large.open("wb") as stream:
            stream.truncate(16 * 1024**2)
        large.chmod(0o600)
        info = large.stat()
        entries = [f"large:{i}" for i in range(8)]
        for entry in entries:
            receipt["retained_files"][entry] = dict(root=str(checkout), relative="large",
                st_dev=info.st_dev, st_ino=info.st_ino, sha256="0" * 64)
        last = self.base / "result"
        last.write_text("retained")
        receipt["report"]["artifacts"] = [*entries, str(last)]
        factory.save(path, receipt)
        self.assertEqual(self.listed(), [])
        receipt["report"]["artifacts"].pop(0)
        factory.save(path, receipt)
        self.assertEqual(self.listed(), [dict(scratch=receipt["scratch"], receipt=str(path))])

    def test_partial_reads_consume_bytes_without_a_digest(self):
        path = self.base / "report.txt"
        path.write_bytes(b"complete report")
        for failure in (b"", OSError("read failed")):
            with self.subTest(failure=failure), path.open("rb") as source:
                budget = factory.ReportHashBudget(2, 100, 5)
                self.assertTrue(budget.attempt())
                with patch.object(os, "read", side_effect=[b"complete", failure]):
                    if isinstance(failure, OSError):
                        with self.assertRaises(OSError):
                            budget.digest(source.fileno(), path.stat(), path)
                    else:
                        self.assertIsNone(budget.digest(source.fileno(), path.stat(), path))
                self.assertEqual((budget.files, budget.remaining), (1, 92))
        with patch.object(factory.time, "monotonic", side_effect=[0, 1, 1, 6]), path.open("rb") as source:
            budget = factory.ReportHashBudget(2, 100, 5)
            self.assertTrue(budget.attempt())
            with patch.object(os, "read", return_value=b"first"):
                self.assertIsNone(budget.digest(source.fileno(), path.stat(), path))
            self.assertEqual(budget.remaining, 95)

    def test_skips_unreadable_task_and_reads_legacy_task_without_migrating(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch)
        task_path = self.state / "task.json"
        record = json.loads(task_path.read_text())
        del record["assignments"]["qa"]["interpreter"]
        record["schema_version"] = 10
        factory.save(task_path, record)
        before = task_path.read_bytes()
        factory.save(self.state / "broken.json", "invalid JSON")
        self.assertEqual(self.listed(), [])
        self.assertEqual(task_path.read_bytes(), before)

    def test_lists_receipt_from_per_user_runtime_without_relocation(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch)
        task_path = self.state / "task.json"
        record = json.loads(task_path.read_text())
        home = self.base / "runtime-install"
        record["assignments"]["qa"]["request"]["command"][3] = str(home / "skill/runtime/claude_worker.py")
        factory.save(task_path, record)
        with patch.object(factory, "runtime_home", return_value=home):
            self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])
        self.assertFalse(home.exists())

    def test_lists_historical_python_and_canonical_worker_through_runtime_alias(self):
        scratch = self.scratch()
        receipt = self.receipt(scratch)
        task_path = self.state / "task.json"
        record = json.loads(task_path.read_text())
        home = self.base / "runtime-install"
        home.mkdir()
        alias = self.base / "data-home"
        alias.symlink_to(home, target_is_directory=True)
        command = record["assignments"]["qa"]["request"]["command"]
        record["assignments"]["qa"]["interpreter"]["invocation"] = "/fixture/historical/python3.11"
        command[0] = "/fixture/historical/python3.11"
        command[3] = str(home / "skill/runtime/claude_worker.py")
        factory.save(task_path, record)
        with patch.object(factory, "runtime_home", return_value=alias):
            self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])

    def test_excludes_running_process(self):
        self.receipt()
        self.assertEqual(self.listed(lookup=lambda pid: pid), [])

    def test_excludes_reused_pid(self):
        self.receipt(native_process=dict(pid=201, start_time=100, scope="fixture-scope"))
        with patch.object(factory, "native_start_time", return_value=400):
            self.assertEqual(self.listed(lookup=lambda pid: pid), [])

    def test_excludes_missing_result(self):
        self.receipt(report=None)
        self.assertEqual(self.listed(), [])

    def test_excludes_invalid_receipt_reports(self):
        scratch = self.scratch()
        for report in (["Evidence"], dict(outcome="unknown", artifacts=["Evidence"]),
                       dict(outcome="approved"), dict(outcome="approved", artifacts="Evidence"),
                       dict(outcome="approved", artifacts=["Evidence", 1])):
            with self.subTest(report=report):
                self.receipt(scratch, report=report)
                self.assertEqual(self.listed(), [])

    def test_excludes_arbitrary_and_hard_linked_report_sidecars(self):
        sidecar = self.receipt(report=None).with_suffix(".report.txt")
        original = self.state / "unrelated.txt"
        factory.save(original, "Unrelated evidence\n")
        with self.subTest(sidecar="ordinary file"):
            factory.save(sidecar, "Arbitrary evidence\n")
            self.assertEqual(self.listed(), [])
        sidecar.unlink()
        with self.subTest(sidecar="hard link"):
            os.link(original, sidecar)
            self.assertEqual(self.listed(), [])

    def test_excludes_unrecorded_artifact(self):
        path = self.receipt(report=None)
        retained = path.with_suffix(".artifacts") / "nested"
        retained.mkdir(parents=True)
        (retained / "review.md").write_text("Unrecorded artifact")
        self.assertEqual(self.listed(), [])

    def test_excludes_symlinked_artifact(self):
        path = self.receipt(report=None)
        path.with_suffix(".artifacts").mkdir()
        (path.with_suffix(".artifacts") / "link").symlink_to(path)
        self.assertEqual(self.listed(), [])

    def test_excludes_missing_finish_time(self):
        self.receipt(finished_at=None)
        self.assertEqual(self.listed(), [])

    def test_excludes_finish_before_creation(self):
        self.receipt(finished_at=999)
        self.assertEqual(self.listed(), [])

    def test_excludes_missing_creation_time(self):
        self.receipt(created_at=None)
        self.assertEqual(self.listed(), [])

    def test_excludes_nonterminal_receipt(self):
        self.receipt(state="starting")
        self.assertEqual(self.listed(), [])

    def test_retained_uncollected_failed_and_lost_attempts_remain_eligible(self):
        scratch = self.scratch()
        for state in ("failed", "lost"):
            with self.subTest(state=state):
                receipt = self.receipt(scratch, state=state, exit_code=17)
                self.assertEqual(self.listed(), [dict(scratch=str(scratch), receipt=str(receipt))])

    def test_excludes_successful_removal(self):
        self.receipt(scratch_removed=True)
        self.assertEqual(self.listed(), [])

    def test_excludes_absent_removal_status(self):
        path = self.receipt()
        receipt = json.loads(path.read_text())
        del receipt["scratch_removed"]
        factory.save(path, receipt)
        self.assertEqual(self.listed(), [])

    def test_excludes_null_removal_status(self):
        self.receipt(scratch_removed=None)
        self.assertEqual(self.listed(), [])

    def test_excludes_string_removal_status(self):
        self.receipt(scratch_removed="false")
        self.assertEqual(self.listed(), [])

    def test_excludes_integer_removal_status(self):
        self.receipt(scratch_removed=0)
        self.assertEqual(self.listed(), [])

    def test_excludes_array_removal_status(self):
        self.receipt(scratch_removed=[])
        self.assertEqual(self.listed(), [])

    def test_excludes_legacy_receipt(self):
        self.receipt(schema_version=4)
        self.assertEqual(self.listed(), [])

    def test_excludes_missing_start_fingerprint_without_observed_exit(self):
        self.receipt(native_process=dict(pid=201, start_time=None, scope="fixture-scope"))
        self.assertEqual(self.listed(), [])

    def test_excludes_absent_start_time_key_with_observed_exit(self):
        self.receipt(native_process=dict(pid=201, scope="fixture-scope"), exit_code=0)
        self.assertEqual(self.listed(), [])

    def test_excludes_different_process_scope(self):
        self.receipt(native_process=dict(pid=201, start_time=400, scope="other-scope"))
        self.assertEqual(self.listed(), [])

    def test_excludes_uninspectable_process(self):
        self.receipt()
        self.assertEqual(self.listed(lookup=PermissionError), [])

    def test_excludes_foreign_owned_scratch(self):
        scratch = self.scratch()
        self.receipt(scratch)
        lstat = Path.lstat

        def ownership(path):
            info = lstat(path)
            if path == scratch:
                fields = list(info)
                fields[4] = os.getuid() + 1
                return os.stat_result(fields)
            return info

        with patch.object(Path, "lstat", ownership):
            self.assertEqual(self.listed(), [])

    def test_excludes_replacement_scratch_directory(self):
        scratch = self.scratch()
        self.receipt(scratch)
        original = scratch.rename(self.base / "original-scratch")
        scratch.mkdir()
        self.assertNotEqual(scratch.stat().st_ino, original.stat().st_ino)
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_with_wrong_device(self):
        scratch = self.scratch()
        info = scratch.stat()
        self.receipt(scratch, scratch_identity=dict(st_dev=info.st_dev + 1, st_ino=info.st_ino))
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_without_recorded_identity(self):
        self.receipt(scratch_identity=None)
        self.assertEqual(self.listed(), [])

    def test_excludes_named_legacy_folder(self):
        self.receipt(self.scratch("named-legacy-folder"))
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_without_receipt(self):
        self.scratch()
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_outside_root(self):
        outside = self.base / "sf-outside"
        outside.mkdir()
        self.receipt(outside)
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_symlink_to_valid_directory_inside_root(self):
        scratch = self.scratch()
        info = scratch.stat()
        link = self.root / "sf-link"
        link.symlink_to(scratch, target_is_directory=True)
        self.receipt(link, scratch_identity=dict(st_dev=info.st_dev, st_ino=info.st_ino))
        self.assertEqual(self.listed(), [])

    def test_excludes_scratch_below_symlinked_parent(self):
        outside = self.base / "sf-outside"
        outside.mkdir()
        alias = self.root / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        self.receipt(alias / outside.name)
        self.assertEqual(self.listed(), [])

    def test_excludes_symlinked_receipt(self):
        path = self.receipt()
        original = path.rename(self.base / "original.json")
        path.symlink_to(original)
        self.assertEqual(self.listed(), [])

    def test_records_real_child_identity_before_proving_the_pid_is_gone(self):
        scratch = self.scratch()
        path = self.receipt(scratch)
        receipt = json.loads(path.read_text())
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE) as child:
            factory.record_native_process(path, receipt, child)
            observed = json.loads(path.read_text())["native_process"]
            self.assertEqual(observed["pid"], child.pid)
            self.assertGreater(observed["start_time"], 0)
            self.assertEqual(observed["scope"], factory.process_scope())
            self.assertEqual(factory.abandoned_scratch(self.state), [])
            factory.save(path, {**receipt, "native_process": {**observed, "start_time": observed["start_time"] + 1}})
            self.assertEqual(factory.abandoned_scratch(self.state), [])
            factory.save(path, receipt)
            child.communicate()
        self.assertEqual(factory.abandoned_scratch(self.state), [dict(scratch=str(scratch), receipt=str(path))])

    def test_linux_proc_identity_fixture_uses_kernel_ticks_and_pid_namespace(self):
        pid = 201
        boot_id = "ab4c3671-ac54-413b-8860-b66eb2fd0d16"
        boot = self.base / "boot_id"
        boot.write_text(boot_id + "\n")
        proc_stat = self.base / "stat"
        proc_stat.write_text(" ".join([str(pid), "(name with a ) parenthesis)", "S",
                                       str(os.getpid()), *(["0"] * 17), "404"]) + "\n")
        files = {"/proc/sys/kernel/random/boot_id": boot, f"/proc/{pid}/stat": proc_stat}
        with patch.object(factory.sys, "platform", "linux"), patch.object(factory, "Path", side_effect=files.__getitem__), \
                patch.object(factory.os, "readlink", return_value="pid:[1234]") as readlink:
            self.assertEqual(factory.process_scope(), f"linux:{boot_id}:pid:[1234]")
            self.assertEqual(factory.native_start_time(pid), 404)
        readlink.assert_called_once_with("/proc/self/ns/pid")

    def test_saves_start_time_when_child_exits_during_failed_scope_lookup(self):
        path = self.receipt()
        receipt = json.loads(path.read_text())
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE) as child:
            def failed_scope():
                child.communicate(timeout=5)
                raise OSError("Scope unavailable")

            with patch.object(factory, "process_scope", side_effect=failed_scope):
                factory.record_native_process(path, receipt, child)
            self.assertEqual(child.returncode, 0)
        observed = json.loads(path.read_text())["native_process"]
        self.assertIsInstance(observed["start_time"], int)
        self.assertGreater(observed["start_time"], 0)
        self.assertIsNone(observed["scope"])


class NativeRetainedResults(unittest.TestCase):
    """Replay each provider's original final event through launch, worker, collection and listing."""

    def check_reports(self, fixture_type, run):
        from test_codex_worker import FakeProcess
        for case in ("inline", "file", "missing-file", "copy", "scratch", "result-locator", "notice",
                     "direction", "markdown", "ambiguous", "uncollected", "legacy"):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                invocation = Path(tmp) / "custom-python"
                invocation.symlink_to(sys.executable)
                with patch.object(sys, "executable", str(invocation)):
                    fixture = fixture_type()
                    fixture.setUp()
                    try:
                        target = fixture.root.resolve() / "result with spaces.md"
                        target.write_text("Offline verification passed.\n")
                        scratch = factory.new_scratch()
                        scratch_file = scratch / "report.md"
                        scratch_file.write_text("Report inside the native scratch")
                        artifact = {
                            "inline": json.dumps(dict(observation="The worker records its scratch in worker.py",
                                                      citations=["missing.py:1", "absent.py:2"])),
                            "file": "File: " + str(target),
                            "missing-file": "File: " + str(target.with_suffix(".missing")),
                            "direction": f"Result: Read {scratch_file} for the full report.",
                            "markdown": f"Result: See [report](<{scratch_file}>) for the full report.",
                            "copy": "File: alias/../nested/proof.md",
                            "scratch": "File: " + str(scratch_file),
                            "result-locator": "Result: " + str(scratch_file),
                            "notice": f"[advisory] Verified the report was written successfully. [Report]({scratch_file}:1)",
                            "ambiguous": "[defect] Confirmed the report is saved as MISSING",
                            "uncollected": '{"observation":"The reader observed a failed cleanup."}',
                            "legacy": str(target),
                        }[case]
                        entries = [artifact]
                        if case == "inline":
                            entries = ["  Historical result — checked  ", artifact, artifact]
                        report = dict(outcome="approved", artifacts=entries)
                        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                                              stdin=subprocess.PIPE) as child:
                            with ExitStack() as effects:
                                effects.enter_context(patch.object(FakeProcess, "pid", child.pid))
                                effects.enter_context(patch.object(factory, "new_scratch", return_value=scratch))
                                effects.enter_context(patch.object(factory, "remove_scratch", return_value=False))
                                if case == "uncollected":
                                    effects.enter_context(patch.object(factory, "retain_report_files",
                                                                      side_effect=OSError("fixture I/O failure")))
                                if case == "copy":
                                    reader_checkout = factory.reader_checkout
                                    @contextmanager
                                    def checkout_with_report(*args, **kwargs):
                                        with reader_checkout(*args, **kwargs) as (checkout, admin, source):
                                            nested = checkout / "nested"
                                            nested.mkdir()
                                            (checkout / "alias").symlink_to(nested, target_is_directory=True)
                                            (nested / "proof.md").write_text("Private reader result")
                                            yield checkout, admin, source
                                    effects.enter_context(patch.object(factory, "reader_checkout", checkout_with_report))
                                receipt, collect = run(fixture, report)
                            child.communicate()
                        receipt_path = fixture.args.receipt
                        self.assertEqual(receipt["report"], report, receipt["error"])
                        self.assertIs(receipt["scratch_removed"], False)
                        if case == "uncollected":
                            self.assertEqual(receipt["state"], "failed")
                        else:
                            self.assertEqual(receipt["state"], "completed", receipt["error"])
                            if case == "legacy":
                                del receipt["interpreter"], receipt["retained_files"]
                                receipt.pop("conversation_root", None)
                                factory.save(receipt_path, {**receipt, "schema_version": 7})
                            original = receipt_path.read_bytes()
                            collect()
                            self.assertEqual(receipt_path.read_bytes(), original)
                            result, = factory.load_task(fixture.path)["assignments"].values()
                            self.assertEqual(result["result"]["artifacts"], [str(receipt_path), *dict.fromkeys(entries)])
                            self.assertEqual(receipt_path.with_suffix(".report.txt").read_bytes(),
                                             ("\n".join(entries) + "\n").encode())
                            if case not in ("copy", "legacy"):
                                self.assertEqual(receipt["retained_files"], {})

                        if case == "inline":
                            self.check_foreign_launch(fixture, invocation)
                        expected = case in ("inline", "file", "copy", "uncollected")

                        def check_listing(eligible):
                            with patch.object(subprocess, "Popen", side_effect=AssertionError("executed archived argv")), \
                                    patch.object(factory, "process_scope", return_value=receipt["native_process"]["scope"]):
                                self.assertEqual(bool(factory.abandoned_scratch(fixture.root)), eligible)

                        def snapshot():
                            files = [receipt_path, fixture.path, target, fixture.args.events, scratch_file]
                            files.extend(receipt_path.with_suffix(".checkout-artifacts").glob("*"))
                            files.extend(receipt_path.parent.glob(receipt_path.with_suffix(".report.txt").name))
                            return {p: (p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns, p.read_bytes())
                                    for p in files}

                        before = snapshot()
                        self.assertEqual(listing_cli(fixture.root),
                                         [dict(scratch=str(scratch), receipt=str(receipt_path))] if expected else [])
                        with patch.object(sys, "executable", "/bin/sh"):
                            for search_path in (os.environ["PATH"], os.defpath):
                                with patch.dict(os.environ, PATH=search_path):
                                    check_listing(expected)
                            self.assertEqual(before, snapshot())
                            if case == "copy":
                                copy, = receipt_path.with_suffix(".checkout-artifacts").iterdir()
                                self.assertEqual(copy.read_text(), "Private reader result")
                                before_copy = copy.stat()
                                copy.write_text("Other reader contents")
                                os.utime(copy, ns=(before_copy.st_atime_ns, before_copy.st_mtime_ns))
                                check_listing(False)
                                copy.write_text("Private reader result")
                                check_listing(True)
                                copy.rename(copy.with_name("moved.md"))
                                factory.save(copy, "Same name, different file")
                                check_listing(False)
                            if case in ("direction", "markdown"):
                                scratch_file.rename(scratch_file.with_suffix(".moved"))
                                check_listing(False)
                            if case == "file":
                                target.unlink()
                                check_listing(False)
                            if case == "inline":
                                invocation.unlink()
                                invocation.symlink_to("/bin/sh")
                                check_listing(False)
                    finally:
                        fixture.doCleanups()

    def check_foreign_launch(self, fixture, invocation):
        from test_rex_launcher import FakeRex
        # Replay the same job, pins and receipt in a separate launch record with a foreign interpreter.
        state = fixture.root / "foreign-launch"
        state.mkdir()
        task = state / "task.json"
        record = factory.load_task(fixture.path)
        identity, = record["assignments"]
        request = record["assignments"][identity]["request"]
        record.update(assignments={}, rex=None, session_id=None)
        factory.save(task, record)
        impostor = state / "bin/python3.11"
        impostor.parent.mkdir()
        impostor.symlink_to("/bin/sh")
        stdlib = state / "lib/python3.11"
        stdlib.mkdir(parents=True)
        (stdlib / "os.py").touch()
        original = task.read_bytes()
        with patch.object(factory, "Rex", FakeRex), patch.object(FakeRex, "sessions", []):
            with self.assertRaisesRegex(factory.FactoryError, "original Python"):
                factory.launch(task, task="task", assignment_id=identity, role=request["role"],
                    goal=request["goal"], cwd=request["cwd"], command=[str(impostor), *request["command"][1:]],
                    purpose="work", binary=str(invocation), server="unix:///test.sock")
        self.assertEqual(task.read_bytes(), original)
        impostor.unlink()
        impostor.symlink_to(invocation)
        self.assertEqual(factory.abandoned_scratch(state), [])

    def test_codex(self):
        from test_codex_worker import NativeResults, FakeProcess, events
        def run(fixture, report):
            record, assignment = fixture.launch_assignment(execute=False)
            stream = events()
            stream[-2]["item"]["text"] = json.dumps(report)
            fixture.process = FakeProcess(stream)
            receipt = fixture.execute()
            def collect():
                with patch.object(factory, "status", return_value=dict(state="exited")):
                    factory.collect_codex(fixture.path, "qa", fixture.rex, record, assignment)
            return receipt, collect
        self.check_reports(NativeResults, run)

    def test_claude(self):
        from test_claude_execution import ClaudeWorker, FakeProcess
        def run(fixture, report):
            fixture.events[-4]["message"]["content"][0]["input"] = report
            fixture.events[-1].update(structured_output=report, result=json.dumps(report))
            fixture.process = FakeProcess(fixture.events)
            return fixture.execute(), fixture.collect
        self.check_reports(ClaudeWorker, run)

    def test_grok(self):
        from test_grok_worker import GrokWorker, FakeProcess
        def run(fixture, report):
            fixture.events[-1]["structuredOutput"] = report
            fixture.process = FakeProcess(fixture.events)
            return fixture.execute(), fixture.collect
        self.check_reports(GrokWorker, run)

    def test_antigravity(self):
        from test_antigravity_worker import AntigravityWorker, stream
        def run(fixture, report):
            events = stream(fixture.repo)
            cast(dict, events[-1]["result"]).update(structured_output=report, response=json.dumps(report))
            receipt = fixture.execute(fixture.args, events=events)
            return receipt, lambda: fixture.collect("inspect")
        self.check_reports(AntigravityWorker, run)
