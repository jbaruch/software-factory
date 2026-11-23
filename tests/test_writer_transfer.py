"""Deterministic offline tests for one-hop writer provenance transfer.

Two real temporary Git clones with different common_dir and markers, two state directories,
two fake Rex hosts.  No real Rex/provider calls, no network, no actual transfers.
"""
import runtime_path  # noqa: F401  Must precede runtime imports.
import copy
from contextlib import contextmanager, redirect_stderr, redirect_stdout
import errno
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import factory
import writer_transfer
import integration
import release_evidence
from test_integration import EvidenceRex, GitHub, NativeReviewFixture


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git(repo, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Test", "GIT_AUTHOR_EMAIL": "test@test.invalid",
           "GIT_COMMITTER_NAME": "Test", "GIT_COMMITTER_EMAIL": "test@test.invalid",
           "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
           "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00"}
    result = subprocess.run([factory.git_binary(), "-C", str(repo), *args],
                            capture_output=True, text=True, env=env)
    return result


def _admit(state_dir, task, repo, goal="Deliver fixture"):
    path = Path(state_dir) / (task + ".json")
    factory.admit(path, task=task, goal=goal, repository=str(repo),
                  acceptance=["original-requirements"], corrections=2, investigations=1)
    return path


class FakeSourceRex(EvidenceRex):
    host = "source-host"


class FakeDestRex(EvidenceRex):
    host = "dest-host"


def check_provider_export_brief(case, args, replacement):
    """Exercise export on the collected writer from a provider's offline fixture."""
    path = case.path
    record = factory.load_task(path)
    rex = case.rex
    exits = {assignment["block_id"]: 0 for assignment in record["assignments"].values()}
    with patch.dict(type(rex).exits, exits):
        factory.tidy(path, rex, record)
    factory.hold_task(path, "Source transfer hold", request_id="hold1", expected_revision=None)
    case.git("remote", "add", "origin", "https://github.com/fixture/project.git")
    command = ["--state-dir", str(path.parent), "export-writer", "--task", path.stem,
               "--id", "implement", "--to-host", "dest-host", "--to-task", "dst-task", "--out"]
    bundles = tempfile.TemporaryDirectory()
    case.addCleanup(bundles.cleanup)
    valid = Path(bundles.name) / "valid.bundle"
    with patch.object(factory, "Rex", type(rex)), redirect_stdout(StringIO()):
        case.assertEqual(factory.main([*command, str(valid)]), 0)
    bundle = json.loads(valid.read_bytes())
    for name in ("brief", "events", "receipt"):
        case.assertEqual(bundle["blobs"][name]["content"].encode(), getattr(args, name).read_bytes())
    receipt = json.loads(args.receipt.read_bytes())
    identity = receipt.get("native_session") or receipt["conversation_id"]
    case.assertEqual(bundle["contributors"], [dict(id="implement", context=identity)])
    case.assertNotEqual(args.brief.read_bytes(), replacement)
    args.brief.write_bytes(replacement)
    files = (path, args.brief, args.events, args.receipt)
    before = [file.read_bytes() for file in files]
    out = Path(bundles.name) / "damaged.bundle"
    with patch.object(factory, "Rex", type(rex)), redirect_stderr(StringIO()) as errors, redirect_stdout(StringIO()) as output:
        case.assertEqual(factory.main([*command, str(out)]), 1)
    case.assertEqual(output.getvalue(), "")
    case.assertIn(str(args.brief), errors.getvalue())
    case.assertIn("frozen digest", errors.getvalue())
    case.assertIn("Restore the original file", errors.getvalue())
    case.assertNotIn("fixture payload", errors.getvalue())
    case.assertNotIn("Traceback", errors.getvalue())
    case.assertFalse(out.exists())
    case.assertEqual([file.read_bytes() for file in files], before)


class TransferBase(NativeReviewFixture):
    named_requirements: str | None = None

    def setUp(self):
        super().setUp()
        self.tmp = self.root
        self.src_repo = self.repo
        self.src_state = self.tmp / "src-state"
        self.dst_state = self.tmp / "dst-state"
        self.bundle_dir = self.tmp / "bundles"
        for directory in (self.src_state, self.dst_state, self.bundle_dir):
            directory.mkdir()
        for rex in (FakeSourceRex, FakeDestRex):
            rex.sessions, rex.calls = [], []
        _git(self.src_repo, "config", "commit.gpgsign", "false").check_returncode()
        _git(self.src_repo, "config", "core.hooksPath", "/dev/null").check_returncode()
        _git(self.src_repo, "remote", "add", "origin", "https://github.com/fixture/project.git").check_returncode()
        self.src_path = self.path = _admit(self.src_state, "src-task", self.repo)
        self.base = self.head = factory.git_commit(str(self.repo), "HEAD")
        requirements = self.named_requirements or self.base
        factory.set_inputs(self.path, candidate=self.base, requirements=requirements)
        with patch.object(factory, "Rex", FakeSourceRex):
            writer = self.review("w1", "backend", frozen=False, writer=True, outcome="completed",
                                 purpose="correction", change="Candidate change")
            self.candidate = writer["result"]["candidate"]
            record = factory.load_task(self.path)
            factory.tidy(self.path, factory.connect(record), record)
        factory.hold_task(self.path, "Source transfer hold", request_id="hold1", expected_revision=None)
        self.evidence_paths = [Path(writer["brief"]), self.native.args.events, self.native.args.receipt]

        self.dst_repo = self.tmp / "dst-repo"
        _git(self.tmp, "clone", "--quiet", "--no-local", str(self.src_repo), str(self.dst_repo)).check_returncode()
        _git(self.dst_repo, "remote", "set-url", "origin", "https://github.com/fixture/project.git").check_returncode()
        self.dst_path = _admit(self.dst_state, "dst-task", self.dst_repo)
        factory.set_inputs(self.dst_path, candidate=self.candidate, requirements=requirements)
        self.path, self.repo, self.head = self.dst_path, self.dst_repo, self.candidate
        dest_rex = patch.object(factory, "Rex", FakeDestRex)
        dest_rex.start()
        self.addCleanup(dest_rex.stop)

    @contextmanager
    def source_unchanged(self):
        paths = [self.src_path, *self.evidence_paths]

        def snapshot():
            return [(path.exists(), path.read_bytes() if path.is_file() else None) for path in paths]

        before = snapshot()
        try:
            yield
        finally:
            self.assertEqual(before, snapshot())

    def _do_export(self, out_name="bundle.json"):
        out_path = self.bundle_dir / out_name
        with self.source_unchanged(), patch.object(factory, "Rex", FakeSourceRex):
            with factory.task_lock(self.src_state, "src-task") as p:
                source_host, sha256 = writer_transfer.export_writer(
                    p, assignment_id="w1", to_host="dest-host", to_task="dst-task", out_path=str(out_path))
        return out_path, source_host, sha256

    def _do_import(self, out_path, sha256, *, from_host="source-host", host="dest-host"):
        with self.source_unchanged(), factory.task_lock(self.dst_state, "dst-task") as p:
            return writer_transfer.import_writer(p, file_path=str(out_path), from_host=from_host, sha256=sha256,
                                                 whoami_host_id=host)

    def _src_record(self):
        with factory.task_lock(self.src_state, "src-task") as p:
            return factory.load_task(p)

    def _dst_record(self):
        with factory.task_lock(self.dst_state, "dst-task") as p:
            return factory.load_task(p)


class TestTransferRequirements(TransferBase):
    named_requirements = "fixture/project#7@2026-11-13T03:37:26Z"

    def test_transfer_preserves_named_requirements(self):
        out, _, digest = self._do_export()
        factory.set_inputs(self.dst_path, candidate=self.candidate, requirements="changed")
        with self.assertRaisesRegex(factory.FactoryError, "requirements do not match"):
            self._do_import(out, digest)
        factory.set_inputs(self.dst_path, candidate=self.candidate, requirements=self.named_requirements)
        record = self._do_import(out, digest)
        self.assertEqual(record["writer_origin"]["requirements"], self.named_requirements)


class TestExport(TransferBase):
    def test_export_success_produces_valid_bundle(self):
        """Export produces a bundle with the correct digest and structure."""
        out_path, source_host, sha256 = self._do_export()
        self.assertTrue(out_path.exists())
        raw = out_path.read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(), sha256)
        self.assertEqual(source_host, "source-host")
        bundle = json.loads(raw)
        self.assertEqual(bundle["schema_version"], 1)
        self.assertEqual(bundle["source"]["host_id"], "source-host")
        self.assertEqual(bundle["destination"]["host_id"], "dest-host")
        self.assertEqual(bundle["destination"]["task"], "dst-task")
        self.assertEqual(bundle["candidate"], self.candidate)
        for name, path in zip(("brief", "events", "receipt"), self.evidence_paths):
            raw = path.read_bytes()
            self.assertEqual(bundle["blobs"][name], {
                "path": str(path), "content": raw.decode("utf-8"), "digest": hashlib.sha256(raw).hexdigest()})
        identity = json.loads(self.native.args.receipt.read_bytes())["thread_id"]
        self.assertEqual(bundle["contributors"], [{"id": "w1", "context": identity}])
        origin = self._do_import(out_path, sha256)["writer_origin"]
        self.assertEqual(origin["contributors"], bundle["contributors"])
        self.assertEqual(Path(origin["archive"]).read_bytes(), out_path.read_bytes())

    def assert_export_error(self, *expected_parts, existing_bundle=None):
        out = self.bundle_dir / "bundle.json"
        with patch.object(factory, "Rex", FakeSourceRex), redirect_stdout(StringIO()) as output, redirect_stderr(StringIO()) as errors:
            code = factory.main(["--state-dir", str(self.src_state), "export-writer", "--task", "src-task",
                                 "--id", "w1", "--to-host", "dest-host", "--to-task", "dst-task", "--out", str(out)])
        self.assertEqual(code, 1)
        self.assertEqual(output.getvalue(), "")
        message = errors.getvalue()
        for expected in expected_parts:
            self.assertIn(str(expected), message)
        for private in ("fixture payload", "0xff", "Traceback"):
            self.assertNotIn(private, message)
        if existing_bundle is None:
            self.assertFalse(out.exists())
        else:
            self.assertEqual(out.read_bytes(), existing_bundle)

    def test_source_record_read_and_decode_failures(self):
        for content, cause in ((b'{"fixture payload":', "Invalid JSON"), (b"fixture payload\xff", "Invalid UTF-8")):
            with self.subTest(cause=cause):
                self.src_path.write_bytes(content)
                self.assert_export_error(self.src_path, cause, "Restore the original record")
                self.assertEqual(self.src_path.read_bytes(), content)
        self.src_path.unlink()
        self.assert_export_error(self.src_path, os.strerror(errno.ENOENT), "Restore access to the original record")
        self.assertFalse(self.src_path.exists())

    def test_export_refuses_schema_10_swap_without_migrating(self):
        legacy = self._src_record()
        legacy["schema_version"] = 10
        legacy.pop("acceptance", None)
        replacement = json.dumps(legacy, indent=2).encode()
        read = factory.read_record
        swapped = False

        def swap_after_read(path, **kwargs):
            nonlocal swapped
            content = read(path, **kwargs)
            if path == self.src_path and not swapped:
                self.src_path.write_bytes(replacement)
                swapped = True
            return content

        with patch.object(factory, "read_record", swap_after_read), patch.object(factory, "save", wraps=factory.save) as save:
            self.assert_export_error(self.src_path, "changed during export")
            save.assert_not_called()
        self.assertEqual(self.src_path.read_bytes(), replacement)

    def test_export_refuses_unsupported_schema_without_saving(self):
        record = self._src_record()
        record["schema_version"] = 13
        self.src_path.write_bytes(json.dumps(record).encode())
        with self.source_unchanged(), patch.object(factory, "save", wraps=factory.save) as save:
            self.assert_export_error("schema 11")
            save.assert_not_called()

    def test_export_applies_owner_validation_to_schema_11(self):
        record = self._src_record()
        record["session_label"] = "unrelated-session"
        self.src_path.write_bytes(json.dumps(record).encode())
        with self.source_unchanged():
            self.assert_export_error("Invalid Rex session identity")

    def test_export_rechecks_source_before_successful_replay(self):
        out, _, _ = self._do_export()
        existing_bundle = out.read_bytes()
        original = self.src_path.read_bytes()
        changed = original.replace(b"\n", b"\r\n")
        self.assertNotEqual(original, changed)
        open_bundle = writer_transfer._private_bundle

        @contextmanager
        def change_during_replay(path):
            with open_bundle(path) as bundle:
                self.src_path.write_bytes(changed)
                yield bundle

        with patch.object(writer_transfer, "_private_bundle", change_during_replay), patch.object(factory, "save") as save:
            self.assert_export_error(self.src_path, "changed during export", existing_bundle=existing_bundle)
            save.assert_not_called()
        self.assertEqual(self.src_path.read_bytes(), changed)

    def test_export_reports_first_damaged_original(self):
        # Damage all three before export. Restore each in turn to expose the next failure.
        brief, events, receipt = self.evidence_paths
        originals = {path: path.read_bytes() for path in (receipt, events, brief)}
        for path in originals:
            path.write_bytes(b"fixture payload\xff")
        for path, original in originals.items():
            with self.subTest(file=path.name, damage="encoding"), self.source_unchanged():
                self.assert_export_error(path, "Invalid UTF-8", "Restore the original file")
            path.unlink()
            with self.subTest(file=path.name, damage="missing"), self.source_unchanged():
                self.assert_export_error(path, os.strerror(errno.ENOENT), "Restore access to the original file")
            path.mkdir()
            with self.subTest(file=path.name, damage="unreadable"), self.source_unchanged():
                self.assert_export_error(path, os.strerror(errno.EISDIR), "Restore access to the original file")
            path.rmdir()
            path.write_bytes(original)
            path.chmod(0o600)

    def test_export_refuses_redirected_originals(self):
        original = self._src_record()
        for historical in (False, True):
            for name in ("receipt", "events", "brief"):
                record = copy.deepcopy(original)
                writer = record["assignments"]["w1"]
                command = writer["request"]["command"]
                if historical:
                    del command[1:3]
                alternate = str(self.tmp / "fixture payload")
                if name == "brief":
                    writer["brief"] = alternate
                else:
                    command[command.index("--" + name) + 1] = alternate
                factory.save(self.src_path, record)
                with self.subTest(file=name, historical=historical), self.source_unchanged():
                    self.assert_export_error("w1", "bindings", "Restore the original")

    def test_archival_file_damage_after_native_proof(self):
        contributor_context = integration.contributor_context
        brief, events, receipt = self.evidence_paths
        for path, content, cause, recovery in (
                (brief, b"fixture payload", "changed during validation", "Restore the original file"),
                (events, b"fixture payload\xff", "Invalid UTF-8", "Restore the original file"),
                (receipt, None, os.strerror(errno.ENOENT), "Restore access to the original file")):
            original = path.read_bytes()

            def damage_after_proof(*args):
                context = contributor_context(*args)
                if content is None:
                    path.unlink()
                else:
                    path.write_bytes(content)
                return context

            with self.subTest(file=path.name), patch.object(integration, "contributor_context", damage_after_proof):
                self.assert_export_error(path, cause, recovery)
            path.write_bytes(original)
            path.chmod(0o600)

    def test_export_verifies_bytes_captured_before_restore(self):
        brief, events, receipt = self.evidence_paths
        read = factory.read_record
        for path, damage, diagnostic in (
                (receipt, b'{"fixture payload": 1}', ("Native receipt", "inspect the original artifacts")),
                (events, b'{"fixture payload": 1}', ("event record", "inspect the original stream")),
                (brief, b"fixture payload", (brief, "frozen digest", "Restore the original file"))):
            original = path.read_bytes()

            def restore_after_capture(file, **kwargs):
                # Only the archive reads see the damaged bytes. Every path-based
                # native read, including contributor corroboration, sees the original.
                if file == path and kwargs.get("max_bytes") == writer_transfer.BUNDLE_MAX_BYTES:
                    path.write_bytes(damage)
                    try:
                        return read(file, **kwargs)
                    finally:
                        path.write_bytes(original)
                return read(file, **kwargs)

            with self.subTest(file=path.name), self.source_unchanged(), patch.object(factory, "read_record", restore_after_capture):
                self.assert_export_error(*diagnostic)

    def test_export_refuses_crlf_drift(self):
        brief = self.evidence_paths[0]
        original = brief.read_bytes()
        drifted = original.replace(b"\n", b"\r\n")
        self.assertNotEqual(drifted, original)
        brief.write_bytes(drifted)
        with self.source_unchanged():
            self.assert_export_error(brief, "frozen digest", "Restore the original file")

    def test_export_refuses_selected_contributor_disagreement(self):
        with self.source_unchanged(), patch.object(integration, "contributor_context", return_value="fixture payload"):
            self.assert_export_error("w1", "differs from the verified receipt", "Restore the original evidence")

    def test_export_idempotent_replay_returns_same_digest(self):
        """Moving the runtime preserves the exact archived writer command and evidence."""
        _, _, sha256a = self._do_export()
        with patch.object(factory, "ROOT", self.tmp / "new-runtime"):
            _, _, sha256b = self._do_export()
        self.assertEqual(sha256a, sha256b)

    def test_export_proves_an_omitted_historical_reader(self):
        self.path, self.repo, self.head = self.src_path, self.src_repo, self.candidate
        factory.hold_task(self.path, None, request_id="resume-reader")
        factory.set_inputs(self.path, candidate=self.candidate, requirements=self.base)
        with patch.object(factory, "Rex", FakeSourceRex):
            reader = self.review("reader", "qa", frozen=False)
            record = self._src_record()
            factory.tidy(self.path, factory.connect(record), record)
        factory.hold_task(self.path, "Reader settled", request_id="hold-reader")
        with patch.object(factory, "ROOT", self.tmp / "new-runtime"):
            out, _, _ = self._do_export("intact.bundle")
            self.assertEqual(json.loads(out.read_bytes())["contributors"], [dict(id="w1", context="context-w1")])
            Path(reader["brief"]).unlink()
            self.assert_export_error("src-task/reader", "original frozen native assignment brief")

    def test_export_refuses_different_existing_bytes(self):
        """Export refuses when the output file exists with different content."""
        out_path = self.bundle_dir / "bundle.json"
        out_path.write_bytes(b"different content")
        os.chmod(out_path, 0o600)
        with self.assertRaisesRegex(factory.FactoryError, "already exists with different content"):
            self._do_export()

    def test_export_refuses_unheld_source(self):
        """Export refuses when the source task is not held."""
        factory.hold_task(self.src_path, None, request_id="unhold1", expected_revision=None)
        with self.assertRaisesRegex(factory.FactoryError, "not held"):
            self._do_export()

    def test_export_refuses_same_destination_host(self):
        """Export refuses when to-host equals the source host."""
        with factory.task_lock(self.src_state, "src-task") as p:
            with self.assertRaisesRegex(factory.FactoryError, "same"):
                writer_transfer.export_writer(
                    p, assignment_id="w1", to_host="source-host", to_task="dst-task",
                    out_path=str(self.bundle_dir / "bundle.json"))

    def test_export_refuses_uncollected_writer(self):
        """Export refuses when the writer is not natively collected."""
        with factory.task_lock(self.src_state, "src-task") as p:
            record = factory.load_task(p)
            record["assignments"]["w1"]["native_collected"] = False
            factory.save(p, record)
        with self.assertRaisesRegex(factory.FactoryError, "not collected"):
            self._do_export()

    def test_export_requires_created_commit_and_exact_result(self):
        reader = factory.evidence_reader("codex")

        def historical_unchanged(*args, **kwargs):
            parsed, receipt, native = reader(*args, **kwargs)
            receipt["commit"]["state"] = "unchanged"
            return parsed, receipt, native

        with patch.object(factory, "evidence_reader", return_value=historical_unchanged), self.assertRaisesRegex(
                factory.FactoryError, "committed nothing"):
            self._do_export()
        record = self._src_record()
        record["assignments"]["w1"]["result"]["artifacts"].append("different result")
        factory.save(self.src_path, record)
        with self.assertRaisesRegex(factory.FactoryError, "differs from the complete original"):
            self._do_export()
        self.assertFalse((self.bundle_dir / "bundle.json").exists())

    def test_export_refuses_third_pair_and_accepts_produced_pair(self):
        record = self._src_record()
        record["inputs"] = dict(candidate=self.candidate, requirements=self.candidate)
        factory.save(self.src_path, record)
        with self.assertRaisesRegex(factory.FactoryError, "Selected task inputs changed"):
            self._do_export()
        record["inputs"]["requirements"] = self.base
        factory.save(self.src_path, record)
        self._do_export()

    def test_export_requires_settled_one_hop_source_and_known_contributors(self):
        original = self._src_record()
        for kind, message in (("pre-11", "schema 11"), ("re-export", "schema 11"),
                              ("active", "terminal observation"), ("failed", "not completed"),
                              ("unknown", "metadata|context|receipt|command")):
            with self.subTest(kind=kind):
                record = copy.deepcopy(original)
                writer = record["assignments"]["w1"]
                if kind == "pre-11":
                    record["schema_version"] = 10
                elif kind == "re-export":
                    record.update(schema_version=12, writer_origin={})
                elif kind == "active":
                    record["session_id"] = writer.pop("closed")["session_id"]
                elif kind == "failed":
                    writer["result"].update(outcome="blocked", candidate=self.base)
                else:
                    command = writer["request"]["command"].copy()
                    command[command.index("--receipt") + 1] = str(self.tmp / "missing-receipt")
                    record["assignments"]["prior"] = dict(state="abandoned",
                        request={**writer["request"], "command": command},
                        brief=writer["brief"], window_label="src-task:backend-prior-12345678",
                        abandoned=dict(reason="fixture", at=1000), result=None, native_collected=False)
                factory.save(self.src_path, record)
                with self.assertRaisesRegex(factory.FactoryError, message):
                    self._do_export()

    def test_source_record_unchanged_after_export(self):
        """The source task record is byte-identical before and after export."""
        with factory.task_lock(self.src_state, "src-task") as p:
            before = factory.read_record(p)
        self._do_export()
        with factory.task_lock(self.src_state, "src-task") as p:
            after = factory.read_record(p)
        self.assertEqual(before, after)


class TestImport(TransferBase):
    def setUp(self):
        super().setUp()
        self.out_path, self.source_host, self.sha256 = self._do_export()

    def test_judge_cap_combines_source_and_destination_grants(self):
        # Two historical grants already spent on the source.
        source = self._src_record()
        for n in range(2):
            source["allowance_events"].append(dict(id=f"grant-{n}", purpose="correction", previous_limit=2 + n,
                additional=1, reason="Source ruling", at=1000, judge=f"judge-{n}"))
        factory.save(self.src_path, source)
        out, _, digest = self._do_export("grants")
        self._do_import(out, digest)
        factory.hold_task(self.path, None, request_id="resume", expected_revision=None)
        for n in (2, 3):
            key = f"judge-{n}"
            factory.launch(self.path, task=self.path.stem, assignment_id=key, role="judge", goal="Judge",
                           cwd=str(self.repo), command=[sys.executable], purpose="work",
                           binary=sys.executable, server="unix:///fixture.sock")
            factory.record_result(self.path, assignment_id=key, candidate=self.head, requirements=self.base,
                                  outcome="completed", artifacts=["Fixture ruling"])
            args = dict(purpose="correction", additional=1, expected_limit=n, reason="Destination ruling",
                        request_id=f"grant-{n}", judge=key)
            if n == 2:
                factory.extend_allowance(self.path, **args)
            else:
                with self.assertRaisesRegex(factory.FactoryError, "already added 3"):
                    factory.extend_allowance(self.path, **args)
        record = self._dst_record()
        self.assertEqual(len(record["allowance_events"]), 1)
        third = record["allowance_events"][0]
        record["allowance_events"].append({**third, "id": "grant-3", "previous_limit": 3, "judge": "judge-3"})
        factory.save(self.path, record)
        with self.assertRaisesRegex(factory.FactoryError, "Invalid allowance history"):
            self._dst_record()

    def test_import_success(self):
        """Import sets writer_origin, schema 12, and hold on the destination."""
        record = self._do_import(self.out_path, self.sha256)
        self.assertEqual(record["schema_version"], 12)
        self.assertIn("writer_origin", record)
        wo = record["writer_origin"]
        self.assertEqual(wo["schema_version"], 1)
        self.assertNotEqual(record["admission"]["repository_identity"], self._src_record()["admission"]["repository_identity"])
        self.assertEqual(wo["source_host"], "source-host")
        self.assertEqual(wo["destination_host"], "dest-host")
        self.assertEqual(wo["candidate"], self.candidate)
        self.assertEqual(wo["sha256"], self.sha256)
        self.assertIsNotNone(record["hold"])

    def test_import_idempotent_replay(self):
        """A second identical import returns without error."""
        self._do_import(self.out_path, self.sha256)
        record2 = self._do_import(self.out_path, self.sha256)
        self.assertEqual(record2["schema_version"], 12)

    def test_import_wrong_sha256_refused(self):
        """Import refuses when --sha256 does not match the file."""
        with self.assertRaisesRegex(factory.FactoryError, "SHA-256"):
            self._do_import(self.out_path, "a" * 64)

    def test_import_wrong_from_host_refused(self):
        """Import refuses when --from-host does not match the bundle."""
        with self.assertRaisesRegex(factory.FactoryError, "from-host"):
            self._do_import(self.out_path, self.sha256, from_host="wrong-host")

    def test_import_wrong_destination_host_refused(self):
        with self.assertRaisesRegex(factory.FactoryError, "destination host"):
            self._do_import(self.out_path, self.sha256, host="wrong-dest-host")

    def test_import_cli_checks_local_endpoint_before_whoami(self):
        before = self.dst_path.read_bytes()
        archive = self.dst_path.with_suffix(".writer-origin.bundle")
        args = ["import-writer", "--task", self.dst_path.stem, "--file", str(self.out_path),
                "--from-host", self.source_host, "--sha256", self.sha256]
        for binary, server in ((sys.executable, "tcp://example.invalid:1"), (sys.executable, "unix://"),
                               ("relative-rex", "unix:///fixture.sock")):
            with self.subTest(binary=binary, server=server), redirect_stderr(StringIO()) as errors:
                code = factory.main(["--state-dir", str(self.dst_state), "--rex", binary, "--server", server, *args])
                self.assertEqual(code, 1)
                self.assertIn("--server unix://", errors.getvalue())
                self.assertEqual(FakeDestRex.calls, [])
                self.assertFalse(archive.exists())
                self.assertEqual(self.dst_path.read_bytes(), before)
        with redirect_stdout(StringIO()):
            self.assertEqual(factory.main(["--state-dir", str(self.dst_state), "--rex", sys.executable,
                                           "--server", "unix:///fixture.sock", *args]), 0)
        self.assertEqual(FakeDestRex.calls, [("whoami", "--json")])
        self.assertEqual(archive.read_bytes(), self.out_path.read_bytes())

    def test_import_already_launched_destination_refused(self):
        """Import refuses when the destination already has assignments."""
        factory.launch(self.dst_path, task="dst-task", assignment_id="prior", role="qa", goal="Verify",
                       cwd=str(self.dst_repo), command=[sys.executable], purpose="work",
                       binary=sys.executable, server="unix:///fixture.sock")
        with self.assertRaisesRegex(factory.FactoryError, "already has assignments"):
            self._do_import(self.out_path, self.sha256)

    def test_interrupted_task_save_reuses_identical_orphan(self):
        before = self.dst_path.read_bytes()
        archive = self.dst_path.with_suffix(".writer-origin.bundle")
        with patch.object(factory, "save", side_effect=OSError("interrupted")), self.assertRaisesRegex(
                OSError, "interrupted"):
            self._do_import(self.out_path, self.sha256)
        self.assertEqual(self.dst_path.read_bytes(), before)
        self.assertEqual(archive.read_bytes(), self.out_path.read_bytes())
        inode = archive.stat().st_ino
        record = self._do_import(self.out_path, self.sha256)
        self.assertEqual(archive.stat().st_ino, inode)
        self.assertEqual(record["schema_version"], 12)
        self.assertEqual(len(record["control_events"]), 1)

    def test_interrupted_archive_sync_requires_durable_orphan_on_retry(self):
        before = self.dst_path.read_bytes()
        archive = self.dst_path.with_suffix(".writer-origin.bundle")
        fsync = os.fsync

        # Fail the initial file sync, then each durability step during orphan reuse.
        for target in (archive, archive, archive.parent):
            def interrupt(fd):
                if os.path.samestat(os.fstat(fd), target.stat()):
                    raise OSError("interrupted sync")
                fsync(fd)

            with patch.object(os, "fsync", side_effect=interrupt), self.assertRaisesRegex(OSError, "interrupted sync"):
                self._do_import(self.out_path, self.sha256)
            self.assertEqual(self.dst_path.read_bytes(), before)
            self.assertEqual(archive.read_bytes(), self.out_path.read_bytes())
        inode = archive.stat().st_ino
        record = self._do_import(self.out_path, self.sha256)
        self.assertEqual(archive.stat().st_ino, inode)
        self.assertEqual(record["schema_version"], 12)
        self.assertEqual(len(record["control_events"]), 1)

    def test_import_checks_exact_destination_and_archive_fields(self):
        original = json.loads(self.out_path.read_bytes())
        for section, key, value, message in (
                ("source", "github_origin", "fixture/other", "GitHub origin"),
                ("destination", "task", "another", "destination task"),
                (None, "candidate", self.base, "candidate"),
                (None, "requirements", self.head, "requirements"),
                (None, "parent", self.head, "commit parent"),
                (None, "tree", _git(self.dst_repo, "rev-parse", self.base + "^{tree}").stdout.strip(), "commit tree"),
                (None, "parent", "f" * 40, "not found"),
                (None, "used", {"correction": False, "investigation": 0}, "writer_origin"),
                (None, "schema_version", 2, "writer_origin"),
                (None, "extra", 1, "bundle fields")):
            with self.subTest(key=key, value=value):
                bundle = copy.deepcopy(original)
                (bundle[section] if section else bundle)[key] = value
                self.out_path.write_text(json.dumps(bundle))
                digest = hashlib.sha256(self.out_path.read_bytes()).hexdigest()
                with self.assertRaisesRegex(factory.FactoryError, message):
                    self._do_import(self.out_path, digest)
                self.assertFalse(self.dst_path.with_suffix(".writer-origin.bundle").exists())

    def test_acceptance_digest_preserves_issue_body(self):
        capture = "### fixture/project#7: Task\nUpdated 2000-01-01\n\nUpdated original requirement\n"
        for path in (self.src_path, self.dst_path):
            record = factory.load_task(path)
            record["admission"]["acceptance"] = ["https://github.com/fixture/project/issues/7"]
            factory.save(path, record)
        factory.set_inputs(self.src_path, candidate=self.base, requirements=self.base, acceptance=capture)
        out, _, digest = self._do_export("captured")
        changed_time = capture.replace("2000-01-01", "2000-01-02")
        record = self._dst_record()
        record["acceptance"] = {self.base: changed_time.replace("original requirement", "changed requirement")}
        factory.save(self.dst_path, record)
        with self.assertRaisesRegex(factory.FactoryError, "acceptance digest"):
            self._do_import(out, digest)
        record["acceptance"][self.base] = changed_time
        factory.save(self.dst_path, record)
        self.assertEqual(self._do_import(out, digest)["schema_version"], 12)

    def test_import_oversized_file_refused(self):
        """Import refuses a bundle that exceeds the size limit."""
        big_path = self.bundle_dir / "big.json"
        with big_path.open("wb") as stream:
            stream.truncate(writer_transfer.BUNDLE_MAX_BYTES + 1)
        big_path.chmod(0o600)
        with self.assertRaisesRegex(factory.FactoryError, "size limit"):
            self._do_import(big_path, "a" * 64)

    def test_import_symlink_refused(self):
        link = self.bundle_dir / "link.json"
        link.symlink_to(self.out_path)
        with self.assertRaisesRegex(factory.FactoryError, "regular file|Cannot read"):
            self._do_import(link, self.sha256)

    def test_destination_hold_set_after_import(self):
        """The destination task is held after import."""
        record = self._do_import(self.out_path, self.sha256)
        self.assertIsNotNone(record["hold"])

    def test_source_record_unchanged_after_import(self):
        """The source task record is not modified by import."""
        with factory.task_lock(self.src_state, "src-task") as p:
            before = factory.read_record(p)
        self._do_import(self.out_path, self.sha256)
        with factory.task_lock(self.src_state, "src-task") as p:
            after = factory.read_record(p)
        self.assertEqual(before, after)


class TestGatesAfterImport(TransferBase):
    def setUp(self):
        super().setUp()
        self.out_path, self.source_host, self.sha256 = self._do_export()
        self._do_import(self.out_path, self.sha256)

    def test_launch_rejects_origin_type_and_valid_looking_field_drift(self):
        self.resume()
        record = self._dst_record()
        for key, value in (("destination_host", "source-two"), ("used", {"correction": 0, "investigation": 0}),
                           ("judge_rounds", 1), ("source_task", "other"), ("candidate", self.base),
                           ("requirements", self.candidate), ("archive", str(self.out_path)), ("schema_version", True),
                           ("contributors", [{"id": "w1", "context": "changed"}]), (None, None)):
            with self.subTest(key=key):
                changed = copy.deepcopy(record)
                if key is None:
                    changed["writer_origin"] = value
                else:
                    changed["writer_origin"][key] = value
                factory.save(self.dst_path, changed)
                before = self.dst_path.read_bytes()
                with self.assertRaisesRegex(factory.FactoryError, "writer_origin"):
                    self.launch()
                self.assertEqual(self.dst_path.read_bytes(), before)
        self.assertFalse(FakeDestRex.calls)

    def test_load_task_accepts_schema_12(self):
        """load_task accepts the imported schema-12 record."""
        record = self._dst_record()
        self.assertEqual(record["schema_version"], 12)
        self.assertIn("writer_origin", record)

    def test_load_task_refuses_schema_12_without_writer_origin(self):
        """load_task refuses a schema-12 record missing writer_origin."""
        with factory.task_lock(self.dst_state, "dst-task") as p:
            record = factory.load_task(p)
            del record["writer_origin"]
            factory.save(p, record)
        with self.assertRaisesRegex(factory.FactoryError, "writer_origin"):
            self._dst_record()

    def test_load_task_refuses_writer_origin_at_schema_11(self):
        """load_task refuses writer_origin on a schema-11 record."""
        with factory.task_lock(self.dst_state, "dst-task") as p:
            record = factory.load_task(p)
            record["schema_version"] = 11
            factory.save(p, record)
        with self.assertRaisesRegex(factory.FactoryError, "writer_origin"):
            self._dst_record()

    def launch(self, key="new", role="qa", purpose="work", dependencies=()):
        return factory.launch(self.dst_path, task="dst-task", assignment_id=key, role=role, goal="Verify",
                              cwd=str(self.dst_repo), command=[sys.executable], purpose=purpose,
                              dependencies=dependencies, binary=sys.executable, server="unix:///fixture.sock")

    def resume(self):
        factory.hold_task(self.dst_path, None, request_id="resume1", expected_revision=None)

    def test_launch_requires_resume_and_destination_host(self):
        held = self.dst_path.read_bytes()
        with self.assertRaisesRegex(factory.FactoryError, "paused"):
            self.launch()
        self.assertEqual(self.dst_path.read_bytes(), held)
        self.assertFalse(FakeDestRex.calls)
        self.resume()
        resumed = self.dst_path.read_bytes()
        source_calls = len(FakeSourceRex.calls)
        with patch.object(factory, "Rex", FakeSourceRex), self.assertRaisesRegex(factory.FactoryError, "destination host"):
            self.launch()
        self.assertEqual(FakeSourceRex.calls[source_calls:], [("whoami", "--json")])
        self.assertEqual(self.dst_path.read_bytes(), resumed)
        self.assertEqual(FakeDestRex.sessions, [])
        record, _ = self.launch()
        self.assertEqual(record["rex"]["host_id"], "dest-host")
        launched = self.dst_path.read_bytes()
        self._do_import(self.out_path, self.sha256)
        self.assertEqual(self.dst_path.read_bytes(), launched)
        self.assertIsNone(self._dst_record()["hold"])
        record["rex"]["host_id"] = FakeSourceRex.host
        factory.save(self.dst_path, record)
        with patch.object(factory, "Rex", FakeSourceRex), self.assertRaisesRegex(factory.FactoryError, "archive"):
            self.launch(key="later")

    def test_first_launch_requires_imported_pair_and_later_correction_can_change_it(self):
        for candidate, requirements in ((self.base, self.base), (self.candidate, self.candidate)):
            with self.subTest(candidate=candidate, requirements=requirements), redirect_stdout(StringIO()):
                self.assertEqual(factory.main(["--state-dir", str(self.dst_state), "inputs", "--task", self.path.stem,
                                               "--candidate", candidate, "--requirements", requirements]), 0)
                if candidate == self.base:
                    self.resume()
                before = self.dst_path.read_bytes()
                with self.assertRaisesRegex(factory.FactoryError, "imported candidate and requirements"):
                    self.launch()
                self.assertEqual(FakeDestRex.calls, [])
                self.assertEqual(self.dst_path.read_bytes(), before)
        original = dict(candidate=self.candidate, requirements=self.base)
        factory.set_inputs(self.dst_path, **original)
        _, first = self.launch()
        self.assertEqual(first["request"]["inputs"], original)

        changed = dict(candidate=self.base, requirements=self.candidate)
        _git(self.dst_repo, "checkout", "--detach", self.base).check_returncode()
        factory.set_inputs(self.dst_path, **changed)
        record, later = self.launch(key="later", purpose="correction")
        self.assertEqual(later["request"]["inputs"], changed)
        self.assertEqual(factory.task_view(record)["allowances"]["correction"]["remaining"], 0)

    def test_origin_cannot_supply_a_result_or_dependency(self):
        self.resume()
        self.assertEqual(self._dst_record()["assignments"], {})
        with self.assertRaisesRegex(factory.FactoryError, "Unknown assignment"):
            factory.record_result(self.path, assignment_id="w1", candidate=self.head, requirements=self.base,
                                  outcome="completed", artifacts=["fixture"])
        with self.assertRaisesRegex(factory.FactoryError, "earlier assignment"):
            self.launch(dependencies=["w1"])

    def test_schema_11_records_not_rewritten_by_load(self):
        """Schema-11 source record stays at 11 (not migrated to 12) by load_task."""
        before = self.src_path.read_bytes()
        record = self._src_record()
        self.assertEqual(self.src_path.read_bytes(), before)
        self.assertEqual(record["schema_version"], 11)
        self.assertNotIn("writer_origin", record)

    def test_launch_spends_only_remaining_correction(self):
        self.resume()
        self.assertEqual(factory.task_view(self._dst_record())["allowances"]["correction"],
                         {"limit": 2, "used": 1, "remaining": 1})
        self.launch(key="last", purpose="correction")
        with self.assertRaisesRegex(factory.FactoryError, "correction allowance exhausted"):
            self.launch(key="excess", purpose="correction")
        self.assertEqual(list(self._dst_record()["assignments"]), ["last"])

    def test_conflicting_replay_refused(self):
        """import_writer refuses a second bundle with a different digest."""
        # Modify the bundle to create a different one.
        bundle_b = self.bundle_dir / "bundle_b.json"
        raw = self.out_path.read_bytes()
        data = json.loads(raw)
        data["candidate"] = "b" * 40  # Corrupt candidate to change digest.
        bundle_b.write_bytes(json.dumps(data, indent=2).encode())
        os.chmod(bundle_b, 0o600)
        sha256_b = hashlib.sha256(bundle_b.read_bytes()).hexdigest()
        with self.assertRaisesRegex(factory.FactoryError, "different writer_origin|Conflicting"):
            self._do_import(bundle_b, sha256_b)

    def test_fresh_approvals_and_final_acceptance_check_archived_history(self):
        self.resume()
        transport = GitHub(self.base, self.head, self.head)
        pr = transport.identity["url"]
        self.frozen = release_evidence.capture(self._dst_record(), pr, transport)
        self.review("qa", "qa", frozen=False)
        self.review("overlap", "qa", frozen=False, context="context-w1")
        self.review("release", "release")
        record = self._dst_record()
        with patch.object(transport, "binding", return_value={str(self.dst_repo / "poll-pr-reviews.sh"): "writer-source"}):
            with self.assertRaisesRegex(factory.FactoryError, "assignment checkout"):
                release_evidence.capture(record, pr, transport)
        with patch.object(factory, "ROOT", self.tmp / "new-runtime"):
            with self.assertRaisesRegex(factory.FactoryError, "independent of contributors"):
                integration.approvals(record, "release", ["overlap"])
        # Reproduce the production in-memory edit with the pinned archive bytes intact.
        record["writer_origin"]["contributors"].clear()
        with self.assertRaisesRegex(factory.FactoryError, "writer_origin"):
            integration.approvals(record, "release", ["overlap"])
        transport.pr.update(state="closed", merged=True, merge_commit_sha=self.head)
        receipt = integration.delivered(self.path, pr=pr, review="release", required=["qa"], transport=transport)
        self.assertEqual(set(receipt["approvals"]), {"release", "qa"})
        self.review("accept", "release", delivery="release")
        self.review("self-accept", "release", context="context-w1", delivery="release")
        with self.assertRaisesRegex(factory.FactoryError, "contributor cannot judge"):
            integration.delivered_accepted(self.path, "release", "self-accept")
        self.assertEqual(integration.delivered_accepted(self.path, "release", "accept")["outcome"], "approved")
        record = self._dst_record()
        archive = Path(record["writer_origin"]["archive"])
        original = archive.read_bytes()
        for problem in ("cached context", "redirected", "modified", "missing", "symlink", "permissions"):
            with self.subTest(problem=problem):
                altered = copy.deepcopy(record)
                if problem == "cached context":
                    altered["writer_origin"]["contributors"][0]["context"] = "different"
                elif problem == "redirected":
                    elsewhere = self.bundle_dir / archive.name
                    archive.rename(elsewhere)
                    altered["writer_origin"]["archive"] = str(elsewhere)
                elif problem == "modified":
                    archive.write_bytes(original + b" ")
                elif problem == "permissions":
                    archive.chmod(0o644)
                else:
                    archive.unlink()
                    if problem == "symlink":
                        archive.symlink_to(self.out_path)
                factory.save(self.path, altered)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(factory.FactoryError, "archive|permissions|writer_origin"):
                    self._do_import(self.out_path, self.sha256)
                self.assertEqual(self.path.read_bytes(), before)
                with self.assertRaisesRegex(factory.FactoryError, "archive|permissions|writer_origin"):
                    integration.approvals(altered, "release", ["qa"])
                with self.assertRaisesRegex(factory.FactoryError, "archive|permissions|writer_origin"):
                    integration.delivered_accepted(self.path, "release", "accept")
                archive.unlink(missing_ok=True)
                archive.write_bytes(original)
                archive.chmod(0o600)

if __name__ == "__main__":
    unittest.main()
