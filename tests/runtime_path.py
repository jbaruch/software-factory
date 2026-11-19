"""Put the runtime packaged in the factory skill on sys.path; tests import its modules by name."""
import os
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "skills" / "factory" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

# Never the operator's factory configuration, which holds a GitHub token; worker subprocesses inherit this.
# The object lives as long as the test process and removes the directory when it ends.
CONFIG = tempfile.TemporaryDirectory(prefix="factory-test-config-")
os.environ["SOFTWARE_FACTORY_CONFIG"] = CONFIG.name

# Attempts whose exit can't be confirmed keep their scratch on purpose; in tests that stays in this run's
# directory, which is removed when the test process ends, never in the real /tmp. Short, for Unix sockets.
SCRATCH = tempfile.TemporaryDirectory(prefix="sf-test-", dir="/tmp" if os.path.isdir("/tmp") else None)
import factory  # noqa: E402  The runtime path is set above.
factory.SCRATCH_ROOT = SCRATCH.name
