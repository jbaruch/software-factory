"""Assignment briefs: every writer and every architect carries the simplicity mandate."""
import runtime_path  # noqa: F401  Must precede runtime imports.
import unittest

import factory


class Briefs(unittest.TestCase):
    def test_writers_and_architects_carry_the_simplicity_mandate(self):
        # Baruch: the simplicity mandate is critical, not a nice-to-have.
        self.assertIn(factory.SIMPLICITY, factory.brief("backend", "Change it", designs=True))
        self.assertIn(factory.SIMPLICITY, factory.brief("architecture", "Design it"))
        self.assertNotIn(factory.SIMPLICITY, factory.brief("qa", "Check it"))
        self.assertIn("no option, flag, schema version, fallback", factory.SIMPLICITY)


class TaskSchema(unittest.TestCase):
    """Version 11 adds the optional `acceptance` capture (review of #152)."""

    def test_version_10_migrates_and_never_carries_a_capture(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "task.json"
            factory.save(path, {**factory.new_task("task"), "schema_version": 10})
            self.assertEqual(factory.load_task(path)["schema_version"], 11)
            factory.save(path, {**factory.new_task("task"), "schema_version": 10,
                                "acceptance": {"r": "text"}})
            with self.assertRaisesRegex(factory.FactoryError, "Legacy task has newer fields"):
                factory.load_task(path)


if __name__ == "__main__":
    unittest.main()
