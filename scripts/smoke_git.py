"""Explicit local Git/sandbox conformance; no model turn, Rex or user repository effects."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from types import SimpleNamespace

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import codex_worker
import factory


HOOK = """import json,os,pathlib,socket,subprocess,sys
git,credential,key,port,marker=sys.argv[1:]
facts={'external_push_blocked':subprocess.run([git,'push','origin','HEAD'],capture_output=True).returncode!=0}
for name,path in [('credential_read_blocked',credential),('private_key_read_blocked',key)]:
 try:
  pathlib.Path(path).read_bytes()
  facts[name]=False
 except (PermissionError,FileNotFoundError):
  facts[name]=True
try:
 socket.create_connection(('127.0.0.1',int(port)),timeout=2).close()
 facts['network_blocked']=False
except OSError:
 facts['network_blocked']=True
facts['publication_token_absent']='GH_TOKEN' not in os.environ
pathlib.Path(marker).write_text(json.dumps(facts))
"""


def conformance(binary) -> dict:
    # These overrides affect only this disposable fixture process.
    for name in list(os.environ):
        if name.startswith("GIT_CONFIG"):
            del os.environ[name]
    os.environ.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                      GH_TOKEN="fixture-not-a-real-token", PYTHONDONTWRITEBYTECODE="1")
    agent = os.environ.get("SSH_AUTH_SOCK")
    keygen = shutil.which("ssh-keygen", path=os.defpath)
    if keygen is None:
        raise factory.FactoryError("Install the operating system's SSH tools before running this check")
    with tempfile.TemporaryDirectory(prefix="factory-git-smoke-") as temporary:
        root = Path(temporary).resolve()
        repo, remote = root / "repo", root / "remote.git"
        repo.mkdir()
        def git(directory, *args, allowed=(0,)):
            result = subprocess.run([factory.git_binary(), "-C", str(directory), *args],
                capture_output=True, text=True, timeout=10,
                env={**factory.git_environment(), "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull})
            if result.returncode not in allowed:
                raise factory.FactoryError("Fixture Git failed; inspect the selected Git/sandbox installation")
            return result
        git(repo, "init", "--quiet", "--initial-branch=main")
        git(root, "init", "--bare", "--quiet", str(remote))
        key = root / "fixture-key"
        subprocess.run([keygen, "-q", "-t", "ed25519", "-N", "", "-f", str(key)],
                       check=True, capture_output=True, timeout=10)
        for name, value in {"user.name": "Fixture", "user.email": "fixture@example.invalid",
                "core.hooksPath": ".git/hooks", "core.fsmonitor": "false", "gpg.format": "ssh",
                "user.signingkey": str(key) + ".pub", "commit.gpgsign": "false"}.items():
            git(repo, "config", name, value)
        (repo / "source.txt").write_text("initial fixture source\n")
        git(repo, "add", "--all")
        git(repo, "commit", "--quiet", "-m", "Initial fixture")
        git(repo, "config", "commit.gpgsign", "true")
        git(repo, "remote", "add", "origin", str(remote))
        credential = root / "credential-fixture"
        credential.write_text("not a real credential\n")
        program, marker = repo / ".git/probe.py", repo / ".git/probe.json"
        program.write_text(HOOK)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            hook = repo / ".git/hooks/post-commit"
            hook.write_text("#!/bin/sh\n" + shlex.join([str(Path(sys.executable).resolve()), "-B",
                str(program), factory.git_binary(), str(credential), str(key),
                str(listener.getsockname()[1]), str(marker)]) + "\n")
            hook.chmod(0o700)
            previous = Path.cwd()
            try:
                os.chdir(repo)
                identity = factory.repository_identity(".", create=True)
                parent = factory.git_commit(".", "HEAD")
                args = SimpleNamespace(codex=binary, worktree_git_dir=factory.git_dir("."))
                (repo / "source.txt").write_text("changed fixture source\n")
                with codex_worker.isolated_git(args, identity, signing=True):
                    for command in [("add", "--all"), ("commit", "--quiet", "-m", "Signed fixture")]:
                        if factory.git_run(".", *command).returncode:
                            raise factory.FactoryError("Sandboxed fixture Git failed; inspect native sandbox and SSH signing support")
                    candidate = factory.git_commit(".", "HEAD")
                    signed = "gpgsig -----BEGIN SSH SIGNATURE-----" in factory.git_run(".", "cat-file", "commit", candidate).stdout
                    factory.check_checkout(str(repo), str(repo), candidate, expected_identity=identity,
                                           expected_git_dir=args.worktree_git_dir)
                if not marker.is_file():
                    raise factory.FactoryError("Fixture hook produced no evidence; inspect native sandbox execution")
                facts = json.loads(marker.read_text())
                if (not isinstance(facts, dict) or set(facts) != {"external_push_blocked", "credential_read_blocked",
                        "private_key_read_blocked", "network_blocked", "publication_token_absent"}
                        or any(type(value) is not bool for value in facts.values())):
                    raise factory.FactoryError("Fixture hook evidence is incomplete; inspect the selected sandbox")
                facts.update(local_signed_commit_created=candidate != parent and signed,
                             external_remote_unchanged=git(remote, "show-ref", allowed=(0, 1)).returncode == 1,
                             source_clean=True, user_agent_unchanged=os.environ.get("SSH_AUTH_SOCK") == agent)
                return dict(schema_version=1, scope="local Git/OS sandbox only; no native assignment or model turn", checks=facts)
            finally:
                os.chdir(previous)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if not args.codex.is_absolute() or not args.codex.is_file() or not os.access(args.codex, os.X_OK):
            raise factory.FactoryError("Supply an existing absolute native Codex executable")
        result = conformance(args.codex.resolve())
        print(json.dumps(result, indent=2))
        return 0 if all(result["checks"].values()) else 1
    except (factory.FactoryError, OSError, ValueError, subprocess.SubprocessError) as exc:
        print("smoke-git: " + str(exc) + "; repair the fixture/tool environment before another check", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
