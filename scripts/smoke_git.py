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
git,keygen,credential,key,port,agent,public,sibling,config=sys.argv[1:]
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
facts['signer_environment_absent']='SSH_AUTH_SOCK' not in os.environ
facts['inherited_git_auth_absent']=not any('fixture-auth-sentinel' in value for value in os.environ.values())
p=subprocess.run([git,'config','--get-regexp','^http.*extraheader$'],capture_output=True,text=True)
facts['git_auth_settings_absent']='fixture-auth-sentinel' not in p.stdout
try:
 pathlib.Path(config).read_text()
 facts['original_config_read_blocked']=False
except (PermissionError,FileNotFoundError):
 facts['original_config_read_blocked']=True
probe=pathlib.Path(os.environ['TMPDIR'])/'probe.pub'
probe.write_text(public)
signed=subprocess.run([keygen,'-Y','sign','-n','fixture-probe','-f',str(probe)],input=b'not a git commit',capture_output=True,env={**os.environ,'SSH_AUTH_SOCK':agent})
facts['arbitrary_agent_signing_blocked']=signed.returncode!=0
try:
 pathlib.Path(sibling).write_bytes(b'unrelated index corruption')
 facts['sibling_index_write_blocked']=False
except (PermissionError,FileNotFoundError):
 facts['sibling_index_write_blocked']=True
print(json.dumps(facts))
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
        git(repo, "config", "http.extraHeader", "Authorization: Bearer fixture-auth-sentinel-local")
        os.environ.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraHeader",
                          GIT_CONFIG_VALUE_0="Authorization: Bearer fixture-auth-sentinel-inherited")
        credential = root / "credential-fixture"
        credential.write_text("not a real credential\n")
        sibling = root / "sibling"
        git(repo, "worktree", "add", "--quiet", "--detach", str(sibling), "HEAD")
        sibling_index = Path(git(sibling, "rev-parse", "--absolute-git-dir").stdout.strip()) / "index"
        sibling_before = sibling_index.read_bytes()
        program = repo / ".git/hooks/probe.py"
        program.write_text(HOOK)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            previous = Path.cwd()
            try:
                os.chdir(repo)
                identity = factory.repository_identity(".", create=True)
                parent = factory.git_commit(".", "HEAD")
                args = SimpleNamespace(codex=binary, worktree_git_dir=factory.git_dir("."),
                    candidate=parent, commit_message="Signed fixture", receipt=root / "receipt.json")
                receipt = dict(repository_identity=identity, commit=dict(state="preparing", parent=parent,
                    message=args.commit_message, staged_tree=None, candidate=None))
                factory.save(args.receipt, receipt)
                control_public = root / "control.pub"
                control_public.write_bytes(key.with_suffix(".pub").read_bytes())
                with codex_worker.private_signer(key, str(Path(sys.executable).resolve())) as attack_agent:
                    control = subprocess.run([keygen, "-Y", "sign", "-n", "fixture-probe", "-f", str(control_public)],
                        input=b"not a git commit", capture_output=True, timeout=10,
                        env={**os.environ, "SSH_AUTH_SOCK": attack_agent})
                    if control.returncode:
                        raise factory.FactoryError("Fixture agent control could not sign; repair SSH tools before checking denial")
                    hook = repo / ".git/hooks/pre-commit"
                    hook.write_text("#!/bin/sh\n" + shlex.join([str(Path(sys.executable).resolve()), "-B",
                        str(program), factory.git_binary(), keygen, str(credential), str(key),
                        str(listener.getsockname()[1]), attack_agent, control_public.read_text(), str(sibling_index), str(repo / ".git/config")]) + "\n")
                    hook.chmod(0o700)
                    (repo / "source.txt").write_text("changed fixture source\n")
                    with codex_worker.isolated_git(args, identity):
                        hooked = codex_worker.commit_changes(args, receipt)
                if hooked is None:
                    raise factory.FactoryError("Fixture pre-commit produced no evidence; inspect native sandbox execution")
                lines = (hooked.stdout + "\n" + hooked.stderr).splitlines()
                evidence = [line for line in lines if line.startswith("{") and line.endswith("}")]
                if len(evidence) != 1:
                    raise factory.FactoryError("Fixture hook produced no unique evidence; inspect native sandbox execution")
                facts = json.loads(evidence[0])
                expected = {"external_push_blocked", "credential_read_blocked", "private_key_read_blocked",
                    "network_blocked", "publication_token_absent", "signer_environment_absent",
                    "arbitrary_agent_signing_blocked", "sibling_index_write_blocked", "inherited_git_auth_absent",
                    "git_auth_settings_absent", "original_config_read_blocked"}
                if (not isinstance(facts, dict) or set(facts) != expected
                        or any(type(value) is not bool for value in facts.values())):
                    raise factory.FactoryError("Fixture hook evidence is incomplete; inspect the selected sandbox")
                candidate = factory.git_commit(".", "HEAD")
                signed = "gpgsig -----BEGIN SSH SIGNATURE-----" in git(repo, "cat-file", "commit", candidate).stdout
                external = root / "external-hooks"
                external.mkdir()
                effect = root / "external-hook-effect"
                outside_hook = external / "pre-commit"
                outside_hook.write_text("#!/bin/sh\nprintf called > " + shlex.quote(str(effect)) + "\n")
                outside_hook.chmod(0o700)
                git(repo, "config", "core.hooksPath", str(external))
                outside_args = SimpleNamespace(**{**vars(args), "candidate": candidate,
                    "receipt": root / "external-hook-receipt.json"})
                outside_receipt = dict(repository_identity=identity, commit=dict(state="preparing", parent=candidate,
                    message=args.commit_message, staged_tree=None, candidate=None))
                factory.save(outside_args.receipt, outside_receipt)
                index_before = (args.worktree_git_dir / "index").read_bytes()
                try:
                    with codex_worker.isolated_git(outside_args, identity):
                        codex_worker.commit_changes(outside_args, outside_receipt)
                    raise factory.FactoryError("External hook fixture unexpectedly proceeded")
                except factory.FactoryError as exc:
                    if "external hook paths" not in str(exc):
                        raise
                finally:
                    git(repo, "config", "core.hooksPath", ".git/hooks")
                facts["external_precommit_refused_before_staging"] = (not effect.exists()
                    and outside_receipt["commit"]["state"] == "preparing"
                    and (args.worktree_git_dir / "index").read_bytes() == index_before
                    and factory.git_commit(".", "HEAD") == candidate)
                factory.check_checkout(str(repo), str(repo), candidate, expected_identity=identity,
                                       expected_git_dir=args.worktree_git_dir)
                facts.update(local_signed_commit_created=candidate != parent and signed,
                             external_remote_unchanged=git(remote, "show-ref", allowed=(0, 1)).returncode == 1,
                             source_clean=True, sibling_index_unchanged=sibling_index.read_bytes() == sibling_before,
                             fixture_agent_control_signed=True, user_agent_unchanged=os.environ.get("SSH_AUTH_SOCK") == agent)
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
