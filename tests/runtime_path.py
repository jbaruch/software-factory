"""Put the runtime packaged in the factory skill on sys.path; tests import its modules by name."""
import os
import sys
import tempfile
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "skills" / "factory" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

# Never the operator's factory configuration, which holds a GitHub token; worker subprocesses inherit this.
os.environ["SOFTWARE_FACTORY_CONFIG"] = tempfile.mkdtemp(prefix="factory-test-config-")
