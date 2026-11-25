"""Cache retention in real disposable linked repositories. Rex observations are fixtures."""
import runtime_path  # noqa: F401
import asyncio
from contextlib import chdir
import copy
import fcntl
import hashlib
import shutil
import json
import os
from pathlib import Path
from io import StringIO
import signal
import socket
import stat
import subprocess
import time
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

    def build(self):
        subprocess.run(["go", "build", str(self.file)], cwd=self.destination, env={
            "HOME": str(self.root), "TMPDIR": str(self.root), "PATH": os.environ["PATH"],
            "GOCACHE": str(self.cache), "GOTOOLCHAIN": "local", "GOENV": "off",
            "GOTELEMETRY": "off", "GOWORK": "off", "GO111MODULE": "off", "GOPROXY": "off"}, check=True)

    def set_cache_time(self, timestamp):
        for path in [self.cache, *self.cache.rglob("*")]:
            os.utime(path, (timestamp, timestamp))

    def inspect(self):
        status, out, err = self.cli("go-caches", "--repo", str(self.repo))
        self.assertTrue(out, err)
        row, = json.loads(out)
        self.assertEqual(status, 0, row)
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

    def test_parent_only_sigterm_cancels_the_gated_native_compiler(self):
        # This disposable tool gate pauses the real Go driver while it waits for its compiler.
        fixture = self.root / "disposable-trim-go"
        ready = self.root / "disposable-compiler-ready"
        gate = self.root / "disposable-compiler-gate"
        diagnostic = self.root / "disposable-go.stderr"
        go = shutil.which("go")
        fixture.write_text(f"""#!{sys.executable}
import fcntl, os, sys
from pathlib import Path
if sys.argv[1] == "build":
    # Keep fixture diagnostics readable after the CLI's stderr pipe loses its reader.
    with open({str(diagnostic)!r}, "w") as log:
        os.dup2(log.fileno(), 2)
    os.environ["PATH"] = {str(self.root)!r} + os.pathsep + os.defpath
    sys.argv.insert(2, "-toolexec={fixture.name}")
elif sys.argv[1] != "env":
    with open({str(gate)!r}) as gate:
        Path({str(ready)!r}).touch()
        fcntl.flock(gate, fcntl.LOCK_SH)
    os.execv(sys.argv[1], sys.argv[1:])
os.execv({go!r}, [{go!r}, *sys.argv[1:]])
""")
        fixture.chmod(0o700)
        old = self.cache / "ab" / ("ab" + "2" * 62 + "-d")
        old.write_bytes(b"disposable old entry")
        os.utime(old, (self.at, self.at))
        (self.cache / "trim.txt").write_text(str(self.at))
        paths = (factory.execution_lock_path(self.state, factory.task_line(self.record)),
                 self.path.with_suffix(".lock"), self.admin, self.cache)
        with gate.open("w") as hold:
            fcntl.flock(hold, fcntl.LOCK_EX)
            with subprocess.Popen([sys.executable, factory.__file__, "--state-dir", str(self.state),
                                   "go-caches", "--repo", str(self.repo), "--trim-go", str(fixture)],
                                  env={**os.environ, "TMPDIR": str(self.root)}, stdout=subprocess.PIPE) as parent:
                try:
                    deadline = time.monotonic() + 30
                    while not ready.exists():
                        self.assertIsNone(parent.poll(), "Factory exited before the disposable compiler gate")
                        self.assertLess(time.monotonic(), deadline, "Go did not reach the disposable compiler gate")
                        time.sleep(0.02)
                finally:
                    parent.terminate()
                self.assertEqual(parent.communicate(timeout=10), (b"", None))
                self.assertEqual(parent.returncode, -signal.SIGTERM)
            # Keep the compiler gate closed. Cancellation must release the locks
            # independently of both the gate and the normal build deadline.
            deadline = time.monotonic() + 5
            while True:
                try:
                    with go_cache.inode_lock(self.cache):
                        break
                except factory.ExecutionBusy:
                    self.assertLess(time.monotonic(), deadline, "Go retained the disposable cache lock")
                    time.sleep(0.02)
            self.assertTrue(old.exists(), diagnostic.read_text())
            for path in paths:
                with go_cache.inode_lock(path, directory=path.is_dir()):
                    pass

    def test_native_go_process_failures(self):
        fixture = self.root / "disposable-go"
        ready = self.root / "compiler-ready"
        exit_driver = self.root / "exit-driver"
        entry = next(self.cache.glob("*/*-d"))
        fixture.write_text(f"""#!{sys.executable}
import fcntl, os, sys, time
from pathlib import Path
if sys.argv[1] == "env":
    sys.stdout.write("\\n\\n")
    sys.exit(0)
compiler = os.fork()
if compiler:
    while not Path({str(exit_driver)!r}).exists():
        if os.waitpid(compiler, os.WNOHANG)[0]:
            sys.exit(0)
        time.sleep(0.01)
    sys.exit(7)
os.closerange(0, os.sysconf("SC_OPEN_MAX"))
with open({str(entry)!r}, "ab", buffering=0) as cache:
    fcntl.flock(cache, fcntl.LOCK_EX)
    Path({str(ready)!r}).touch()
    until = time.monotonic() + 20
    while time.monotonic() < until:
        cache.write(b".")
        time.sleep(0.02)
""")
        fixture.chmod(0o700)
        paths = (factory.execution_lock_path(self.state, factory.task_line(self.record)),
                 self.path.with_suffix(".lock"), self.admin, self.cache)
        for failure in ("timeout", "driver exit", "parent kill", "parent interrupt", "group teardown"):
            with self.subTest(failure=failure):
                # A session-leading Factory can have unrelated peers. This peer echoes
                # a byte after readback to prove it survived the hosted timeout.
                probe, peer = socket.socketpair()
                with probe, peer:
                    launch = f"""import os, subprocess, sys
import factory, go_cache
subprocess.Popen(["/bin/cat"], stdin={peer.fileno()}, stdout={peer.fileno()})
os.close({peer.fileno()})
go_cache.GO_BUILD_SECONDS = {2 if failure == "timeout" else 30}
raise SystemExit(factory.main())
"""
                    cli = [sys.executable, "-B", "-c", launch, "--state-dir", str(self.state),
                           "go-caches", "--repo", str(self.repo), "--trim-go", str(fixture)]
                    with subprocess.Popen(cli, cwd=factory.ROOT, env={**os.environ, "TMPDIR": str(self.root)},
                                          start_new_session=True, pass_fds=(peer.fileno(),),
                                          stdout=subprocess.PIPE) as caller:
                        peer.close()
                        deadline = time.monotonic() + 10
                        while not ready.exists():
                            self.assertIsNone(caller.poll(), "Factory exited before the compiler wrote")
                            self.assertLess(time.monotonic(), deadline)
                            time.sleep(0.01)
                        for path in paths:
                            with self.assertRaises(factory.ExecutionBusy), go_cache.inode_lock(path, directory=path.is_dir()):
                                self.fail(f"Acquired {path} during a compiler write")
                        if failure == "parent kill":
                            caller.kill()
                        elif failure == "parent interrupt":
                            caller.send_signal(signal.SIGINT)
                        elif failure == "group teardown":
                            os.killpg(caller.pid, signal.SIGKILL)
                        elif failure == "driver exit":
                            exit_driver.touch()
                        # At the first opportunity to acquire each admission lock, the
                        # compiler must already have released its separate mutation flock.
                        deadline = time.monotonic() + 5
                        with entry.open("rb") as mutation:
                            pending = set(paths)
                            while pending:
                                for path in pending.copy():
                                    try:
                                        with go_cache.inode_lock(path, directory=path.is_dir()):
                                            fcntl.flock(mutation, fcntl.LOCK_SH | fcntl.LOCK_NB)
                                            pending.remove(path)
                                    except factory.ExecutionBusy:
                                        continue
                                self.assertLess(time.monotonic(), deadline, f"Locks retained after stop: {pending}")
                                time.sleep(0.01)
                        output, _ = caller.communicate(timeout=6)
                        if failure in ("timeout", "driver exit"):
                            row, = json.loads(output)
                            self.assertEqual((caller.returncode, row["decision"], row["go_exit_code"]),
                                             (1, "failed", 124 if failure == "timeout" else 7), row)
                            self.assertEqual(row["removed_entries"], 0)
                            self.assertGreater(row["after_kib"], 0)
                            if failure == "timeout":
                                self.assertIn("exceeded its 2-second deadline", row["reason"])
                            probe.settimeout(2)
                            probe.sendall(b"?")
                            self.assertEqual(probe.recv(1), b"?")
                        elif failure == "parent interrupt":
                            self.assertEqual(caller.returncode, -signal.SIGINT)
                        else:
                            self.assertEqual(caller.returncode, -signal.SIGKILL)
                ready.unlink()
                exit_driver.unlink(missing_ok=True)

    def test_native_aging_retains_cache_under_replaceable_ancestor(self):
        self.root.chmod(0o770)
        with patch.object(go_cache, "native_build") as build:
            row = self.trim()
            self.assertEqual(row["decision"], "retained")
            self.assertIn(f"replaceable directory {self.root}", row["reason"])
            build.assert_not_called()

    def test_native_aging_refuses_foreign_owner_of_mode_0700_ancestor(self):
        root = self.root.stat()
        fstat = os.fstat

        def fixture_owner(fd):
            info = fstat(fd)
            if os.path.samestat(info, root):
                fields = list(info)
                fields[stat.ST_UID] = os.getuid() + 1  # Fixture owner could chmod this ancestor.
                return os.stat_result(fields)
            return info

        with patch.object(os, "fstat", side_effect=fixture_owner), patch.object(go_cache, "native_build") as build:
            row = self.trim()
            self.assertEqual(row["decision"], "retained")
            self.assertIn(f"replaceable directory {self.root}", row["reason"])
            build.assert_not_called()

    @unittest.skipUnless(sys.platform == "darwin", "Darwin extended ACLs")
    def test_native_aging_checks_acl_grants_on_cache_and_ancestors(self):
        subprocess.run(["/bin/chmod", "+a", "everyone deny delete", self.root], check=True)
        self.addCleanup(subprocess.run, ["/bin/chmod", "-a", "everyone deny delete", self.root], check=True)
        self.assertEqual(self.trim()["go_exit_code"], 0)
        for path, add in ((self.cache, "+ai"), (self.root, "+a")):
            with self.subTest(path=path), patch.object(go_cache, "native_build") as build:
                grant = "group:staff allow list,search"
                subprocess.run(["/bin/chmod", add, grant, path], check=True)
                try:
                    row = self.trim()
                    self.assertEqual(row["decision"], "retained")
                    self.assertIn(f"granting ACL at {path}", row["reason"])
                    build.assert_not_called()
                finally:
                    subprocess.run(["/bin/chmod", "-a", grant, path], check=True)

    def test_native_aging_refuses_distribution_cache_backend(self):
        executable = self.root / "fixture-go"
        executable.write_text("#!/bin/sh\nif [ \"$1\" = env ]; then printf '/fixture/backend\\n\\n'; "
                              "else touch \"$GOCACHE/build-ran\"; fi\n")
        executable.chmod(0o700)
        row = self.trim(str(executable), status=1)
        self.assertIn("GOCACHEPROG", row["reason"])
        self.assertFalse((self.cache / "build-ran").exists())

    def test_native_aging_leaves_a_cache_without_entries_alone(self):
        for shard in self.cache.iterdir():
            if shard.is_dir():
                shutil.rmtree(shard)
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

    def test_terminal_receipt_bounds(self):
        self.native["state"] = "starting"
        factory.save(self.receipt, self.native)
        self.assertIn("inconsistent terminal", self.inspect()["reason"])
        self.native["state"] = "failed"
        factory.save(self.receipt, self.native)
        self.assertIn("inconsistent terminal", self.inspect()["reason"])
        self.assignment["closed"].update(state="lost", exit=None, reason="Rex restarted")
        factory.save(self.path, self.record)
        self.assertIn("Rex process exit", self.inspect()["reason"])

    def test_promised_objects_must_be_local_for_census(self):
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
            with self.subTest(field=field):
                row = self.inspect()
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
            self.assertEqual(self.inspect()["decision"], "eligible")
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
        row = self.inspect()
        self.assertEqual(row["decision"], "retained")
        self.assertIn("release judgment approved", row["reason"])
        for changes in ({"kind": "other"}, {"task": "other"}, {"review": "other"}, {"schema_version": True},
                        {"required": []}, {"worktree_git_dir": str(self.admin)}, {"cwd": str(self.destination)},
                        {"approvals": {**delivered["approvals"], "qa": delivered["approvals"]["release"]}},
                        {"required": ["unknown"], "approvals": {"unknown": delivered["approvals"]["qa"],
                                                               "release": delivered["approvals"]["release"]}}):
            with self.subTest(changes=changes):
                factory.save(path, {**delivered, **changes})
                self.assertEqual(self.inspect()["decision"], "retained")
        factory.save(path, delivered)

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
                self.assertIn("distinct original native contexts", self.inspect()["reason"])
                self.assertTrue(list(self.cache.glob("*/*-a")))

    def test_current_reader_cannot_select_an_older_writer_commit(self):
        self.git("-C", str(self.destination), "commit", "--allow-empty", "-qm", "Next input")
        self.record = factory.set_inputs(self.path, candidate=self.git("-C", str(self.destination), "rev-parse", "HEAD"),
                                         requirements="requirements-v1")
        self.reader_fixture("qa", self.destination)
        self.assertTrue(factory.result_current(self.record, self.record["assignments"]["qa"]["result"]))
        before = self.protected()
        self.assertIn("verified commit produced the selected", self.inspect()["reason"])
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

    def test_release_retains_nested_legacy_reader(self):
        self.reader_fixture("qa", self.destination / "nested", legacy=True)
        assignment = self.record["assignments"]["qa"]
        self.assertTrue(assignment["native_collected"])
        self.assertNotIn("launch_checkout", assignment)
        owner_bytes = self.path.read_bytes()
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("descendants", error)
        self.assertEqual(self.path.read_bytes(), owner_bytes)

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
        self.assertIn("original reader worktree ownership", self.inspect()["reason"])
        assignment["reader_checkout"] = go_cache.checkout_binding(reader, factory.git_dir(reader),
            self.record["admission"]["repository_identity"])
        del assignment["launch_checkout"]
        factory.save(self.path, self.record)
        collected = collect(self.path, "qa", None, self.record, assignment)["assignments"]["qa"]
        self.assertNotIn("launch_checkout", collected)
        self.assertEqual(collected["result"], assignment["result"])
        self.assertIn("original reader worktree ownership", self.inspect()["reason"])

    def test_replaced_writer_pointer_is_never_adopted(self):
        pointer = self.destination / ".git"
        saved = self.root / "original-pointer"
        pointer.rename(saved)
        pointer.write_bytes(saved.read_bytes())
        self.assertNotEqual(pointer.stat().st_ino, saved.stat().st_ino)
        entries = set(self.cache.rglob("*"))
        self.assertIn("original writer checkout", self.inspect()["reason"])
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("original provisioning identity", error)
        self.assertTrue(pointer.exists())
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_separate_reader_requires_its_reciprocal_link(self):
        reader = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(reader), self.record["inputs"]["candidate"])
        self.delivery_fixture(reader)
        self.assertEqual(self.inspect()["decision"], "eligible")
        backlink = factory.git_dir(reader) / "gitdir"
        original = backlink.read_text()
        backlink.write_text(str(self.destination / ".git") + "\n")
        self.assertIn("worktree", self.inspect()["reason"])
        backlink.write_text(original)
        self.assertEqual(self.inspect()["decision"], "eligible")

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
        self.assertIn("original reader worktree ownership", self.inspect()["reason"])
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
        self.assertIn("original reader worktree ownership", self.inspect()["reason"])
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
            row = self.inspect()
        self.assertEqual(row["decision"], "eligible", row)

    def test_release_retains_failed_native_with_detached_helper(self):
        self.assignment.update(result=None, native_collected=False)
        self.assignment["closed"].update(state="failed", exit={"exit_code": 1})
        self.assignment["request"]["command"][0] = str(self.root / "previous-python")
        self.attest(self.assignment)
        self.assignment["native_launch"]["runtime"] = "a" * 64
        # The recorded native exits after starting a detached helper with its cache.
        helper = ("import os, socket; s = socket.socket(socket.AF_UNIX); "
                  "s.settimeout(20); s.connect(os.environ['FIXTURE_GATE']); "
                  "s.sendall(os.fsencode(os.environ['GOCACHE'])); s.recv(1); s.sendall(b'alive')")
        launch = ("import subprocess, sys; sys.stdin.read(1); "
                  "subprocess.Popen([sys.executable, '-c', sys.argv[1]], start_new_session=True); sys.exit(1)")
        with socket.socket(socket.AF_UNIX) as listener:
            with chdir(self.root):
                listener.bind("helper.sock")
            listener.listen(1)
            listener.settimeout(20)
            with subprocess.Popen([sys.executable, "-c", launch, helper], cwd=self.destination, stdin=subprocess.PIPE,
                                  env={**os.environ, "GOCACHE": str(self.cache),
                                       "FIXTURE_GATE": "../helper.sock"}) as parent:
                process = dict(scope=factory.process_scope(), pid=parent.pid, start_time=factory.native_start_time(parent.pid))
                parent.communicate(b"x", timeout=10)
                self.assertEqual(parent.returncode, 1)
            child, _ = listener.accept()
            with child:
                child.settimeout(20)
                self.assertEqual(child.recv(4096), os.fsencode(self.cache))
                self.native.update(schema_version=8, native_process=process, state="failed", exit_code=1, report=None)
                factory.save(self.receipt, self.native)
                factory.save(self.path, self.record)
                code, _, error = self.cli("release-worktree", "--task", "fixture")
                self.assertEqual(code, 1)
                self.assertIn("descendants", error)
                self.assertTrue(self.cache.is_dir())
                self.assertTrue(self.destination.is_dir())
                child.sendall(b"x")
                self.assertEqual(child.recv(5), b"alive")

    def test_release_confirms_git_removal_and_retains_the_dependent_cache(self):
        cwd = self.root / "qa"
        owner = self.state / "qa.json"
        factory.admit(owner, task="qa", repository=str(self.repo), goal="Read the fixture",
                      acceptance=["requirements.md"], corrections=0, investigations=0)
        factory.set_inputs(owner, **self.record["inputs"])
        with factory.execution_lock(self.state), factory.task_lock(self.state, "qa"):
            factory.provision_worktree(owner, worktree_id="reader", destination=str(cwd))
        linked = factory.git_dir(cwd)
        self.delivery_fixture(cwd)
        self.assertEqual(self.inspect()["decision"], "eligible")
        entries = set(self.cache.rglob("*"))
        code, _, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 1)
        self.assertIn("descendants", error)
        self.assertTrue(linked.is_dir())
        self.assertEqual(set(self.cache.rglob("*")), entries)

        # Existing releases from older runtimes still cannot authorize native aging.
        item = factory.load_task(owner)["provisioning"]
        witness = self.record["assignments"]["qa"]["launch_checkout"]
        self.git("worktree", "remove", "--force", "--force", str(cwd))
        receipt = owner.with_suffix(".worktree-release.json")
        factory.save(receipt, dict(schema_version=1, at=self.at + 1, checkout=witness, head=witness["head"],
                                  task="qa", worktree=str(cwd), git_dir=str(linked), owner_token=item["owner_token"],
                                  branch=item["request"]["branch"], already_released=False))
        code, output, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["already_released"])
        self.assertIn("direct-user exclusion", self.trim()["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_used_legacy_reader_retains_dependent_cache(self):
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
        code, _, error = self.cli("release-worktree", "--task", "qa")
        self.assertEqual(code, 1)
        self.assertIn("descendants", error)
        self.assertTrue(reader.is_dir())
        self.assertIn("Missing original provisioner ownership", self.inspect()["reason"])
        self.assertEqual({p: p.read_bytes() for p in self.cache.rglob("*") if p.is_file()}, entries)
        self.assertEqual({p: p.read_bytes() for p in original}, original)

    def test_unrecorded_sibling_teardown_preserves_all_entries(self):
        cwd = self.root / "qa"
        self.git("worktree", "add", "--quiet", "--detach", str(cwd), self.record["inputs"]["candidate"])
        path, delivered = self.delivery_fixture(cwd)
        self.git("worktree", "remove", str(cwd))
        before = self.protected()
        entries = set(self.cache.rglob("*"))
        self.assertIn("original owner release", self.inspect()["reason"])
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
            if replay.call_count == 2:
                backlink.write_text(str(other / ".git") + "\n")
            return result

        with patch.object(release_evidence, "delivered_receipt", side_effect=redirect_after_verification) as replay:
            row = self.trim()
        self.assertEqual(backlink.read_text().strip(), str(other / ".git"))
        self.assertEqual(row["decision"], "retained", row)
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_writer_backlink_changed_during_final_delivery_check_retains_cache(self):
        self.assert_late_backlink_change_retains_cache("writer")

    def test_reader_backlink_changed_during_final_delivery_check_retains_cache(self):
        self.assert_late_backlink_change_retains_cache("reader")

    def test_busy_locks_and_other_line_reuse(self):
        for lock in (factory.execution_lock(self.state), factory.task_lock(self.state, "fixture"),
                     go_cache.inode_lock(self.admin, shared=True)):
            with lock:
                self.assertIn("Busy lock", self.inspect()["reason"])
        other = factory.new_task("other", "other-line")
        other["assignments"]["pending"] = {"request": self.assignment["request"]}
        factory.save(self.state / "other.json", other)
        self.assertIn("reuse by other/pending", self.inspect()["reason"])
        self.assertTrue(list(self.cache.glob("*/*-a")))

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
            self.assertIn("reuse by other/pending", self.inspect()["reason"])

    def test_receipt_requires_original_harness_and_frozen_request(self):
        extra = self.state / "fixture.writer.claude.json"
        factory.save(extra, {**self.native, "kind": "claude-execution", "state": "starting"})
        self.assertIn(extra.name, self.inspect()["reason"])
        extra.unlink()
        alias = self.root / "receipt-admin"
        alias.symlink_to(self.admin, target_is_directory=True)
        other = factory.new_task("other", "other-line")
        other["assignments"]["writer"] = {"request": {"cwd": str(self.repo), "command": [sys.executable]}}
        factory.save(self.state / "other.json", other)
        foreign = self.state / "other.writer.codex.json"
        factory.save(foreign, {**self.native, "job": "other/writer", "cwd": str(self.repo),
                               "worktree_git_dir": str(alias)})
        self.assertIn(foreign.name, self.inspect()["reason"])
        unknown = foreign.with_name("other.writer.unrecognized.json")
        foreign.rename(unknown)
        self.assertIn(unknown.name, self.inspect()["reason"])
        unknown.unlink()
        for key, value in (("requested_model", "other"), ("writer", False), ("events", "/elsewhere")):
            with self.subTest(key=key):
                factory.save(self.receipt, {**self.native, key: value})
                self.assertIn("inconsistent terminal", self.inspect()["reason"])
        factory.save(self.receipt, self.native)
        self.assertEqual(self.inspect()["decision"], "eligible")

    def test_lost_owner_retains_checkout(self):
        self.assignment["closed"].update(state="lost", exit=None, reason="Rex restarted")
        self.native.update(state="starting")
        factory.save(self.receipt, self.native)
        factory.save(self.path, self.record)
        before = self.protected()
        with factory.execution_lock(self.state) as line_fd, factory.task_lock(self.state, "fixture"), \
                self.assertRaisesRegex(factory.FactoryError, "descendants"):
            factory.release_worktree(self.path, line_fd)
        self.assertEqual(self.protected(), before)
        self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_reset_patch_does_not_prove_a_starting_worker_stopped(self):
        self.assignment["closed"].update(state="failed", exit={"exit_code": 1})
        self.assignment.update(result=None, native_collected=False)
        self.native.update(state="starting", exit_code=None, candidate=None)
        factory.save(self.receipt, self.native)
        factory.save(self.path, self.record)
        patch_path = factory.reset_patch(self.receipt)
        factory.save(patch_path, "original private patch")
        with factory.execution_lock(self.state) as line_fd, factory.task_lock(self.state, "fixture"), \
                self.assertRaisesRegex(factory.FactoryError, "descendants"):
            factory.release_worktree(self.path, line_fd)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(patch_path.read_text(), "original private patch")

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
                    case.assertIn("state store", case.inspect()["reason"])
                return super().call(*args, **kwargs)

        with patch.object(factory, "Rex", ObservedRex), patch.object(FakeRex, "sessions", []), \
                patch.object(FakeRex, "calls", []):
            _, attempt = factory.launch(other / "another.json", task="another", assignment_id="reader",
                role="qa", goal="Inspect", cwd=str(self.destination), command=[sys.executable, "{instructions}"],
                binary=sys.executable, server="unix:///fixture")
        self.assertEqual(attempt["state"], "launched")
        self.assertIn("state store", self.inspect()["reason"])

    def test_mount_checked_before_size_and_again_on_open_shard(self):
        with patch.object(go_cache, "check_mounts", side_effect=factory.FactoryError("fixture mount")), \
                patch.object(go_cache, "cache_contents") as walk:
            self.assertIn("fixture mount", self.inspect()["reason"])
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
            mounted = not kwargs.get("size_only", False)
            return clean(fd, mount, **kwargs)

        with patch.object(go_cache, "cache_contents", side_effect=during_walk), \
                patch.object(go_cache, "mount_id", side_effect=identify_open), patch.object(os, "listdir", list_on_mount):
            self.assertIn("Mount or identity changed", self.inspect()["reason"])
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
            row = self.inspect()
        self.assertGreater(row["size_kib"], 0)
        self.assertIn("Mount or identity changed at cache entry", row["reason"])
        self.assertEqual(len(checks), 1)
        self.assertEqual(artifact.read_bytes(), original)

    def test_lost_owner_changed_root_and_unexpected_contents(self):
        (self.cache / "unknown").symlink_to(self.destination / "tool")
        self.assertIn("unexpected cache entry", self.inspect()["reason"])
        (self.cache / "unknown").unlink()
        self.cache.chmod(0o755)
        self.assertIn("0700", self.inspect()["reason"])
        self.cache.chmod(0o700)
        saved = self.cache.with_name("saved")
        self.cache.rename(saved)
        self.cache.symlink_to(saved, target_is_directory=True)
        self.assertIn("changed path", self.inspect()["reason"])
        self.cache.unlink()
        saved.rename(self.cache)
        self.path.unlink()
        self.assertIn("one provisioned task", self.inspect()["reason"])

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

    def test_schema_eight_retains_for_missing_exit_without_migrating(self):
        self.record["schema_version"] = 8
        self.assignment.pop("native_launch", None)
        self.assignment.pop("interpreter", None)
        self.record.pop("acceptance", None)
        del self.assignment["closed"], self.assignment["native_collected"]
        factory.save(self.path, self.record)
        before = self.protected()
        self.assertIn("observed Rex process exit", self.inspect()["reason"])
        self.assertEqual(self.protected(), before)

    def test_surviving_writer_with_release_receipt_retains_every_entry(self):
        factory.save(self.path.with_suffix(".worktree-release.json"), "conflicting release")
        entries = set(self.cache.rglob("*"))
        self.assertIn("surviving writer checkout", self.inspect()["reason"])
        self.assertEqual(set(self.cache.rglob("*")), entries)

    def test_changed_owner_marker_retains(self):
        (self.admin / "locked").write_text("replacement")
        self.assertIn("ownership differs", self.inspect()["reason"])
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
        self.assertIn(path.name, self.inspect()["reason"])

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
                    code, _, error = self.cli("release-worktree", "--task", "fixture")
                    self.assertEqual(code, 1)
                    self.assertIn("native process is active", error)
                    self.assertTrue(self.cache.is_dir())
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
            self.assertIn("cannot be verified", self.inspect()["reason"])
            gone.assert_called_once_with(None, "original-boot", 0)
        self.native["schema_version"] = 999
        factory.save(self.receipt, self.native)
        self.assertIn("receipt version", self.inspect()["reason"])

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


class UnusedCheckout(test_provisioning.ProvisionedRepository):
    def setUp(self):
        super().setUp()
        self.provision()
        self.admin = factory.git_dir(self.destination)
        self.cache = Path(factory.worker_environment({}, self.destination)["GOCACHE"])
        self.cache.mkdir(mode=0o700, exist_ok=True)
        source = self.root / "library.go"
        source.write_text("package library\n")
        subprocess.run(["go", "build", source], cwd=self.root, check=True, env={
            "HOME": str(self.root), "PATH": os.environ["PATH"], "TMPDIR": str(self.root),
            "GOCACHE": str(self.cache), "GOTOOLCHAIN": "local", "GOENV": "off",
            "GOTELEMETRY": "off", "GO111MODULE": "off", "GOPROXY": "off"})

    def test_request_from_another_cwd_names_the_checkout(self):
        other = factory.new_task("other", "other-line")
        other["assignments"]["reader"] = {"request": {"cwd": str(self.repo),
            "command": ["/bin/cat", str(self.destination / "source.bin")]}}
        factory.save(self.state / "other.json", other)
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("other/reader", error)
        self.assertTrue(self.cache.is_dir())
        self.assertTrue(self.destination.is_dir())

    def test_unknown_admin_data_survives_with_and_without_cache(self):
        unknown = self.admin / "independent-cache"
        unknown.mkdir()
        payload = unknown / "user-data"
        payload.write_bytes(b"preserve")
        for cache_present in (True, False):
            if not cache_present:
                shutil.rmtree(self.cache)
            code, _, error = self.cli("release-worktree", "--task", "fixture")
            self.assertEqual(code, 1)
            self.assertIn("unknown Git admin data", error)
            self.assertEqual(payload.read_bytes(), b"preserve")
            self.assertTrue(self.destination.exists())
            self.assertFalse(self.path.with_suffix(".worktree-release.json").exists())

    def test_unattended_owner_release_removes_only_its_linked_build_cache(self):
        unrelated = self.root / "go-build"
        unrelated.mkdir()
        saved = unrelated / "entry"
        saved.write_bytes(b"another build")
        self.assertTrue(list(self.cache.glob("*/*-a")))
        with patch.object(sys, "stdin", StringIO()):
            code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 0, error)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.cache.exists())
        self.assertFalse(self.admin.exists())
        self.assertEqual(saved.read_bytes(), b"another build")

    def test_git_child_excludes_rebase_after_owner_is_killed(self):
        head = self.git("-C", str(self.destination), "rev-parse", "HEAD")
        inputs = self.path.read_bytes()
        self.git("commit", "--allow-empty", "-qm", "New base")
        onto = self.git("rev-parse", "HEAD")
        wrapper = self.root / "gated-git"
        real_git = factory.git_binary()
        wrapper.write_text(f"""#!{sys.executable}
import os, socket, sys
if sys.argv[-5:-3] == ["worktree", "remove"]:
    gate = socket.socket(socket.AF_UNIX)
    os.chdir({str(self.root)!r})
    gate.connect("remove.sock")
    gate.settimeout(30)
    if gate.recv(1) != b"g":
        sys.exit(1)
    gate.set_inheritable(True)
os.execv({real_git!r}, [{real_git!r}, *sys.argv[1:]])
""")
        wrapper.chmod(0o700)
        with socket.socket(socket.AF_UNIX) as listener:
            with chdir(self.root):
                listener.bind("remove.sock")
            listener.listen(1)
            listener.settimeout(30)
            launch = ("import factory, sys; factory.git_binary = lambda: sys.argv[1]; "
                      "sys.exit(factory.main(sys.argv[2:]))")
            with subprocess.Popen([sys.executable, "-c", launch, str(wrapper), "--state-dir", str(self.state),
                                   "release-worktree", "--task", "fixture"], cwd=factory.ROOT) as parent:
                try:
                    child, _ = listener.accept()
                    with child:
                        parent.kill()
                        self.assertEqual(parent.wait(timeout=10), -signal.SIGKILL)
                        for path in (self.admin, self.cache):
                            with self.assertRaises(factory.ExecutionBusy), go_cache.inode_lock(path):
                                pass
                        code, _, error = self.cli("rebase-worktree", "--task", "fixture", "--onto", onto)
                        self.assertEqual(code, factory.EXIT_BUSY)
                        self.assertIn("owns execution", error)
                        self.assertEqual(self.git("-C", str(self.destination), "rev-parse", "HEAD"), head)
                        self.assertEqual(self.path.read_bytes(), inputs)
                        child.sendall(b"g")
                        child.settimeout(30)
                        self.assertEqual(child.recv(1), b"")
                finally:
                    if parent.poll() is None:
                        parent.kill()
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.admin.exists())

    def test_unknown_linked_cache_data_preserves_the_checkout(self):
        unknown = self.cache / "unfinished-work"
        unknown.write_bytes(b"preserve this")
        code, _, error = self.cli("release-worktree", "--task", "fixture")
        self.assertEqual(code, 1)
        self.assertIn("unexpected cache entry", error)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(unknown.read_bytes(), b"preserve this")


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
