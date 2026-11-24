"""Load a controller or worker from one installed tree through its final write."""
import fcntl
import importlib
import os
from pathlib import Path
import sys


def locked_main(module):
    version = Path(__file__).resolve().with_name("VERSION")
    try:
        with version.open("rb") as active:
            fcntl.flock(active, fcntl.LOCK_SH)
            if not os.path.samestat(os.fstat(active.fileno()), version.stat()):
                print("Factory changed during startup. Restart with the installed runtime.", file=sys.stderr)
                return 1
            return importlib.import_module(module).main()
    except OSError as exc:
        print(f"Cannot run {module} from the installed Factory tree. Check runtime access and retry. {exc}", file=sys.stderr)
        return 1
