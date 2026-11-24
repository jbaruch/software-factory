"""Cache retention in real disposable linked repositories. Rex observations are fixtures."""
import runtime_path  # noqa: F401
import asyncio
from contextlib import chdir
import copy
import hashlib
import shutil
import json
import os
from pathlib import Path
from io import StringIO
import subprocess
import unittest
import sys
from unittest.mock import patch

import codex_worker
import factory
import go_cache
import integration
import release_evidence
from test_codex_worker import FakeProcess, events
from test_integration import GitHub
import test_provisioning


class LinkedCache(test_provisioning.ProvisionedRepository):
    def setUp(self):
        super().setUp()
        self.record = self.provision()
        original_inputs = self.record["inputs"]
        self.admin = Path(self.record["provisioning"]["git_dir"])
        self.cache = self.admin / "go-build"
        self.cache.mkdir(mode=0o700)
        if sys.platform == "darwin":
            factory.exclude_from_time_machine(self.cache)
        self.file = self.destination / "library.go"
        self.file.write_text("package library\nconst Number = 4\n")
        self.git("-C", str(self.destination), "add", "library.go")
        self.git("-C", str(self.destination), "commit", "-qm", "Update the fixture")
        head = self.git("-C", str(self.destination), "rev-parse", "HEAD")
        self.record = factory.set_inputs(self.path, candidate=head, requirements="requirements-v1")
        self.build()
        self.at = 1600000000
        self.set_cache_time(self.at)
        self.record.update(rex=dict(binary="/fixture/rex", server="unix:///fixture", host_id="fixture"),
                           session_id="session")
        command = factory.codex_command(self.path, task="fixture", assignment_id="writer", codex=sys.executable,
                                        model="fixture", effort="high", cwd=str(self.destination), role="backend",
                                        write=True, commit_message="Update the fixture")
        self.receipt = self.state / "fixture.writer.codex.json"
        brief = self.state / "fixture.writer.md"
        factory.save(brief, "Build the fixture")
        req = dict(command=command, role="backend", cwd=str(self.destination), purpose="work", goal="fixture",
                   instructions_sha256=hashlib.sha256(brief.read_bytes()).hexdigest(), inputs=original_inputs)
        result = dict(inputs=req["inputs"], candidate=head, outcome="completed", artifacts=[str(self.receipt)])
        self.assignment: dict = dict(request=req, state="launched", result=result, native_collected=True,
                               window_label="backend-writer-abcd1234", brief=str(brief),
                               window_id="w", block_id="b", closed=dict(state="exited", exit=dict(exit_code=0),
                               session_id="session", capture=None, reason=None, at=self.at))
        self.record["assignments"]["writer"] = self.assignment
        factory.save(self.path, self.record)
        self.native: dict = dict(schema_version=5, candidate=head, report={"outcome": "completed"},
                           kind="codex-execution", job="fixture/writer", state="completed", exit_code=0, inputs=req["inputs"], brief_sha256=req["instructions_sha256"], cwd=req["cwd"],
                           worktree_git_dir=str(self.admin), repository_identity=self.record["admission"]["repository_identity"],
                           commit=dict(state="created", parent=original_inputs["candidate"], candidate=head,
                                       staged_tree=self.git("-C", str(self.destination), "rev-parse", "HEAD^{tree}"),
                                       message="Update the fixture"),
                           requested_model="fixture", requested_effort="high", binary=str(Path(sys.executable).resolve()),
                           events=str(self.receipt.with_suffix(".jsonl")), writer=True,
                           created_at=self.at-120, finished_at=self.at-1)
        self.collect_fixture("writer", self.native)
        self.assignment = self.record["assignments"]["writer"]
        self.addCleanup(patch.stopall)
        patch.object(go_cache.time, "time", return_value=self.at + go_cache.IDLE_SECONDS + 1).start()
        # Scale only the byte floor for this small native build.
        size = sum(p.stat().st_blocks for p in (self.cache, *self.cache.rglob("*"))) // 2
        patch.object(go_cache, "MIN_KIB", size // 2).start()
        # This fixture owns the only build process and keeps it stopped until readback.
        self.terminal = patch.object(sys, "stdin", StringIO()).start()
        self.terminal.isatty = lambda: True
        self.process_check = go_cache.process_check
        self.observer = patch.object(go_cache, "process_check", return_value={"observed_processes": 2}).start()

    def build(self):
        subprocess.run(["go", "build", str(self.file)], cwd=self.destination, env={
            "HOME": str(self.root), "TMPDIR": str(self.root), "PATH": os.environ["PATH"],
            "GOCACHE": str(self.cache), "GOTOOLCHAIN": "local", "GOENV": "off",
            "GOTELEMETRY": "off", "GOWORK": "off", "GO111MODULE": "off", "GOPROXY": "off"}, check=True)

    def set_cache_time(self, timestamp):
        for path in [self.cache, *self.cache.rglob("*")]:
            os.utime(path, (timestamp, timestamp))

    def inspect(self, clean=False):
        self.terminal.seek(0)
        self.terminal.write("fixture\n")
        self.terminal.seek(0)
        status, out, err = self.cli("go-caches", "--repo", str(self.repo),
                                    *(["--clean-task", "fixture"] if clean else []))
        self.assertTrue(out, err)
        row, = json.loads(out)
        self.assertEqual(status, int(clean and row["decision"] != "cleaned"), row)
        return row

    def protected(self):
        paths = [p for base in (self.repo, self.destination, self.state) for p in base.rglob("*")
                 if not p.is_relative_to(self.cache)]
        return {p: (p.stat().st_ino, p.stat().st_mode, p.stat().st_mtime_ns,
                    p.read_bytes() if p.is_file() else None) for p in paths}

    def trim(self, executable=None, status=0):
        code, out, err = self.cli("go-caches", "--repo", str(self.repo), "--trim-go", executable or shutil.which("go"))
        self.assertEqual(code, status, (out, err))
        row, = json.loads(out)
        return row

    def test_native_go_ages_only_old_entries_when_go_deems_trim_due(self):
        # The fixture gives the existing Go entries old mtimes. Go owns the actual clock and trim marker.
        old = next(self.cache.glob("*/*-a"))
        recent = self.cache / "ab" / ("ab" + "1" * 62 + "-d")
        recent.write_bytes(b"recent fixture")
        root, readme = self.cache.stat(), (self.cache / "README").read_bytes()
        protected = self.protected()
        with patch.dict(os.environ, GOFLAGS="-unknown", GOTOOLCHAIN="missing", GOENV="/missing",
                        GOCACHEPROG="/missing", GOPROXY="invalid", GOROOT="/missing"):
            row = self.trim()
            self.assertEqual((row["decision"], row["removed_entries"]), ("retained", 0), row)
            self.assertTrue(old.is_file())
            (self.cache / "trim.txt").write_text(str(self.at))
            before = {p.name for p in self.cache.glob("*/*")}
            row = self.trim()
        after = {p.name for p in self.cache.glob("*/*")}
        self.assertFalse(after - before, "The hourly probe should reuse its own cache entries")
        self.assertEqual((row["decision"], row["removed_entries"], row["retained_entries"]),
                         ("cleaned", len(before - after), len(before & after)), row)
        self.assertFalse(old.exists())
        self.assertEqual(recent.read_bytes(), b"recent fixture")
        self.assertTrue(os.path.samestat(self.cache.stat(), root))
        self.assertEqual((self.cache / "README").read_bytes(), readme)
        self.assertEqual(self.protected(), protected)
        self.observer.assert_not_called()
        self.build()

    def test_native_aging_requires_owner_terminal_local_cache_and_exclusive_locks(self):
        with patch.object(go_cache, "native_build") as build:
            with go_cache.inode_lock(self.admin, shared=True):
                self.assertIn("Busy lock", self.trim()["reason"])
            unknown = self.cache / "not-a-go-entry"
            unknown.touch()
            self.assertIn("unexpected cache entry", self.trim()["reason"])
            unknown.unlink()
            other = factory.new_task("other", "other-line")
            other["assignments"]["consumer"] = {"request": {"command": ["/bin/cat", str(self.cache / "README")],
                                                            "cwd": str(self.root)}}
            factory.save(self.state / "other.json", other)
            self.assertIn("reuse by other/consumer", self.trim()["reason"])
            (self.state / "other.json").unlink()
            self.assignment["closed"] = None
            factory.save(self.path, self.record)
            self.assertEqual(self.trim()["decision"], "retained")
            self.path.unlink()
            self.assertIn("one provisioned task", self.trim()["reason"])
            build.assert_not_called()

    def test_failed_native_build_reports_readback_and_nonzero_status(self):
        executable = self.root / "fixture-go"
        executable.write_text("#!/bin/sh\nif [ \"$1\" = env ]; then printf '\\n\\n'; else exit 7; fi\n")
        executable.chmod(0o700)
        row = self.trim(str(executable), status=1)
        self.assertEqual(row["decision"], "failed")
        self.assertIn("Go build exited 7", row["reason"])
        self.assertEqual((row["removed_entries"], row["after_kib"]), (0, row["size_kib"]))

    def test_native_aging_refuses_distribution_cache_backend(self):
        executable = self.root / "fixture-go"
        executable.write_text("#!/bin/sh\nif [ \"$1\" = env ]; then printf '/fixture/backend\\n\\n'; "
                              "else touch \"$GOCACHE/build-ran\"; fi\n")
        executable.chmod(0o700)
        row = self.trim(str(executable), status=1)
        self.assertIn("GOCACHEPROG", row["reason"])
        self.assertFalse((self.cache / "build-ran").exists())

    def test_native_aging_leaves_an_attended_retirement_empty(self):
        self.assertEqual(self.inspect(True)["decision"], "cleaned")
        remaining = list(self.cache.iterdir())
        row = self.trim()
        self.assertEqual((row["decision"], row["removed_entries"], row["retained_entries"]), ("retained", 0, 0))
        self.assertCountEqual(self.cache.iterdir(), remaining)

    def test_native_build_and_readback_hold_admission_locks(self):
        line = factory.execution_lock_path(self.state, factory.task_line(self.record))
        def contend():
            for path in (line, self.path.with_suffix(".lock"), self.admin, self.cache):
                with self.assertRaises(factory.ExecutionBusy), go_cache.inode_lock(path, directory=path.is_dir()):
                    self.fail(f"Acquired {path} during native aging")

        build = go_cache.native_build
        def run(*args):
            contend()
            return build(*args)

        walk = go_cache.cache_contents
        def readback(*args, **kwargs):
            if invoked.called:
                contend()
                raise OSError("fixture readback failure")
            return walk(*args, **kwargs)

        with patch.object(go_cache, "native_build", side_effect=run) as invoked, \
                patch.object(go_cache, "cache_contents", side_effect=readback):
            row = self.trim(status=1)
        self.assertEqual(row["decision"], "failed")
        self.assertIn("fixture readback failure", row["reason"])
        self.assertNotIn("removed_entries", row)

    def test_census_preserves_review_exports_but_refuses_a_damaged_task(self):
        export = self.state / "release-review.json"
        snapshot = GitHub("a" * 40, "b" * 40, "c" * 40).snapshot_value
        snapshot["inline_comments"] = {}
        snapshot["watch"] = {"status": "complete"}
        export.write_text(json.dumps(snapshot))
        export.chmod(0o644)
        before = self.protected()
        self.assertEqual(self.inspect()["decision"], "eligible")
        self.assertEqual(self.protected(), before)

        damaged = self.state / "other-task.json"
        for payload, mode in ((b"{", 0o600), (json.dumps({**snapshot, "task": "other-task"}).encode(), 0o644)):
            with self.subTest(mode=mode):
                damaged.write_bytes(payload)
                damaged.chmod(mode)
                row = self.inspect()
                self.assertEqual(row["decision"], "retained")
                self.assertIn(damaged.name, row["reason"])
                self.assertEqual(damaged.read_bytes(), payload)

    def test_clean_and_native_rebuild_preserve_working_admin_and_owner_files(self):
        (self.destination / "untracked").write_text("operator work")
        (self.destination / "tool").write_text("edited")
        self.git("-C", str(self.destination), "add", "tool")
        (self.destination / "tool").write_text("another unstaged edit")
        (self.admin / "index.lock").write_text("keep")
        before = self.protected()
        identity = self.cache.stat()
        self.assertTrue(list(self.cache.glob("*/*-a")))
        self.assertEqual(self.inspect()["decision"], "eligible")
        row = self.inspect(True)
        self.assertEqual(row["decision"], "cleaned", row)
        self.assertLess(row["after_kib"], row["size_kib"])
        self.assertFalse(list(self.cache.glob("*/*-a")))
        self.assertTrue(os.path.samestat(self.cache.stat(), identity))
        if sys.platform == "darwin":
            attr, expected = factory.TIME_MACHINE_EXCLUSION
            value = subprocess.check_output(["/usr/bin/xattr", "-px", attr, str(self.cache)], text=True)
            self.assertEqual(bytes.fromhex(value), bytes.fromhex(expected))
        self.build()
        self.set_cache_time(self.at + go_cache.IDLE_SECONDS)
        self.assertTrue(list(self.cache.glob("*/*-a")))
        self.assertIn("24 hours", self.inspect(True)["reason"])
        self.assertEqual(self.protected(), before)

    def test_terminal_and_size_bounds(self):
        with patch.object(go_cache, "MIN_KIB", 1024 * 1024):
            self.assertIn("size floor", self.inspect(True)["reason"])
        with patch.object(go_cache.time, "time", return_value=self.at + go_cache.IDLE_SECONDS - 1):
            self.assertIn("24 hours", self.inspect(True)["reason"])
        self.native["state"] = "starting"
        factory.save(self.receipt, self.native)
        self.assertIn("inconsistent terminal", self.inspect(True)["reason"])
        self.native["state"] = "failed"
        factory.save(self.receipt, self.native)
        self.assertIn("inconsistent terminal", self.inspect(True)["reason"])
        self.assignment["closed"].update(state="lost", exit=None, reason="Rex restarted")
        factory.save(self.path, self.record)
        self.assertIn("Rex process exit", self.inspect(True)["reason"])

    def test_promised_objects_must_be_local_for_census_and_clean(self):
        remote = self.root / "promisor.git"
        self.git("clone", "--quiet", "--bare", "--no-local", str(self.repo), str(remote))
        self.git("remote", "add", "fixture", str(remote))
        self.git("config", "remote.fixture.promisor", "true")
        self.git("config", "remote.fixture.partialclonefilter", "blob:none")
        self.assertEqual(self.inspect()["decision"], "eligible")
        held = self.root / "held-object"
        for field in ("staged_tree", "parent", "candidate"):
            oid = self.native["commit"][field]
            self.git("--git-dir=" + str(remote), "cat-file", "-e", oid)
            obj = self.repo / ".git/objects" / oid[:2] / oid[2:]
            obj.rename(held)
            before = self.protected()
            for clean in (False, True):
                with self.subTest(field=field, clean=clean):
                    row = self.inspect(clean)
                    self.assertEqual(row["decision"], "retained", row)
                    self.assertEqual(self.protected(), before)
                    self.assertTrue(list(self.cache.glob("*/*-a")))
            held.rename(obj)

    def test_configured_trace_destinations_stay_absent_through_cleanup(self):
        self.delivery_fixture(self.destination)
        home = self.root / "home"
        home.mkdir()
        targets = [self.repo / ".git" / name for name in ("normal", "perf", "event")]
        (home / ".gitconfig").write_text("[trace2]\n" + "".join(
            f"  {target.name}Target = {target}\n" for target in targets))
        with patch.dict(os.environ, HOME=str(home), XDG_CONFIG_HOME=str(home)):
            self.git("rev-parse", "HEAD")
            for target in targets:
                self.assertGreater(target.stat().st_size, 0)
                target.unlink()
            before = self.protected()
            for clean, expected in ((False, "eligible"), (True, "cleaned")):
                self.assertEqual(self.inspect(clean)["decision"], expected)
                self.assertFalse(any(path.exists() for path in targets))
                self.assertEqual(self.protected(), before)

    def test_signed_commit_parent_read_uses_no_git_children(self):
        headers, message = self.git("cat-file", "commit", self.native["candidate"]).split("\n\n", 1)
        # Opaque signature fixture. Reading its parent never needs a signing tool.
        signed = self.root / "commit"
        signed.write_bytes((headers + "\nencoding ISO-8859-1\ngpgsig -----BEGIN PGP SIGNATURE-----\n fixture\n"
                            " -----END PGP SIGNATURE-----\n\n" + message + " signé\n").encode("latin-1"))
        candidate = self.git("hash-object", "-w", "-t", "commit", str(signed))
        self.git("-C", str(self.destination), "update-ref", "HEAD", candidate, self.native["candidate"])
        self.record["inputs"]["candidate"] = self.assignment["result"]["candidate"] = candidate
        self.native["candidate"] = self.native["commit"]["candidate"] = candidate
        factory.save(self.path, self.record)
        factory.save(self.receipt, self.native)
        self.git("config", "log.showSignature", "true")
        self.git("config", "gpg.program", "/usr/bin/false")
        trace = self.root / "git-trace"
        run = subprocess.run

        def instrument(argv, **kwargs):
            if argv[0] == factory.git_binary():
                kwargs["env"] = {**kwargs["env"], "GIT_TRACE2_EVENT": str(trace)}
            return run(argv, **kwargs)

        with patch.object(subprocess, "run", side_effect=instrument):
            self.assertEqual(self.inspect()["decision"], "eligible")
            self.assertEqual(self.inspect(True)["decision"], "cleaned")
        events = [json.loads(line) for line in trace.read_text().splitlines()]
        self.assertTrue(any(event["event"] == "start" for event in events))
        self.assertFalse([event for event in events if event["event"] == "child_start"])

    def attest(self, assignment):
        request = assignment["request"]
        assignment["native_launch"] = dict(runtime=factory.native_runtime_digest(),
                                           request=factory.request_digest(request))
        assignment["interpreter"] = dict(invocation=request["command"][0],
                                         file=factory.interpreter_identity(sys.executable))
        self.record["schema_version"] = 13

    def collect_fixture(self, key, receipt):
        path = self.state / f"fixture.{key}.codex.json"
        stream = events(receipt["report"]["outcome"])
        stream[0]["thread_id"] = key
        receipt.update(codex_worker.parse_events(stream), elapsed_seconds=119, error=None)
        factory.save(path, receipt)
        factory.save(path.with_suffix(".jsonl"), "".join(
            json.dumps(dict(schema_version=1, kind="codex-event-line", data=json.dumps(event))) + "\n"
            for event in stream))
        assignment = self.record["assignments"][key]
        assignment.update(result=None, native_collected=False)
        self.attest(assignment)
        factory.save(self.path, self.record)
        self.record = factory.collect_codex(self.path, key, None, self.record, assignment)

    def reader_fixture(self, key, cwd, frozen=None, *, legacy=False):
        command = factory.codex_command(self.path, task="fixture", assignment_id=key, codex=sys.executable,
                                        model="fixture", effort="high", cwd=str(cwd), role=key, release_evidence=frozen)
        brief = self.state / f"fixture.{key}.md"
        factory.save(brief, "Review the fixture" + (release_evidence.render(frozen) if frozen else ""))
        path = brief.with_suffix(".codex.json")
        req: dict = dict(self.assignment["request"], role=key, cwd=str(cwd), command=command, inputs=self.record["inputs"],
                   instructions_sha256=hashlib.sha256(brief.read_bytes()).hexdigest())
        self.record["assignments"][key] = dict(self.assignment, request=req, brief=str(brief),
            window_label=f"{key}-{key}-abcd1234", window_id=key, block_id=key)
        if not legacy:
            self.record["assignments"][key]["launch_checkout"] = go_cache.checkout_binding(
                Path(factory.git_worktree(cwd)), factory.git_dir(cwd), self.record["admission"]["repository_identity"])
        self.collect_fixture(key, dict(self.native, job="fixture/" + key, candidate=req["inputs"]["candidate"], writer=False, commit=None,
            cwd=str(cwd), worktree_git_dir=str(factory.git_dir(cwd)), inputs=req["inputs"],
            brief_sha256=req["instructions_sha256"], events=str(path.with_suffix(".jsonl")), report={"outcome": "approved"}))

    def delivery_fixture(self, reader):
        head = self.record["inputs"]["candidate"]
        transport = GitHub(self.native["inputs"]["candidate"], head, "a" * 40)
        self.git("remote", "add", "origin", "https://github.com/fixture/project")
        for key, cwd in (("qa", reader), ("release", self.destination)):
            frozen = release_evidence.capture(self.record, transport.identity["url"], transport) if key == "release" else None
            self.reader_fixture(key, cwd, frozen)
        transport.pr.update(state="closed", merged=True, merge_commit_sha=transport.merge_sha)
        patch.object(factory, "connect", side_effect=AssertionError("Stored exit replay contacted Rex")).start()
        delivered = integration.delivered(self.path, pr=transport.identity["url"], review="release",
                                          required=["qa"], transport=transport)
        return Path(delivered.pop("receipt_path")), delivered

    def test_finalized_delivery_is_distinct_from_conflicting_receipts(self):
        path, delivered = self.delivery_fixture(self.destination)
        self.assertEqual(self.inspect()["decision"], "eligible")
        factory.save(path, {**delivered, "pr": "https://github.com/fixture/project/pull/8"})
        for clean in (False, True):
            row = self.inspect(clean)
            self.assertEqual(row["decision"], "retained")
            self.assertIn("release judgment approved", row["reason"])
        for changes in ({"kind": "other"}, {"task": "other"}, {"review": "other"}, {"schema_version": True},
                        {"required": []}, {"worktree_git_dir": str(self.admin)}, {"cwd": str(self.destination)},
                        {"approvals": {**delivered["approvals"], "qa": delivered["approvals"]["release"]}},
                        {"required": ["unknown"], "approvals": {"unknown": delivered["approvals"]["qa"],
                                                               "release": delivered["approvals"]["release"]}}):
            with self.subTest(changes=changes):
                factory.save(path, {**delivered, **changes})
                self.assertEqual(self.inspect(True)["decision"], "retained")
        factory.save(path, delivered)

        def replace_receipt(*_):
            factory.save(path, self.native)
            return {}

        self.observer.side_effect = replace_receipt
        self.assertEqual(self.inspect(True)["decision"], "retained")
        self.assertTrue(list(self.cache.glob("*/*-a")))
        self.observer.side_effect = None
        factory.save(path, delivered)
        before = self.protected()
        self.assertEqual(self.inspect(True)["decision"], "cleaned")
        self.assertEqual(self.protected(), before)

    def test_delivery_replays_original_approval_and_writer_contexts(self):
        path, delivered = self.delivery_fixture(self.destination)
        qa = self.state / "fixture.qa.codex.json"
        receipt = json.loads(qa.read_text())
        stream = qa.with_suffix(".jsonl")
        original = stream.read_text()
        for context in ("release", "writer"):
            with self.subTest(context=context):
                receipt["thread_id"] = context
                factory.save(qa, receipt)
                delivered["approvals"]["qa"] = integration.approval_record("codex", receipt,
                    self.record["assignments"]["qa"]["result"])
                factory.save(path, delivered)
                factory.save(stream, original)
                self.assertIn("original events", self.inspect()["reason"])
                lines = original.splitlines()
                first = json.loads(lines[0])
                first["data"] = json.dumps(dict(type="thread.started", thread_id=context))
                factory.save(stream, "\n".join([json.dumps(first), *lines[1:]]) + "\n")
                self.assertIn("distinct original native contexts", self.inspect(True)["reason"])
                self.assertTrue(list(self.cache.glob("*/*-a")))

    def test_current_reader_cannot_select_an_older_writer_commit(self):
        self.git("-C", str(self.destination), "commit", "--allow-empty", "-qm", "Next input")
        self.record = factory.set_inputs(self.path, candidate=self.git("-C", str(self.destination), "rev-parse", "HEAD"),
                                         requirements="requirements-v1")
        self.reader_fixture("qa", self.destination)
        self.assertTrue(factory.result_current(self.record, self.record["assignments"]["qa"]["result"]))
        before = self.protected()
        self.assertIn("verified commit produced the selected", self.inspect(True)["reason"])
        self.assertEqual(self.protected(), before)
        self.assertTrue(list(self.cache.glob("*/*-a")))

    def test_collection_after_reader_head_advances_keeps_original_approval(self):
        reader = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        collect = factory.collect_codex

        def advance(*args):
            self.git("-C", str(reader), "commit", "--allow-empty", "-qm", "Advance reader")
            return collect(*args)

        with patch.object(factory, "collect_codex", side_effect=advance):
            self.reader_fixture("qa", reader)
        assignment = self.record["assignments"]["qa"]
        self.assertTrue(assignment["native_collected"])
        self.assertEqual(assignment["result"]["candidate"], self.native["candidate"])
        self.assertEqual(assignment["launch_checkout"]["head"], self.native["candidate"])
        self.assertIn("original reader worktree ownership", self.inspect()["reason"])

    def test_nested_legacy_reader_passes_attended_release_user_check_only(self):
        self.reader_fixture("qa", self.destination / "nested", legacy=True)
        assignment = self.record["assignments"]["qa"]
        self.assertTrue(assignment["native_collected"])
        self.assertNotIn("launch_checkout", assignment)
        owner_bytes = self.path.read_bytes()
        witness = go_cache.checkout_binding(self.destination, self.admin, self.record["admission"]["repository_identity"])
        with go_cache.inode_lock(self.admin):
            users = go_cache.check_release_users(self.state, self.record, self.destination, self.admin, witness)
        receipt = self.state / "fixture.qa.codex.json"
        self.assertEqual(users[receipt], receipt.read_bytes())
        self.assertIn("missing original reader worktree ownership", self.inspect()["reason"])
        self.assertEqual(self.path.read_bytes(), owner_bytes)
        # The same collected reader can finish an attended pre-upgrade checkout's lifecycle.
        del self.record["provisioning"]["checkout_inodes"]
        factory.save(self.path, self.record)
        owner_bytes = self.path.read_bytes()
        self.terminal.seek(0)
        self.terminal.write("fixture\n")
        self.terminal.seek(0)
        code, output, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["already_released"])
        self.assertEqual(self.path.read_bytes(), owner_bytes)
        self.assertEqual(receipt.read_bytes(), users[receipt])

    def test_collection_does_not_adopt_a_reader_pointer_or_a_legacy_collection_witness(self):
        reader = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        collect = factory.collect_codex

        def change_pointer(*args):
            pointer = reader / ".git"
            original = self.root / "original-qa-pointer"
            pointer.rename(original)
            pointer.write_bytes(original.read_bytes())
            return collect(*args)

        with patch.object(factory, "collect_codex", side_effect=change_pointer):
            self.reader_fixture("qa", reader)
        assignment = self.record["assignments"]["qa"]
        self.assertTrue(assignment["native_collected"])
        self.assertEqual(assignment["result"]["outcome"], "approved")
        self.assertIn("original reader worktree ownership", self.inspect(True)["reason"])
        assignment["reader_checkout"] = go_cache.checkout_binding(reader, factory.git_dir(reader),
            self.record["admission"]["repository_identity"])
        del assignment["launch_checkout"]
        factory.save(self.path, self.record)
        collected = collect(self.path, "qa", None, self.record, assignment)["assignments"]["qa"]
        self.assertNotIn("launch_checkout", collected)
        self.assertEqual(collected["result"], assignment["result"])
        self.assertIn("original reader worktree ownership", self.inspect(True)["reason"])

    def test_replaced_writer_pointer_is_never_adopted(self):
        pointer = self.destination / ".git"
        saved = self.root / "original-pointer"
        pointer.rename(saved)
        pointer.write_bytes(saved.read_bytes())
        self.assertNotEqual(pointer.stat().st_ino, saved.stat().st_ino)
        entries = set(self.cache.rglob("*"))
        self.assertIn("original writer checkout", self.inspect(True)["reason"])
        with self.assertRaisesRegex(factory.FactoryError, "original provisioning identity"):
            factory.release_worktree(self.path)
        self.assertTrue(pointer.exists())
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_separate_reader_requires_its_reciprocal_link(self):
        reader = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        self.delivery_fixture(reader)
        self.assertEqual(self.inspect()["decision"], "eligible")
        backlink = factory.git_dir(reader) / "gitdir"
        original = backlink.read_text()
        def redirect_backlink(*_):
            backlink.write_text(str(self.destination / ".git") + "\n")
            return {}

        self.observer.side_effect = redirect_backlink
        self.assertIn("worktree", self.inspect(True)["reason"])
        self.observer.side_effect = None
        backlink.write_text(original)
        self.assertEqual(self.inspect(True)["decision"], "cleaned")

    def assert_reader_replacement_retained(self, provisioned):
        reader = self.root / "qa"
        owner = self.state / "qa.json"
        if provisioned:
            factory.admit(owner, task="qa", repository=str(self.repo), goal="Read",
                          acceptance=["requirements.md"], corrections=0, investigations=0)
            factory.set_inputs(owner, **self.record["inputs"])
            with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
                factory.provision_worktree(owner, worktree_id="reader", destination=str(reader))
        else:
            self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        self.delivery_fixture(reader)
        self.assertEqual(self.inspect()["decision"], "eligible")
        assignment = self.record["assignments"]["qa"]
        witness = assignment.pop("launch_checkout")
        factory.save(self.path, self.record)
        original = self.path.read_bytes()
        self.assertIn("original reader worktree ownership", self.inspect(True)["reason"])
        self.assertEqual(self.path.read_bytes(), original)
        assignment["launch_checkout"] = witness
        factory.save(self.path, self.record)
        linked = factory.git_dir(reader)
        for path in (reader, linked):
            saved = self.root / ("original-" + ("admin" if path == linked else "tree"))
            path.rename(saved)
            shutil.copytree(saved, path, symlinks=True)
            self.assertNotEqual(path.stat().st_ino, saved.stat().st_ino)
        self.assertEqual(factory.git_dir(reader), linked)
        self.assertEqual(factory.git_commit(str(reader), "HEAD"), witness["head"])
        before = set(self.cache.rglob("*"))
        self.assertIn("original reader worktree ownership", self.inspect(True)["reason"])
        self.assertEqual(set(self.cache.rglob("*")), before)

    def test_replacement_reader_with_copied_provisioner_marker_retains(self):
        self.assert_reader_replacement_retained(True)

    def test_replacement_reader_without_provisioner_retains(self):
        self.assert_reader_replacement_retained(False)

    def test_replay_collected_v5_from_original_runtime_path(self):
        self.delivery_fixture(self.destination)
        successor = self.root / "successor"
        shutil.copytree(factory.ROOT, successor)
        with patch.object(factory, "ROOT", successor):
            self.assertIsNone(factory.native_harness(self.assignment["request"]["command"]))
            row = self.inspect(True)
        self.assertEqual(row["decision"], "cleaned", row)

    def test_release_confirms_git_removal_and_retains_the_dependent_cache(self):
        cwd = self.root / "qa"
        owner = self.state / "qa.json"
        factory.admit(owner, task="qa", repository=str(self.repo), goal="Read the fixture",
                      acceptance=["requirements.md"], corrections=0, investigations=0)
        factory.set_inputs(owner, **self.record["inputs"])
        with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
            factory.provision_worktree(owner, worktree_id="reader", destination=str(cwd))
        linked = factory.git_dir(cwd)
        path, delivered = self.delivery_fixture(cwd)
        self.assertEqual(self.inspect()["decision"], "eligible")
        entries = set(self.cache.rglob("*"))
        verify = release_evidence.delivered_receipt

        raw_owner = owner.read_bytes()
        self.observer.reset_mock()
        def rewrite_owner_after_replay(*args, **kwargs):
            verified = verify(*args, **kwargs)
            if self.observer.called:
                owner.write_bytes(raw_owner.replace(b"\n", b"\r\n"))
            return verified
        with patch.object(release_evidence, "delivered_receipt", side_effect=rewrite_owner_after_replay):
            self.assertEqual(self.inspect(True)["decision"], "retained")
            self.assertEqual(json.loads(owner.read_bytes()), json.loads(raw_owner))
            self.assertEqual(set(self.cache.rglob("*")), entries)
        owner.write_bytes(raw_owner)

        with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
            self.terminal.seek(0)
            self.terminal.write("qa\n")
            self.terminal.seek(0)
            released = factory.release_worktree(owner)
        self.assertFalse(released["already_released"])
        self.assertFalse(os.path.lexists(linked))
        self.assertIn("direct-user exclusion", self.inspect(True)["reason"])
        factory.save(path, {**delivered, "pr": "https://github.com/fixture/project/pull/8"})
        self.assertIn("release judgment approved", self.inspect(True)["reason"])
        factory.save(path, delivered)
        release_receipt = owner.with_suffix(".worktree-release.json")
        original = json.loads(release_receipt.read_text())
        release_receipt.unlink()
        with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
            with self.assertRaisesRegex(factory.FactoryError, "Missing original owner release"):
                factory.release_worktree(owner)
        self.assertIn("owner release outcome", self.inspect(True)["reason"])
        self.assertFalse(release_receipt.exists())
        for changes in ({"owner_token": "another-owner"}, {"worktree": str(self.destination)},
                        {"already_released": True}, {"schema_version": True}):
            with self.subTest(changes=changes):
                factory.save(release_receipt, {**original, **changes})
                self.assertIn("owner release outcome", self.inspect(True)["reason"])
        factory.save(release_receipt, original)
        self.assertEqual(set(self.cache.rglob("*")), entries)
        self.assertIn("direct-user exclusion", self.inspect(True)["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_attended_legacy_reader_release_and_retry_keep_dependent_cache(self):
        reader = self.root / "qa"
        owner = self.state / "qa.json"
        factory.admit(owner, task="qa", repository=str(self.repo), goal="Read the fixture",
                      acceptance=["requirements.md"], corrections=0, investigations=0)
        factory.set_inputs(owner, **self.record["inputs"])
        with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
            record = factory.provision_worktree(owner, worktree_id="reader", destination=str(reader))
        item = record["provisioning"]
        self.reader_fixture("qa", reader)
        self.assertEqual(self.inspect()["decision"], "eligible")
        del item["checkout_inodes"]
        factory.save(owner, record)
        original = {p: p.read_bytes() for p in (owner, self.path, self.state / "fixture.qa.codex.json")}
        entries = {p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}
        self.assertIn("Missing original provisioner ownership", self.inspect()["reason"])
        self.terminal.seek(0)
        self.terminal.truncate()
        self.terminal.write("wrong\nqa\n")
        self.terminal.seek(0)
        code, _, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 1)
        self.assertIn("unconfirmed", error)
        self.assertTrue(reader.is_dir())
        code, output, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 0, error)
        self.assertIn("never recorded checkout_inodes", error)
        self.assertFalse(json.loads(output)["already_released"])
        self.assertFalse(reader.exists())
        self.assertFalse(Path(item["git_dir"]).exists())
        self.assertEqual(self.git("rev-parse", item["request"]["branch"]), record["inputs"]["candidate"])
        released = owner.with_suffix(".worktree-release.json")
        original[released] = released.read_bytes()
        proof = json.loads(original[released])
        self.assertNotIn("checkout", proof)
        self.assertEqual(proof["observed_checkout"], self.record["assignments"]["qa"]["launch_checkout"])
        code, output, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertIn("Missing original provisioner ownership", self.inspect()["reason"])
        self.assertIn("Missing original provisioner ownership", self.inspect(True)["reason"])
        self.assertEqual({p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}, entries)
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_unrecorded_sibling_teardown_preserves_all_entries(self):
        cwd = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(cwd), self.record["inputs"]["candidate"])
        path, delivered = self.delivery_fixture(cwd)
        self.git("worktree", "remove", str(cwd))
        before = self.protected()
        entries = set(self.cache.rglob("*"))
        self.assertIn("original owner release", self.inspect(True)["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries)
        self.assertEqual(self.protected(), before)
        self.assertEqual(json.loads(path.read_text()), delivered)

    def assert_late_backlink_change_retains_cache(self, role):
        reader = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        self.delivery_fixture(reader)
        work, other = (self.destination, reader) if role == "writer" else (reader, self.destination)
        backlink = factory.git_dir(work) / "gitdir"
        entries = set(self.cache.rglob("*"))
        self.assertTrue(entries)
        verify_delivery = release_evidence.delivered_receipt

        def redirect_after_verification(*args, **kwargs):
            result = verify_delivery(*args, **kwargs)
            if self.observer.called:
                backlink.write_text(str(other / ".git") + "\n")
            return result

        with patch.object(release_evidence, "delivered_receipt", side_effect=redirect_after_verification):
            row = self.inspect(True)
        self.assertEqual(backlink.read_text().strip(), str(other / ".git"))
        self.assertEqual(row["decision"], "retained", row)
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_writer_backlink_changed_during_final_delivery_check_retains_cache(self):
        self.assert_late_backlink_change_retains_cache("writer")

    def test_reader_backlink_changed_during_final_delivery_check_retains_cache(self):
        self.assert_late_backlink_change_retains_cache("reader")

    def test_live_process_busy_locks_and_other_line_reuse(self):
        self.observer.side_effect = factory.FactoryError("Process 42 has an open path")
        self.assertIn("Process 42", self.inspect(True)["reason"])
        for lock in (factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"),
                     go_cache.inode_lock(self.admin, shared=True)):
            with lock:
                self.assertIn("Busy lock", self.inspect(True)["reason"])
        other = factory.new_task("other", "other-line")
        other["assignments"]["pending"] = {"request": self.assignment["request"]}
        factory.save(self.state / "other.json", other)
        self.assertIn("reuse by other/pending", self.inspect(True)["reason"])
        self.assertTrue(list(self.cache.glob("*/*-a")))

    def test_detached_user_then_inactive_and_warm_cache(self):
        # This workload closes its cache files and waits outside the tree. It can still
        # write into its inherited cache after the synthetic Rex exit.
        program = ("import os, sys; from pathlib import Path; "
                   "sys.stdin.buffer.read(1); (Path(os.environ['GOCACHE'])/'log.txt').write_text('resumed')")
        child = subprocess.Popen([sys.executable, "-c", program], cwd=self.root, stdin=subprocess.PIPE,
                                 env={"GOCACHE": str(self.cache)}, start_new_session=True)
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)

        def observe(*args):
            own = os.getpid()
            ps = f"{own} Sun Sep 13 12:00:00 2020 census\n"
            listing = f"p{own}\0\nfcwd\0tDIR\0n/elsewhere\0\n".encode()
            ps += "41 Sun Sep 13 11:00:00 2020 older-workload\n"
            listing += b"p41\0\nfcwd\0tDIR\0n/elsewhere\0\n"
            if child.poll() is None:
                ps += f"{child.pid} Sun Sep 13 12:26:00 2020 helper\n"
                listing += f"p{child.pid}\0\nfcwd\0tDIR\0n{self.root}\0\n".encode()
            with patch.object(go_cache, "boot_time", return_value=self.at - 3600), patch.object(
                    go_cache, "inventory", side_effect=[(7, ps.encode(), b""), (8, listing, b"")]):
                return self.process_check(*args)
        self.observer.side_effect = observe
        self.assertIn("detached cache user", self.inspect(True)["reason"])
        child.communicate(b"x", timeout=10)
        self.assertEqual(child.returncode, 0)
        self.assertEqual((self.cache / "log.txt").read_text(), "resumed")
        self.set_cache_time(self.at)
        self.assertEqual(self.inspect(True)["decision"], "cleaned")
        self.build()
        with patch.object(go_cache, "MIN_KIB", 1024 * 1024):
            self.assertIn("size floor", self.inspect(True)["reason"])

    def test_standalone_census_from_existing_shared_entry_is_read_only(self):
        data = self.root / "shared-data"
        installed = data / "software-factory/skill/runtime"
        shutil.copytree(factory.ROOT, installed, ignore=shutil.ignore_patterns("__pycache__"))
        before = {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in data.rglob("*")}
        records = self.protected()
        result = subprocess.run([sys.executable, "-B", str(installed / "factory.py"), "--state-dir", str(self.state),
                                 "go-caches", "--repo", str(self.repo)], check=True, capture_output=True, text=True,
                                env={**os.environ, "XDG_DATA_HOME": str(data)})
        self.assertEqual(json.loads(result.stdout)[0]["cache"], str(self.cache))
        self.assertEqual(before, {p: (p.stat().st_ino, p.stat().st_mtime_ns) for p in data.rglob("*")})
        self.assertEqual(self.protected(), records)

    def test_foreign_request_matches_the_physical_admin(self):
        other = factory.new_task("other", "other-line")
        alias = self.root / "alternate-admin"
        alias.symlink_to(self.admin, target_is_directory=True)
        for value in (alias, self.admin.with_name(self.admin.name.upper())):
            if not value.exists():  # The second spelling is an alias on case-insensitive volumes.
                continue
            command = self.assignment["request"]["command"].copy()
            command[command.index("--worktree-git-dir") + 1] = str(value)
            other["assignments"]["pending"] = {"request": {"cwd": str(self.repo), "command": command}}
            factory.save(self.state / "other.json", other)
            self.assertIn("reuse by other/pending", self.inspect(True)["reason"])

    def test_receipt_requires_original_harness_and_frozen_request(self):
        extra = self.state / "fixture.writer.claude.json"
        factory.save(extra, {**self.native, "kind": "claude-execution", "state": "starting"})
        self.assertIn(extra.name, self.inspect(True)["reason"])
        extra.unlink()
        alias = self.root / "receipt-admin"
        alias.symlink_to(self.admin, target_is_directory=True)
        other = factory.new_task("other", "other-line")
        other["assignments"]["writer"] = {"request": {"cwd": str(self.repo), "command": [sys.executable]}}
        factory.save(self.state / "other.json", other)
        foreign = self.state / "other.writer.codex.json"
        factory.save(foreign, {**self.native, "job": "other/writer", "cwd": str(self.repo),
                               "worktree_git_dir": str(alias)})
        self.assertIn(foreign.name, self.inspect(True)["reason"])
        unknown = foreign.with_name("other.writer.unrecognized.json")
        foreign.rename(unknown)
        self.assertIn(unknown.name, self.inspect(True)["reason"])
        unknown.unlink()
        for key, value in (("requested_model", "other"), ("writer", False), ("events", "/elsewhere")):
            with self.subTest(key=key):
                factory.save(self.receipt, {**self.native, key: value})
                self.assertIn("inconsistent terminal", self.inspect(True)["reason"])
        factory.save(self.receipt, self.native)
        self.assertEqual(self.inspect()["decision"], "eligible")

    def test_lost_owner_requires_attended_release_and_never_certifies_exclusion(self):
        self.assignment["closed"].update(state="lost", exit=None, reason="Rex restarted")
        self.native.update(state="starting")
        factory.save(self.receipt, self.native)
        factory.save(self.path, self.record)
        before = self.protected()
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"), \
                self.assertRaisesRegex(factory.FactoryError, "window unconfirmed"):
            factory.release_worktree(self.path)
        self.assertEqual(self.protected(), before)
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"):
            self.terminal.write("fixture\n")
            self.terminal.seek(0)
            factory.release_worktree(self.path)
        proof = json.loads(self.path.with_suffix(".worktree-release.json").read_bytes())
        self.assertNotIn("exclusive", proof)

    def test_reset_owner_can_release_without_certifying_exclusion(self):
        self.assignment["closed"].update(state="failed", exit={"exit_code": 1})
        self.assignment.update(result=None, native_collected=False)
        self.native.update(state="starting", exit_code=None, candidate=None)
        factory.save(self.receipt, self.native)
        factory.save(self.path, self.record)
        patch_path = factory.reset_patch(self.receipt)
        factory.save(patch_path, "original private patch")
        self.terminal.write("fixture\n")
        self.terminal.seek(0)
        with factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"):
            factory.release_worktree(self.path)
        self.assertFalse(self.destination.exists())
        self.assertEqual(patch_path.read_text(), "original private patch")
        self.assertNotIn("exclusive", json.loads(self.path.with_suffix(".worktree-release.json").read_bytes()))

    def test_foreign_store_visible_between_intent_and_rex_start(self):
        from test_rex_launcher import FakeRex
        other = self.root / "second-store"
        other.mkdir(mode=0o700)
        case = self
        class ObservedRex(FakeRex):
            def call(self, *args, **kwargs):
                if args[0] == "new":
                    case.assertEqual(factory.load_task(other / "another.json")["assignments"]["reader"]["state"],
                                     "launching")
                    case.assertIn("state store", case.inspect(True)["reason"])
                return super().call(*args, **kwargs)

        with patch.object(factory, "Rex", ObservedRex), patch.object(FakeRex, "sessions", []), \
                patch.object(FakeRex, "calls", []):
            _, attempt = factory.launch(other / "another.json", task="another", assignment_id="reader",
                role="qa", goal="Inspect", cwd=str(self.destination), command=[sys.executable, "{instructions}"],
                binary=sys.executable, server="unix:///fixture")
        self.assertEqual(attempt["state"], "launched")
        self.assertIn("state store", self.inspect(True)["reason"])

    def test_mount_checked_before_size_and_again_on_open_shard(self):
        with patch.object(go_cache, "check_mounts", side_effect=factory.FactoryError("fixture mount")), \
                patch.object(go_cache, "cache_contents") as walk:
            self.assertIn("fixture mount", self.inspect(True)["reason"])
            walk.assert_not_called()
        shard = (self.cache / "ab").stat()
        mounted = False
        identify = go_cache.mount_id
        listdir = os.listdir
        clean = go_cache.cache_contents

        def identify_open(fd):
            return "other mount" if mounted and os.path.samestat(os.fstat(fd), shard) else identify(fd)

        def list_on_mount(fd):
            if isinstance(fd, int) and mounted:
                self.assertFalse(os.path.samestat(os.fstat(fd), shard))
            return listdir(fd)

        def during_walk(fd, mount, **kwargs):
            nonlocal mounted
            mounted = bool(kwargs.get("clean"))
            return clean(fd, mount, **kwargs)

        with patch.object(go_cache, "cache_contents", side_effect=during_walk), \
                patch.object(go_cache, "mount_id", side_effect=identify_open), patch.object(os, "listdir", list_on_mount):
            self.assertIn("Mount or identity changed", self.inspect(True)["reason"])
        self.assertTrue((self.cache / "ab").is_dir())

    def test_size_pass_skips_leaf_open_but_cleanup_checks_its_mount(self):
        artifact = next(self.cache.glob("*/*-a"))
        original = artifact.read_bytes()
        identity = artifact.stat()
        mount_id = go_cache.mount_id
        checks = []

        def other_mount(fd):
            if os.path.samestat(os.fstat(fd), identity):
                checks.append(fd)
                return "fixture different mount"
            return mount_id(fd)

        with patch.object(go_cache, "mount_id", side_effect=other_mount):
            row = self.inspect(True)
        self.assertGreater(row["size_kib"], 0)
        self.assertIn("Mount or identity changed at cache entry", row["reason"])
        self.assertEqual(len(checks), 1)
        self.assertEqual(artifact.read_bytes(), original)

    def test_lost_owner_changed_root_and_unexpected_contents(self):
        (self.cache / "unknown").symlink_to(self.destination / "tool")
        self.assertIn("unexpected cache entry", self.inspect(True)["reason"])
        (self.cache / "unknown").unlink()
        self.cache.chmod(0o755)
        self.assertIn("0700", self.inspect(True)["reason"])
        self.cache.chmod(0o700)
        saved = self.cache.with_name("saved")
        self.cache.rename(saved)
        self.cache.symlink_to(saved, target_is_directory=True)
        self.assertIn("changed path", self.inspect(True)["reason"])
        self.cache.unlink()
        saved.rename(self.cache)
        self.path.unlink()
        self.assertIn("one provisioned task", self.inspect(True)["reason"])

    def test_interrupt_after_partial_cleanup_is_reported_and_retry_rechecks(self):
        unlink = os.unlink
        def interrupted(name, *, dir_fd):
            unlink(name, dir_fd=dir_fd)
            timestamp = self.at + go_cache.IDLE_SECONDS
            os.utime(dir_fd, (timestamp, timestamp))
            raise OSError("fixture interruption")

        before = self.protected()
        with patch.object(go_cache.os, "unlink", side_effect=interrupted):
            row = self.inspect(True)
        self.assertEqual(row["decision"], "failed")
        self.assertIn("may be partial", row["reason"])
        self.assertEqual(self.protected(), before)
        self.assertIn("24 hours", self.inspect(True)["reason"])
        self.set_cache_time(self.at)
        self.assertEqual(self.inspect(True)["decision"], "cleaned")


    def test_census_keeps_reader_and_leaves_old_task_bytes_unchanged(self):
        reader = self.root / "reader"
        self.git("worktree", "add", "--quiet", "--detach", str(reader))
        other = factory.git_dir(reader) / "go-build"
        other.mkdir(mode=0o700)
        (other / "README").write_bytes((self.cache / "README").read_bytes())
        if sys.platform == "darwin":
            factory.exclude_from_time_machine(other)
        self.record.pop("acceptance", None)
        self.record["schema_version"] = 10
        self.assignment.pop("native_launch", None)
        self.assignment.pop("interpreter", None)
        factory.save(self.path, self.record)
        saved = self.protected()
        status, out, err = self.cli("go-caches", "--repo", str(self.repo))
        self.assertEqual(status, 0, err)
        rows = {row["cache"]: row for row in json.loads(out)}
        self.assertEqual(rows[str(self.cache)]["decision"], "eligible")
        self.assertIn("one provisioned task", rows[str(other)]["reason"])
        self.assertEqual(self.protected(), saved)
        self.assertEqual(self.inspect(True)["decision"], "cleaned")
        self.assertEqual(self.protected(), saved)

    def test_schema_eight_retains_for_missing_exit_without_migrating(self):
        self.record["schema_version"] = 8
        self.assignment.pop("native_launch", None)
        self.assignment.pop("interpreter", None)
        self.record.pop("acceptance", None)
        del self.assignment["closed"], self.assignment["native_collected"]
        factory.save(self.path, self.record)
        before = self.protected()
        for clean in (False, True):
            self.assertIn("observed Rex process exit", self.inspect(clean)["reason"])
            self.assertEqual(self.protected(), before)

    def test_surviving_writer_with_release_receipt_retains_every_entry(self):
        factory.save(self.path.with_suffix(".worktree-release.json"), "conflicting release")
        entries = set(self.cache.rglob("*"))
        self.assertIn("surviving writer checkout", self.inspect(True)["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_replaced_lock_or_owner_marker_retains(self):
        def replace_lock(*_):
            lock = self.state / "fixture.lock"
            lock.rename(self.state / "previous.lock")
            lock.touch(mode=0o600)
            return {"observed_processes": 1}

        self.observer.side_effect = replace_lock
        self.assertIn("lock identity changed", self.inspect(True)["reason"])
        (self.admin / "locked").write_text("different owner")
        self.assertIn("ownership differs", self.inspect(True)["reason"])
        self.assertTrue(list(self.cache.glob("*/*-a")))

    def test_archived_failure_preceding_collected_recovery_is_terminal(self):
        prior = copy.deepcopy(self.assignment)
        path = self.state / "fixture.earlier.codex.json"
        command = prior["request"]["command"]
        command[command.index("--job") + 1] = "fixture/earlier"
        command[command.index("--receipt") + 1] = str(path)
        command[command.index("--events") + 1] = str(path.with_suffix(".jsonl"))
        brief = self.state / "fixture.earlier.md"
        factory.save(brief, "Build the fixture")
        prior.update(brief=str(brief), native_collected=False, result=None, window_label="backend-earlier-abcd1234",
                     window_id="earlier-window", block_id="earlier-block")
        prior["closed"].update(state="failed", exit={"exit_code": 1})
        self.attest(prior)
        self.record["assignments"] = {"earlier": prior, **self.record["assignments"]}
        factory.save(self.path, self.record)
        factory.save(path, {**self.native, "job": "fixture/earlier", "state": "failed", "report": None,
                           "events": str(path.with_suffix(".jsonl")), "exit_code": 1})
        row = self.inspect()
        self.assertEqual((row["decision"], row["attempts"]), ("eligible", 2))
        path.unlink()
        self.assertIn(path.name, self.inspect(True)["reason"])

    def test_collected_worker_receipt_with_private_native_child(self):
        # The inert transport reports completion while its PID stays occupied. Only
        # the test owns and reaps that child. The worker and collector write all evidence.
        class OccupiedProcess(FakeProcess):
            def __init__(self, pid):
                super().__init__(events())
                self.pid = pid

        brief = self.state / "fixture.qa.md"
        factory.save(brief, "# Assignment: qa\nRead the fixture")
        digest = hashlib.sha256(brief.read_bytes()).hexdigest()
        command = factory.codex_command(self.path, task="fixture", assignment_id="qa", codex=sys.executable,
                                        model="fixture", effort="high", cwd=str(self.destination), role="qa")
        req: dict = dict(self.assignment["request"], inputs=self.record["inputs"], instructions_sha256=digest,
                         command=command, role="qa")
        assignment = dict(self.assignment, request=req, brief=str(brief), result=None, native_collected=False,
                          window_label="qa-qa-abcd1234", window_id="qa", block_id="qa")
        assignment["launch_checkout"] = go_cache.checkout_binding(
            self.destination, self.admin, self.record["admission"]["repository_identity"])
        self.record["assignments"]["qa"] = assignment
        self.attest(assignment)
        factory.save(self.path, self.record)
        args = factory.evidence_worker_args("codex", codex_worker.parser(), "qa", self.record, assignment, replay=False)
        args.cwd, args.repo, args.candidate = self.destination, self.repo, req["inputs"]["candidate"]
        args.repo_identity = json.dumps(self.record["admission"]["repository_identity"])
        args.brief, args.brief_sha256, args.requirements = brief, digest, req["inputs"]["requirements"]
        with subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read()"],
                              stdin=subprocess.PIPE, cwd=self.root) as child:
            async def spawn(*argv, **kwargs):
                return OccupiedProcess(child.pid)

            try:
                with chdir(self.destination):
                    receipt = asyncio.run(codex_worker.execute(args, spawn=spawn, mcp_servers=lambda *_: [],
                                                               clock=lambda: self.at - 1))
                self.assertEqual(receipt["state"], "completed", receipt.get("error"))
                collected = factory.collect_codex(self.path, "qa", None, self.record, assignment)
                self.assertTrue(collected["assignments"]["qa"]["native_collected"])
                self.assertEqual(collected["assignments"]["qa"]["closed"]["exit"], {"exit_code": 0})
                if hasattr(factory, "SCRATCH_RECEIPT_VERSION"):
                    self.assertEqual(receipt["schema_version"], getattr(factory, "SCRATCH_RECEIPT_VERSION"))
                    self.assertEqual(receipt["native_process"]["pid"], child.pid)
                    row, = go_cache.census(self.state, self.repo)
                    self.assertIn("native process is active", row["reason"])
            finally:
                child.communicate(input=b"", timeout=10)
        before = self.protected()
        row, = go_cache.census(self.state, self.repo)
        self.assertEqual(row["decision"], "eligible", row)
        self.assertEqual(self.protected(), before)
        if "native_process" in receipt:
            for change in ({"scope": "different-boot"}, {"pid": False}, {"start_time": -1}):
                with self.subTest(change=change):
                    factory.save(args.receipt, {**receipt, "native_process": {**receipt["native_process"], **change}})
                    row, = go_cache.census(self.state, self.repo)
                    self.assertEqual(row["decision"], "retained", row)
                    self.assertIn("cannot be verified", row["reason"])

    def test_original_process_receipt_requires_its_own_runtime_and_pid_proof(self):
        self.native.update(schema_version=7, native_process=None)
        factory.save(self.receipt, self.native)
        with patch.object(factory, "SCRATCH_RECEIPT_VERSION", 7, create=True), \
                patch.object(factory, "process_scope", return_value="original-boot", create=True), \
                patch.object(factory, "process_proven_gone", return_value=False, create=True) as gone:
            self.assertIn("cannot be verified", self.inspect(True)["reason"])
            gone.assert_called_once_with(None, "original-boot", 0)
        self.native["schema_version"] = 999
        factory.save(self.receipt, self.native)
        self.assertIn("receipt version", self.inspect(True)["reason"])

    def test_owner_newlines_change_during_observation_retains_all_entries(self):
        entries = set(self.cache.rglob("*"))
        original = self.path.read_bytes()
        changed = original.replace(b"\n", b"\r\n")
        self.assertNotEqual(original, changed)
        self.assertEqual(json.loads(original), json.loads(changed))
        def rewrite(*_):
            self.path.write_bytes(changed)
            return {}
        self.observer.side_effect = rewrite
        self.assertEqual(self.inspect(True)["decision"], "retained")
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_write_at_return_from_final_delivery_replay_retains_all_entries(self):
        self.delivery_fixture(self.destination)
        entries = set(self.cache.rglob("*"))
        new = self.cache / "aa" / ("aa" + "0" * 62 + "-d")
        verify = release_evidence.delivered_receipt
        def write_after_replay(*args, **kwargs):
            verified = verify(*args, **kwargs)
            if self.observer.called:
                new.write_bytes(b"inert build output")
            return verified
        with patch.object(release_evidence, "delivered_receipt", side_effect=write_after_replay):
            row = self.inspect(True)
        self.assertIn("Cache changed during observation", row["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries | {new})
        self.assertEqual(new.read_bytes(), b"inert build output")

    def test_unattended_selection_retains_even_when_all_samples_are_idle(self):
        entries = set(self.cache.rglob("*"))
        self.terminal.isatty = lambda: False
        self.assertEqual(self.inspect()["decision"], "eligible")
        # A direct writer can enter after the last sample even with the launch lock held.
        shard = self.cache / "aa" / ("aa" + "1" * 62 + "-d")
        def direct_writer():
            with self.assertRaises(factory.ExecutionBusy), go_cache.inode_lock(self.admin, shared=True):
                pass
            subprocess.run([sys.executable, "-c", "import pathlib, sys; pathlib.Path(sys.argv[1]).write_bytes(b'build')",
                            str(shard)], cwd=self.root, check=True)
            return False
        self.terminal.isatty = direct_writer
        row = self.inspect(True)
        self.assertEqual(row["decision"], "retained")
        self.assertIn("attended exclusive maintenance window", row["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries | {shard})
        self.assertEqual(shard.read_bytes(), b"build")

    def test_last_observation_rechecks_cache_writes_and_inode(self):
        def write(*_):
            timestamp = self.at + go_cache.IDLE_SECONDS
            os.utime(next(self.cache.glob("*/*-a")), (timestamp, timestamp))
            return {"observed_processes": 1}

        self.observer.side_effect = write
        self.assertIn("changed during observation", self.inspect(True)["reason"])
        self.set_cache_time(self.at)

        def replace(*_):
            self.cache.rename(self.cache.with_name("original"))
            self.cache.mkdir(mode=0o700)
            return {"observed_processes": 1}

        self.observer.side_effect = replace
        self.assertIn("identity changed", self.inspect(True)["reason"])
        self.assertTrue(list((self.admin / "original").glob("*/*-a")))

    def test_concurrent_launch_cannot_publish_an_intent_or_spend_an_attempt(self):
        from test_rex_launcher import FakeRex
        other = self.state / "another.json"
        alias = self.root / "alternate"
        alias.mkdir()
        (alias / ".git").write_bytes((self.destination / ".git").read_bytes())
        with patch.object(factory, "Rex", FakeRex), patch.object(FakeRex, "sessions", []), \
                patch.object(FakeRex, "calls", []), go_cache.inode_lock(self.admin):
            with self.assertRaisesRegex(factory.ExecutionBusy, "Busy lock"):
                factory.launch(other, task="another", assignment_id="reader", role="qa", goal="Inspect",
                               cwd=str(alias), command=[sys.executable, "{instructions}"],
                               binary=sys.executable, server="unix:///fixture")
            self.assertFalse(other.exists())
            self.assertFalse((self.state / "another.reader.md").exists())
            self.assertFalse(FakeRex.sessions)


class ActivityObservation(unittest.TestCase):
    def test_native_aging_refuses_a_remote_primary_mount(self):
        repo = Path("/fixture/repo")
        table = "server:/home on / (nfs)\n"
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, table, "")), \
                self.assertRaisesRegex(factory.FactoryError, "local filesystem"):
            go_cache.check_mounts(repo, Path("/fixture/work"), repo / ".git/worktrees/work/go-build",
                                  native_mount=(b"fixture", b"/\0"))

    def test_same_device_mount_under_a_shard_is_retained(self):
        repo = Path("/fixture/repo")
        cache = repo / ".git/worktrees/linked/go-build"
        table = f"/disk on / (apfs, local)\n/disk on {cache}/ab (apfs, local)\n"
        with patch.object(sys, "platform", "darwin"), patch.object(subprocess, "run",
                return_value=subprocess.CompletedProcess([], 0, table, "")), \
                self.assertRaisesRegex(factory.FactoryError, "Unexpected mount"):
            go_cache.check_mounts(repo, Path("/fixture/linked"), cache)

    def test_missing_process_cwd_is_never_a_scheduled_permit(self):
        own = os.getpid()
        ps = f"{own} Sun Sep 13 12:00:00 2020 census\n42 Sun Sep 13 12:00:00 2020 login\n".encode()
        listing = f"p{own}\0\nfcwd\0tDIR\0n/elsewhere\0\np42\0\n".encode()
        work, admin = Path("/fixture/tree"), Path("/fixture/admin")
        def observations():
            return patch.object(go_cache, "inventory", side_effect=[(7, ps, b""), (8, listing, b"")])
        with patch.object(go_cache, "boot_time", return_value=100), patch.object(os, "kill"):
            with observations(), self.assertRaisesRegex(factory.FactoryError, "working directory of process 42"):
                go_cache.process_check(work, admin, "example", [])
            with observations(), patch.object(sys, "stderr", StringIO()) as diagnostic:
                go_cache.process_check(work, admin, "example", [], attended_release=True)
                self.assertIn("requires independent exclusion", diagnostic.getvalue())

    def test_user_file_and_process_inventory(self):
        work, admin = Path("/fixture/tree"), Path("/fixture/admin")
        ps = f"{os.getpid()} Sun Sep 13 12:00:00 2020 census\n42 Sun Sep 13 12:00:00 2020 native\n".encode()
        own = f"p{os.getpid()}\0\nfcwd\0tDIR\0n/elsewhere\0\n".encode()
        for listing, expected in [
            (b"p42\0\nfcwd\0tDIR\0n/fixture/tree\0\n", "open path"),
            (b"p42\0\nfcwd\0tDIR\0n/elsewhere\0\nf4\0tREG\0n/fixture/admin/go-build/x\0\n", "open path"),
            (b"p42\0\nfcwd\0tDIR\0\n", "Cannot verify"),
            (b"p42\0\nfcwd\0tDIR\0n/elsewhere\0\n", None),
        ]:
            with self.subTest(listing=listing), patch.object(go_cache, "boot_time", return_value=100), patch.object(go_cache, "inventory", side_effect=[
                    (7, ps, b""), (8, own + listing, b"")]):
                if expected:
                    with self.assertRaisesRegex(factory.FactoryError, expected):
                        go_cache.process_check(work, admin, "example", [(10, 20)])
                else:
                    self.assertEqual(go_cache.process_check(work, admin, "example", [(10, 20)])["observed_processes"], 2)
