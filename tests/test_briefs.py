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


if __name__ == "__main__":
    unittest.main()
