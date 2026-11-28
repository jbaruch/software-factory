"""The per-user runtime that ACR-installed project copies run from."""
import runtime_path  # noqa: F401  Must precede runtime imports.
from contextlib import redirect_stderr
import errno
import fcntl
import io
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import factory


class Relocate(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.home = self.root / "home" / "software-factory"
        self.calls = []

    def copy(self, version, project="project"):
        """An ACR-realized project copy of the factory skill."""
        skill = self.root / project / ".claude" / "skills" / "acr__jbaruch__software-factory__factory"
        (skill / "runtime").mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: factory\n---\n")
        (skill / "runtime" / "VERSION").write_text(version + "\n")
        (skill / "runtime" / "factory.py").write_text(f"# factory {version}\n")
        return skill / "runtime"

    def relocate(self, runtime, argv=("status",)):
        factory.relocate("factory.py", list(argv), root=runtime, home=self.home,
                         execv=lambda path, args: self.calls.append((path, args)))
        return self.calls[-1]

    def installed(self):
        return (self.home / "skill" / "runtime" / "VERSION").read_text().strip()

    def test_source_checkout_runs_in_place(self):
        factory.relocate("factory.py", ["status"], home=self.home,
                         execv=lambda path, args: self.fail("A source checkout never re-executes"))
        self.assertFalse(self.home.exists())

    def test_relocation_discards_an_obsolete_foreman_token_before_exec(self):
        runtime = self.copy("0.19.0")
        for path in factory.ROOT.glob("*.py"):
            (runtime / path.name).write_bytes(path.read_bytes())
        (runtime / "VERSION").write_text((factory.ROOT / "VERSION").read_text())
        (runtime / "probe.py").write_text(
            "import os, subprocess, sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
            "before = os.environ.get('SOFTWARE_FACTORY_GITHUB_READ_TOKEN')\n"
            "import factory\n"
            "factory.relocate('probe.py', sys.argv[1:])\n"
            "child = subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"SOFTWARE_FACTORY_GITHUB_READ_TOKEN\"))'],"
            " capture_output=True, text=True, check=True).stdout.strip()\n"
            "print(__file__, before, child)\n")
        environment = {**os.environ, "XDG_DATA_HOME": str(self.root / "data"), "SOFTWARE_FACTORY_GITHUB_READ_TOKEN": "retired-credential"}
        result = subprocess.run([sys.executable, "-B", str(runtime / "probe.py")], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), [str(self.root / "data" / "software-factory" / "skill" / "runtime" / "probe.py"),
                                                 "None", "None"])

    def test_first_use_installs_the_copy_and_runs_it(self):
        path, args = self.relocate(self.copy("0.19.0"), ["status", "--task", "t"])
        self.assertEqual(path, sys.executable)
        self.assertEqual(args, [sys.executable, "-B", str(self.home / "skill" / "runtime" / "factory.py"),
                                "status", "--task", "t"])
        self.assertEqual(self.installed(), "0.19.0")
        self.assertEqual((self.home / "skill" / "SKILL.md").read_text(), "---\nname: factory\n---\n")
        self.assertEqual(sorted(path.name for path in self.home.iterdir()), [".install.lock", "skill"])

    def test_newer_copy_upgrades_in_place_and_older_never_downgrades(self):
        self.relocate(self.copy("0.19.0", "old"))
        (self.home / "skill" / "runtime" / "only-in-old.txt").write_text("retired\n")
        self.relocate(self.copy("0.20.0", "new"))
        self.assertEqual(self.installed(), "0.20.0")
        self.assertFalse((self.home / "skill" / "runtime" / "only-in-old.txt").exists())
        _, args = self.relocate(self.root / "old" / ".claude" / "skills" / "acr__jbaruch__software-factory__factory" / "runtime")
        self.assertEqual(self.installed(), "0.20.0")
        self.assertEqual(args[2], str(self.home / "skill" / "runtime" / "factory.py"))

    def test_tessl_and_acr_use_the_same_versioned_runtime(self):
        old = self.copy("0.19.0", "old")
        new = self.copy("0.20.0", "new").parent
        tessl = self.root / ".tessl" / "plugins" / "jbaruch" / "software-factory" / "skills" / "factory"
        shutil.copytree(new, tessl)
        _, old_launch = self.relocate(old)
        _, new_launch = self.relocate(tessl / "runtime")
        self.assertEqual(old_launch, new_launch)
        self.assertEqual(self.installed(), "0.20.0")
        self.assertEqual(Path(new_launch[2]).read_bytes(), (tessl / "runtime" / "factory.py").read_bytes())
        self.relocate(old)
        self.assertEqual(self.installed(), "0.20.0")

    def test_fresh_and_released_install_run_the_offered_skill_bytes(self):
        source = factory.ROOT.parent
        offered = self.root / "candidate" / "skills" / "acr__jbaruch__software-factory__factory"
        shutil.copytree(source, offered, ignore=shutil.ignore_patterns("__pycache__"))
        def contents(directory):
            return {p.relative_to(directory): p.read_bytes() for p in directory.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}
        expected = contents(source)
        for previous in (False, True):
            data = self.root / ("upgrade" if previous else "fresh")
            installed = data / "software-factory" / "skill"
            if previous:
                factory.relocate("factory.py", [], root=self.copy("0.39.29"), home=installed.parent,
                                 execv=lambda *args: None)
            with self.subTest(previous=previous):
                subprocess.run([sys.executable, "-B", str(offered / "runtime" / "factory.py"), "--help"],
                               env={**os.environ, "XDG_DATA_HOME": str(data)}, capture_output=True, check=True)
                self.assertEqual(contents(installed), expected)

    def test_exchange_errors_and_process_death_leave_a_complete_active_tree(self):
        old = self.copy("0.39.29", "old")
        new = self.copy("0.39.30", "new")
        (old / "only-old").write_bytes(b"original")
        (new / "only-new").write_bytes(b"replacement")

        def files(skill):
            return {p.relative_to(skill): p.read_bytes() for p in skill.rglob("*") if p.is_file()}

        child = """
import fcntl, os, sys
from pathlib import Path
import factory
stage, home, offer = sys.argv[1:]
exchange = factory.exchange_installation
def interrupt(staged, installed):
    for lock in (installed.parent / '.install.lock', installed / 'runtime/VERSION'):
        with lock.open('rb') as probe:
            try:
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                pass
            else:
                raise AssertionError('exchange without lock: ' + str(lock))
    if stage == 'after':
        exchange(staged, installed)
    os._exit(73)
factory.exchange_installation = interrupt
factory.relocate('factory.py', [], home=Path(home), root=Path(offer), execv=lambda *args: None)
"""
        for stop in ("error", "before", "after"):
            with self.subTest(stop=stop):
                self.home = self.root / stop
                self.relocate(old)
                if stop == "error":
                    with patch.object(factory, "exchange_installation", side_effect=OSError(errno.ENOSPC, "full")), self.assertRaises(factory.FactoryError):
                        self.relocate(new)
                else:
                    result = subprocess.run([sys.executable, "-B", "-c", child, stop, str(self.home), str(new)],
                                            cwd=factory.ROOT, capture_output=True)
                    self.assertEqual(result.returncode, 73, result.stderr)
                expected = files((new if stop == "after" else old).parent)
                self.assertEqual(files(self.home / "skill"), expected)
                self.relocate(self.copy("0.39.28", "older-" + stop))
                self.assertEqual(files(self.home / "skill"), expected)
                self.relocate(new)
                self.assertEqual(files(self.home / "skill"), files(new.parent))

    def test_recover_only_one_locked_retired_tree_matching_original_offer(self):
        old = self.copy("0.39.29", "old")
        self.relocate(old)
        retired = self.home / ".installing-earlier" / "retired"
        retired.parent.mkdir()
        (self.home / "skill").rename(retired)
        with (retired / "runtime" / "VERSION").open() as held:
            fcntl.flock(held, fcntl.LOCK_SH)
            with self.assertRaisesRegex(factory.FactoryError, "in use"):
                self.relocate(old)
        with self.assertRaisesRegex(factory.FactoryError, "original matching package"):
            self.relocate(self.copy("0.39.28", "older"))
        changed = self.copy("0.39.29", "changed")
        (changed / "factory.py").write_text("different bytes at the same version")
        with self.assertRaisesRegex(factory.FactoryError, "original matching package"):
            self.relocate(changed)
        second = self.home / ".installing-another" / "retired"
        shutil.copytree(retired, second)
        with self.assertRaisesRegex(factory.FactoryError, "Several retired"):
            self.relocate(old)
        self.assertFalse((self.home / "skill").exists())
        self.assertEqual((retired / "runtime/factory.py").read_bytes(), (old / "factory.py").read_bytes())
        self.assertTrue(second.exists())
        shutil.rmtree(second.parent)
        marker = retired.parent / "skill"
        marker.mkdir()  # A previous installer's staged offer belongs to its owner.
        self.relocate(old)
        self.assertEqual(self.installed(), "0.39.29")
        self.assertTrue(marker.is_dir())
        self.assertFalse(retired.exists())

    def test_equal_version_leaves_the_installed_runtime_untouched(self):
        self.relocate(self.copy("0.19.0", "first"))
        marker = self.home / "skill" / "runtime" / "kept.txt"
        marker.write_text("unchanged\n")
        self.relocate(self.copy("0.19.0", "second"))
        self.assertEqual(marker.read_text(), "unchanged\n")

    def test_generated_bytecode_is_never_installed(self):
        runtime = self.copy("0.19.0")
        (runtime / "__pycache__").mkdir()
        (runtime / "__pycache__" / "factory.cpython-311.pyc").write_bytes(b"generated")
        self.relocate(runtime)
        self.assertFalse((self.home / "skill" / "runtime" / "__pycache__").exists())

    def test_unreadable_version_refuses_before_installing(self):
        with self.assertRaisesRegex(factory.FactoryError, "Unsupported factory version"):
            self.relocate(self.copy("latest"))
        self.assertFalse((self.home / "skill").exists())

    def test_a_runtime_that_cannot_install_refuses_like_any_command(self):
        # Review of #146: relocate ran outside the CLI's error boundary, so a failed install printed a traceback.
        import ready_runner
        failure = factory.FactoryError("Cannot install the per-user runtime")
        for main, prefix in ((factory.main, "factory: "), (ready_runner.main, "ready-runner: ")):
            errors = io.StringIO()
            with self.subTest(prefix=prefix), patch.object(factory, "relocate", side_effect=failure), \
                    redirect_stderr(errors):
                self.assertEqual(main(["status"]), 1)
                self.assertEqual(errors.getvalue(), prefix + "Cannot install the per-user runtime\n")


if __name__ == "__main__":
    unittest.main()
