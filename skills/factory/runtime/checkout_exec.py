"""Check Rex's actual working directory before executing an admitted command."""
import argparse
import importlib
import json
import os
from pathlib import Path
import runpy
import sys

sys.dont_write_bytecode = True

if __name__ == "__main__":
    from runtime_entry import locked_main
    sys.exit(locked_main("checkout_exec"))

import factory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-digest")
    parser.add_argument("--identity", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--git-dir", required=True)
    parser.add_argument("--launch-checkout", type=factory.decode_json)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    harness = None
    try:
        if args.runtime_digest:
            harness = factory.native_harness(command, interpreter=command[0]) if command else None
            if (not command or Path(command[0]).resolve(strict=True) != Path(sys.executable).resolve(strict=True)
                    or not sys.flags.ignore_environment or not sys.flags.no_user_site
                    or harness is None or factory.native_runtime_digest() != args.runtime_digest):
                raise factory.FactoryError("Installed runtime changed after admission; inspect the original attempt before retrying")
        if not command or not os.path.isabs(command[0]):
            raise factory.FactoryError("Supply an absolute executable command after --")
        try:
            identity = json.loads(args.identity, object_pairs_hook=factory.unique_object)
        except ValueError:
            raise factory.FactoryError("Cannot decode repository pin; inspect the original assignment before another attempt") from None
        if not factory.valid_repository_identity(identity):
            raise factory.FactoryError("Invalid repository pin; inspect the original assignment before another attempt")
        # '.' uses the working directory Rex actually opened, not the saved pathname.
        if not os.path.isabs(args.git_dir):
            raise factory.FactoryError("Supply the saved absolute worktree Git directory; inspect the original assignment")
        def verify_reader():
            if args.launch_checkout is None:
                return
            import go_cache
            root = Path(factory.git_worktree("."))
            if go_cache.checkout_binding(root, Path(args.git_dir), identity) != args.launch_checkout:
                raise factory.FactoryError("Linked reader checkout changed before native launch; inspect the original assignment")

        verify_reader()
        harness = harness or factory.native_harness(command)
        pending = {} if harness and "--write" in command else None
        factory.check_checkout(".", ".", args.candidate, expected_identity=identity,
                               expected_git_dir=args.git_dir, pending_links=pending)
        environment = factory.command_environment()
        environment.pop(factory.WRITER_SOURCE_ENV, None)
        if pending is not None:
            if pending:
                parser = importlib.import_module(f"{harness}_worker").parser()
                worker = factory.parse_saved_args(parser, command[4:] if command[1:3] == ["-E", "-s"] else command[2:])
                factory.remember_writer_links(worker, pending)
            environment[factory.WRITER_SOURCE_ENV] = json.dumps(dict(root=factory.git_worktree("."),
                                                                    displaced=sorted(set(pending.values()))))
        verify_reader()
        if args.runtime_digest and harness is not None:
            os.environ.clear()
            os.environ.update(environment)
            factory.ENVIRONMENT_TOKEN = ""
            offset = 3 if command[1:3] == ["-E", "-s"] else 1
            sys.argv = command[offset:]
            # Execute the allowlisted script as source, just as a fresh script launch would.
            runpy.run_path(sys.argv[0], run_name="__main__")
            return 0
        os.execve(command[0], command, environment)
    except factory.FactoryError as exc:
        print("checkout-exec: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("checkout-exec: Cannot execute the saved command; restore its executable/access and inspect the existing attempt", file=sys.stderr)
        return 1
