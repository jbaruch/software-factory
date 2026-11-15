"""The per-user runtime that ACR-installed project copies run from."""
import runtime_path  # noqa: F401  Must precede runtime imports.
from contextlib import redirect_stderr
import io
import os
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

    def test_the_installed_runtime_still_reads_the_linux_token(self):
        # Review of #152: loading factory.py takes the token out of the environment, and an ACR-realized copy then
        # re-executes the installed runtime, which must still read it while no child of its own inherits it.
        runtime = self.copy("0.19.0")
        for path in factory.ROOT.glob("*.py"):
            (runtime / path.name).write_bytes(path.read_bytes())
        (runtime / "VERSION").write_text((factory.ROOT / "VERSION").read_text())
        (runtime / "probe.py").write_text(
            "import os, subprocess, sys\n"
            "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n"
            "import factory\n"
            "factory.relocate('probe.py', sys.argv[1:])\n"
            "child = subprocess.run([sys.executable, '-c', 'import os; print(os.environ.get(\"SOFTWARE_FACTORY_GITHUB_READ_TOKEN\"))'],"
            " capture_output=True, text=True, check=True).stdout.strip()\n"
            "print(__file__, factory.ENVIRONMENT_TOKEN, child)\n")
        environment = {**os.environ, "XDG_DATA_HOME": str(self.root / "data"), factory.TOKEN_VARIABLE: "github_pat_linux"}
        result = subprocess.run([sys.executable, "-B", str(runtime / "probe.py")], env=environment,
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.split(), [str(self.root / "data" / "software-factory" / "skill" / "runtime" / "probe.py"),
                                                 "github_pat_linux", "None"])

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
