"""Put the runtime packaged in the factory skill on sys.path; tests import its modules by name."""
import sys
from pathlib import Path

RUNTIME = Path(__file__).resolve().parents[1] / "skills" / "factory" / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))
