"""Check Rex's actual working directory before executing an admitted command."""
import argparse
import json
import os
import sys

import factory


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--identity", required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--git-dir", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    try:
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
        factory.check_checkout(".", ".", args.candidate, expected_identity=identity,
                               expected_git_dir=args.git_dir)
        os.execve(command[0], command, factory.command_environment())
    except factory.FactoryError as exc:
        print("checkout-exec: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("checkout-exec: Cannot execute the saved command; restore its executable/access and inspect the existing attempt", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
