"""Factory-owned writer commits, independent of which agent edited the worktree.

Git, configured hooks and signing run inside a local sandbox profile so they cannot
reach the network or write outside the assignment checkout. The sandbox binary is
the native Codex CLI's `sandbox` command: a local OS sandbox wrapper that makes no
model call and uses no subscription allowance. It is not the writing agent.
"""
from contextlib import contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True

import factory


# What every writer is told, whichever harness carries it.
WRITER_CONTRACT = ("Factory commit mode: edit and test only; do not stage or commit Git changes. "
                   "The factory commits after a completed report and only then: report completed once your change "
                   "is in place, listing residual findings in your artifacts; findings or blocked leave your edits "
                   "uncommitted. Report only when you are done: blocked is for a blocker you hit, and progress notes "
                   "belong in your work, never in the report. Keep temporary outputs outside this checkout. "
                   "Do not use git stash: every worktree of this repository shares one stash, so another writer "
                   "could pop your changes; to see old code, read it with git show HEAD:<path>. "
                   "Before committing, the factory scans the lines you add with Gitleaks default rules and ignores "
                   "gitleaks:allow comments, so a match fails your work: keep test fixtures and examples from "
                   "looking like credentials.")


SANDBOX_HINT = "; inspect native Git sandbox support and offline hook/signing access before another attempt; no host fallback was used"


def git_said(result, limit=2000):
    """Git's exit status and the end of what it or a hook printed, so a failed commit names its cause:
    the receipt of a writer that finished during a laptop's dark wake could not tell a timeout,
    a hook and a signing refusal apart."""
    said = (result.stderr.strip() or result.stdout.strip())[-limit:]
    return f" (git exited {result.returncode}" + (": " + said if said else "") + ")"


class Refusal(factory.FactoryError):
    """A factory decision about the writer's own work, not a Git sandbox failure."""


def tool_environment():
    """Minimal environment for local scanner identity checks."""
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
               "TMPDIR", "TMP", "TEMP", "TERM", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME",
               "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {**{key: value for key, value in os.environ.items() if key in allowed},
            "PYTHONDONTWRITEBYTECODE": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


GIT_ENV_LAUNCHER = """import hashlib,json,os,sys
environment = {key: os.environ[key] for key in json.loads(sys.argv[2]) if key in os.environ}
environment.update(json.loads(sys.argv[1]))
if hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest() != sys.argv[3]:
    raise SystemExit(125)
os.execve(sys.argv[4], sys.argv[4:], environment)
"""



@contextmanager
def private_signer(key, python):
    """Own one short-lived signing agent; never change the user's agent."""
    agent = shutil.which("ssh-agent", path=os.defpath)
    add = shutil.which("ssh-add", path=os.defpath)
    if not agent or not add:
        raise factory.FactoryError("Install OpenSSH agent tools for isolated SSH-signed commits")
    with tempfile.TemporaryDirectory(prefix="sf-sign-") as directory:
        socket = str(Path(directory).resolve() / "s")
        environment = {"PATH": os.defpath, "HOME": str(Path.home()), "SSH_AUTH_SOCK": socket,
                       "SSH_ASKPASS_REQUIRE": "never"}
        # Command mode ties agent lifetime to this pipe, including owner crashes.
        # No PKCS#11/FIDO libraries may be loaded through the callback socket.
        try:
            process = subprocess.Popen([agent, "-a", socket, "-P", "", "-t", "300", python,
                "-I", "-S", "-c", "import sys; sys.stdin.buffer.read()"], cwd=directory,
                env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            raise factory.FactoryError("Cannot start the private SSH signer; restore local OpenSSH and socket access before another attempt") from None
        try:
            deadline = time.monotonic() + 5
            while not Path(socket).exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.01)
            if not Path(socket).exists() or process.poll() is not None:
                raise factory.FactoryError("Private SSH signing agent did not start; restore local OpenSSH/socket access before another attempt")
            try:
                loaded = subprocess.run([add, "-q", "-k", "-t", "300", str(key)], env=environment,
                    stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False, start_new_session=True)
            except (OSError, subprocess.TimeoutExpired):
                raise factory.FactoryError("Private SSH key loading failed or timed out; restore noninteractive local signing access before another attempt") from None
            if loaded.returncode:
                raise factory.FactoryError("Cannot load the configured SSH signing key without interaction; choose an approved noninteractive signing setup before another attempt")
            yield socket
        finally:
            if process.stdin is not None:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        raise factory.FactoryError(f"Private signing helper process {process.pid} did not stop; inspect that owned process and retained Git intent before another attempt") from None


@contextmanager
def isolated_git(args, identity, *, signing=False, read_only=False, config_paths=()):
    """Run Git and all its callbacks with local writes and no network access."""
    if factory.repository_identity(".") != identity or factory.git_dir(".") != args.worktree_git_dir:
        raise factory.FactoryError("Writer checkout changed; restore its original repository before local Git execution")
    root = Path(factory.git_worktree("."))
    common = factory.git_common_dir(".")
    python = str(getattr(args, "_git_python", None) or trusted_git_runtime(args))
    home = Path.home()
    protected = (home.resolve(), root, common, args.worktree_git_dir,
                 Path(tempfile.gettempdir()).resolve())
    def callback_path(path):
        path = path.resolve()
        if (any(value.is_relative_to(path) for value in protected)
                or any(path.is_relative_to(value) for value in protected[1:4])):
            raise factory.FactoryError("Unsupported pre-commit access: use dedicated tool/cache paths outside broad host roots, source and Git metadata")
        return path
    inherited = factory.command_environment()
    environment = {key: value for key, value in inherited.items()
                   if key in {"PATH", "HOME", "LANG"}
                   or key.startswith("LC_") or key.startswith("GIT_CONFIG")
                   or key in factory.GIT_IDENTITY_VARIABLES}
    if signing and read_only:
        raise factory.FactoryError("A callback scope cannot expose the signing agent")
    environment.update(PYTHONDONTWRITEBYTECODE="1", GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0",
                       GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    if read_only:
        environment = {key: value for key, value in environment.items() if not key.startswith("GIT_CONFIG")}
        environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    files = {":root": "deny", ":minimal": "read", str(root): "write",
             str(common): "deny" if read_only else "write",
             str(args.worktree_git_dir): "deny" if read_only else "write"}
    if read_only:
        files[str(common / "objects")] = "read"
    if (root / ".git").is_file() or (root / ".git").is_symlink():
        files[str(root / ".git")] = "read"
    base = callback_path(Path(sys.base_prefix))
    files.setdefault(str(base), "read")
    native = args.sandbox.resolve()
    files.setdefault(str(native), "read")
    if getattr(args, "_gitleaks", None) is not None:
        files[str(args._gitleaks)] = "read"
    package = native.parent.parent / "package.json"
    if native.parent.name == "bin" and package.is_file():
        try:
            if json.loads(package.read_text()).get("name") == "@openai/codex":
                if any((package.parent / "vendor").resolve().is_relative_to(path)
                       for path in (root, common, args.worktree_git_dir)):
                    raise factory.FactoryError("Keep the selected Codex runtime outside writer-writable paths")
                files.setdefault(str(package.parent / "vendor"), "read")
        except (OSError, ValueError, AttributeError):
            raise factory.FactoryError("Cannot inspect selected Codex package; restore its installation before local Git execution") from None
    for directory in {common, args.worktree_git_dir}:
        for name in ("config", "config.worktree", "commondir", "hooks", "info", factory.REPOSITORY_MARKER):
            files[str(directory / name)] = "deny" if read_only and name != "hooks" else "read"
    files[str(root / ".codex")] = "read"
    for name in ("bin", "Cellar", "opt", "lib"):
        runtime = Path("/opt/homebrew") / name
        if runtime.is_dir():
            files[str(callback_path(runtime))] = "read"
    for path in [home / ".gitconfig", *(home / ".config/git" / name for name in ("config", "ignore", "attributes")),
                 *(Path(inherited[key]) for key in ("GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM") if key in inherited)]:
        files[str(path.resolve())] = "deny" if read_only else "read"
    if read_only:
        for path in config_paths:
            files[str(path)] = "deny"
        files[os.devnull] = "read"
    # The configured local hook tool may use its already-installed offline cache.
    precommit = shutil.which("pre-commit")
    if precommit:
        tool = callback_path(Path(precommit).resolve().parent.parent)
        if tool != base and not (tool / "pyvenv.cfg").is_file():
            raise factory.FactoryError("Unsupported pre-commit installation: use a dedicated virtual environment or the factory Python installation")
        files.setdefault(str(tool), "read")
        interpreter = tool / "bin/python"
        if interpreter.is_file():
            executable = callback_path(interpreter)
            runtime = callback_path(executable.parent.parent)
            files.setdefault(str(executable), "read")
            if runtime != base:
                libraries = [path.parent for path in (runtime / "lib").glob("python*/os.py")]
                if not libraries:
                    raise factory.FactoryError("Unsupported pre-commit Python runtime; restore a dedicated Python installation")
                for path in [*libraries, *(runtime / "lib").glob("libpython*")]:
                    if not path.resolve().is_relative_to(runtime):
                        raise factory.FactoryError("Unsupported pre-commit runtime library outside its Python installation")
                    files.setdefault(str(callback_path(path)), "read")
        cache = Path(inherited.get("PRE_COMMIT_HOME", str(Path(inherited.get("XDG_CACHE_HOME", str(home / ".cache"))) / "pre-commit")))
        cache = callback_path(cache)
        if cache.name != "pre-commit":
            raise factory.FactoryError("Unsupported pre-commit cache: use a dedicated directory named pre-commit")
        files[str(cache)] = "read"
        environment["PRE_COMMIT_HOME"] = str(cache)
    selected = factory.git_run(".", "config", "--get", "user.signingkey", environment=inherited)
    enabled = factory.git_run(".", "config", "--bool", "--get", "commit.gpgsign", environment=inherited)
    method = factory.git_run(".", "config", "--get", "gpg.format", environment=inherited)
    if any(result.returncode not in (0, 1) for result in (selected, enabled, method)):
        raise factory.FactoryError("Cannot inspect local signing configuration; restore Git config access before committing")
    agent = None
    def command_prefix():
        # Config tables merge. A fresh command-local name prevents earlier
        # callbacks or loaded project profiles from adding grants to this one.
        name = "factory-git-" + uuid.uuid4().hex
        profile = '{extends=":read-only",network={enabled=false},filesystem={' + ",".join(
            json.dumps(path, ensure_ascii=False) + "=" + json.dumps(mode) for path, mode in files.items()) + "}}"
        prefix = [str(args.sandbox), "sandbox", "--permission-profile", name,
                  "-c", "permissions={" + name + "=" + profile + "}", "--cd", str(root)]
        if agent and sys.platform == "darwin":
            prefix.extend(["--allow-unix-socket", agent])
        digest = hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest()
        runtime = json.dumps({key: environment[key] for key in ("PATH", "TMPDIR") if key in environment})
        return [*prefix, "--", python, "-I", "-S", "-c", GIT_ENV_LAUNCHER,
                runtime, json.dumps(sorted(environment)), digest]
    with tempfile.TemporaryDirectory(prefix="factory-git-", dir=args.worktree_git_dir) as temporary, ExitStack() as stack:
        environment["TMPDIR"] = temporary
        files[temporary] = "write"
        if read_only:
            private_git = Path(temporary) / "git"
            (private_git / "objects/info").mkdir(parents=True)
            (private_git / "objects/info/alternates").write_text(str(common / "objects") + "\n")
            hooks = factory.git_run(".", "rev-parse", "--path-format=absolute", "--git-path", "hooks", environment=inherited)
            if hooks.returncode:
                raise factory.FactoryError("Cannot prepare the private pre-commit view; restore hook configuration")
            config = ("[core]\nrepositoryformatversion = " + ("1" if len(args.candidate) == 64 else "0")
                + "\nbare = false\nworktree = " + json.dumps(str(root), ensure_ascii=False)
                + "\nhooksPath = " + json.dumps(hooks.stdout.removesuffix("\n"), ensure_ascii=False)
                + "\nfsmonitor = false\nsplitIndex = false\n")
            if len(args.candidate) == 64:
                config += "[extensions]\nobjectFormat = sha256\n"
            (private_git / "config").write_text(config)
            (private_git / "HEAD").write_text(args.candidate + "\n")
            head = (args.worktree_git_dir / "HEAD").read_text().removesuffix("\n")
            if head.startswith("ref: refs/heads/"):
                reference = private_git / head[5:]
                if not reference.resolve().is_relative_to(private_git):
                    raise factory.FactoryError("Invalid assignment branch; inspect HEAD before another attempt")
                reference.parent.mkdir(parents=True, exist_ok=True)
                reference.write_text(args.candidate + "\n")
                (private_git / "HEAD").write_text(head + "\n")
            index = args.worktree_git_dir / "index"
            private_index = private_git / "index"
            if index.exists():
                if index.is_symlink() or not index.is_file():
                    raise factory.FactoryError("Restore a regular assignment index before invoking pre-commit")
                shutil.copyfile(index, private_index)
            environment["GIT_INDEX_FILE"] = str(private_index)
            environment.update(GIT_DIR=str(private_git), GIT_WORK_TREE=str(root))
        if signing and enabled.stdout.strip() == "true" and method.stdout.strip() == "ssh":
            public_key = Path(selected.stdout.strip()).expanduser()
            if public_key.suffix != ".pub" or not public_key.is_file():
                raise factory.FactoryError("Configure an SSH public-key file with its unlocked private companion for isolated signed commits")
            try:
                with public_key.open(encoding="utf-8") as stream:
                    public = stream.read(16385)
            except (OSError, UnicodeError):
                raise factory.FactoryError("Cannot read the configured SSH public key as UTF-8; restore a readable, valid SSH public-key file before another attempt") from None
            if len(public) > 16384 or not public.startswith(("ssh-", "ecdsa-", "sk-")):
                raise factory.FactoryError("The configured signing file must contain an SSH public key; keep private keys outside callback access")
            private_key = public_key.with_suffix("").resolve()
            if any(private_key.is_relative_to(path) for path in (root, common, args.worktree_git_dir)):
                raise factory.FactoryError("Keep private signing keys outside the writer checkout and Git metadata")
            agent = stack.enter_context(private_signer(private_key, python))
            environment["SSH_AUTH_SOCK"] = agent
            files[str(public_key.resolve())] = "read"
            files[str(private_key)] = "deny"
            files[agent] = "read"
        try:
            with factory.git_execution(command_prefix, environment):
                yield
        except Refusal:
            raise
        except factory.FactoryError as exc:
            if SANDBOX_HINT in str(exc):
                raise  # An inner scope already explained it.
            raise factory.FactoryError(str(exc) + SANDBOX_HINT) from None


def trusted_git_runtime(args):
    """Require native launchers and a runtime outside writer-controlled paths."""
    roots = (Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)
    python = Path(sys.executable).resolve()
    runtime = (python, args.sandbox.resolve(), Path(sys.prefix).absolute(), Path(sys.base_prefix).resolve())
    if any(path.is_relative_to(root) for path in runtime for root in roots):
        raise factory.FactoryError("Use a Python runtime outside the writer checkout and Git metadata before execution")
    for binary in (python, args.sandbox):
        with binary.open("rb") as stream:
            if stream.read(2) == b"#!":
                raise factory.FactoryError("Script-form launchers are unsupported for writer Git mode; select the installed native Codex binary and Python runtime")
    for entry in os.environ.get("PATH", os.defpath).split(os.pathsep):
        path = Path(entry or ".").absolute()
        if any(value.is_relative_to(root) for value in (path.resolve(), *(parent.resolve() for parent in path.parents)) for root in roots):
            raise factory.FactoryError("Remove writer-controlled PATH entries before execution; keep Git runtime lookup outside the checkout and metadata")
    return python


def commit_policy(args, environment):
    """Keep executable callbacks out of metadata-writing and signing phases."""
    observed = factory.git_run(".", "config", "--null", "--list", "--show-origin", "--includes", environment=environment)
    fields = observed.stdout.split("\0")
    if observed.returncode or fields[-1] or len(fields) % 2 != 1:
        raise factory.FactoryError("Cannot inspect configured hooks/signing; restore Git configuration before another attempt")
    root, common = Path(factory.git_worktree(".")), factory.git_common_dir(".")
    allowed = {common / "config", common / "config.worktree", args.worktree_git_dir / "config.worktree"}
    values, drivers, filter_flags, config_paths = {}, set(), [], set()
    for origin, row in zip(fields[0:-1:2], fields[1:-1:2]):
        key, _, value = row.partition("\n")
        values[key.lower()] = value
        if origin.startswith("file:"):
            path = (root / Path(origin[5:])).resolve()
            config_paths.add(path)
            if path.is_relative_to(root) and path not in allowed:
                raise factory.FactoryError("Unsupported configured hooks/signing: keep Git configuration outside writer-controlled source")
            if key.lower() == "include.path" or key.lower().startswith("includeif.") and key.lower().endswith(".path"):
                target = (path.parent / Path(value).expanduser()).resolve()
                config_paths.add(target)
                if "%" in value or target.is_relative_to(root) and target not in allowed:
                    raise factory.FactoryError("Unsupported configured hooks/signing: includes cannot load writer-controlled configuration")
        if (key.lower().startswith("filter.") and key.lower().endswith((".clean", ".smudge", ".process")) and value.strip()):
            drivers.add(key.rsplit(".", 1)[0][7:])
            filter_flags.extend(["-c", key + "="])
        if (key.lower() == "core.alternaterefscommand" and value.strip()
                or key.lower() == "extensions.partialclone" and value.strip()
                or key.lower().startswith("remote.") and key.lower().endswith(".promisor") and value.lower() in {"true", "yes", "on", "1"}):
            raise factory.FactoryError("Unsupported configured hooks/signing: use complete local objects without executable alternate-ref helpers")
    hooks = factory.git_run(".", "rev-parse", "--path-format=absolute", "--git-path", "hooks", environment=environment)
    if hooks.returncode:
        raise factory.FactoryError("Cannot inspect configured hooks/signing; restore hook configuration before another attempt")
    directory = Path(hooks.stdout.removesuffix("\n"))
    active = sorted(path.name for path in directory.iterdir() if path.is_file() and os.access(path, os.X_OK)
                    and not path.name.endswith(".sample")) if directory.is_dir() else []
    if active and directory.resolve() not in {common / "hooks", args.worktree_git_dir / "hooks"}:
        raise factory.FactoryError("Unsupported configured hooks/signing: external hook paths require an inspected explicit local commit")
    if any(name != "pre-commit" for name in active):
        raise factory.FactoryError("Unsupported configured hooks/signing: only the separately confined pre-commit hook is supported")
    enabled = factory.git_run(".", "config", "--bool", "--get", "commit.gpgsign", environment=environment)
    if enabled.returncode not in (0, 1):
        raise factory.FactoryError("Cannot inspect configured hooks/signing; repair commit.gpgsign before another attempt")
    flags = ["-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false",
             "-c", "core.splitIndex=false", "-c", "gc.auto=0", "-c", "maintenance.auto=false",
             "-c", "i18n.commitEncoding=UTF-8", *filter_flags]
    if enabled.stdout.strip() == "true":
        keygen = shutil.which("ssh-keygen", path=os.defpath)
        program = values.get("gpg.ssh.program", "ssh-keygen")
        selected = shutil.which(program, path=os.defpath)
        if (values.get("gpg.format") != "ssh" or not keygen or not selected
                or Path(selected).resolve() != Path(keygen).resolve()):
            raise factory.FactoryError("Unsupported configured hooks/signing: signed factory commits require the operating system's SSH signer")
        flags.extend(["-c", "gpg.format=ssh", "-c", "commit.gpgsign=true",
                      "-c", "gpg.ssh.program=" + keygen])
        if values.get("user.signingkey"):
            flags.extend(["-c", "user.signingkey=" + values["user.signingkey"]])
    else:
        flags.extend(["-c", "commit.gpgsign=false"])
    # Every worktree shares one config file, so a push -u or a merged-branch prune in another worktree rewrites
    # branch.* sections mid-commit; they name upstreams and never change how a commit is made.
    guarded = "\0".join(origin + "\0" + row for origin, row in zip(fields[0:-1:2], fields[1:-1:2])
                        if not row.partition("\n")[0].lower().startswith("branch."))
    return dict(fingerprint=hashlib.sha256(guarded.encode()).hexdigest(),
                precommit="pre-commit" in active, flags=flags, drivers=drivers, config_paths=config_paths)


def scanner_binary(args):
    roots = (Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir,
             Path(tempfile.gettempdir()).resolve(), Path("/tmp").resolve())
    def tool(name):
        selected = shutil.which(name)
        if not selected:
            raise factory.FactoryError("Install a native Gitleaks release, or Go for an unstamped Go build, before automatic writer commits")
        path = Path(selected).resolve()
        if not path.is_file() or any(path.is_relative_to(root) for root in roots):
            raise factory.FactoryError("Install scanner tools outside writer-controlled and temporary paths before automatic commits")
        with path.open("rb") as stream:
            if stream.read(2) == b"#!":
                raise factory.FactoryError("Use native scanner tools rather than script wrappers for automatic commits")
        return path
    binary = tool("gitleaks")
    try:
        scanner = factory.decode_json((factory.ROOT / "config/scanner.json").read_text())
        pinned = scanner.get("gitleaks") if isinstance(scanner, dict) else None
        pin = re.fullmatch(r"([0-9]+\.[0-9]+\.[0-9]+)", pinned) if isinstance(pinned, str) else None
        if pin is None:
            raise factory.FactoryError("Restore the pinned Gitleaks version in the runtime's config/scanner.json before automatic commits")
        version = subprocess.run([str(binary), "version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10, check=False, env=tool_environment())
        verified = not version.returncode and not version.stderr and version.stdout.strip() == pin[1]
        if not version.returncode and not version.stderr and version.stdout.strip() == "version is set by build process":
            metadata = subprocess.run([str(tool("go")), "version", "-m", str(binary)], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10, check=False, env=tool_environment())
            module = "\tmod\tgithub.com/zricethezav/gitleaks/v8\tv" + pin[1] + "\t"
            verified = not metadata.returncode and not metadata.stderr and any(
                line.startswith(module) for line in metadata.stdout.splitlines())
        if not verified:
            raise factory.FactoryError("Scanner identity/version does not match the Gitleaks pin; install the pinned native release before starting a writer")
    except (OSError, UnicodeError, ValueError, subprocess.TimeoutExpired):
        raise factory.FactoryError("Cannot verify the pinned scanner; restore its native installation and manifest before starting a writer") from None
    return binary


def added_lines(patch):
    """The lines a unified diff adds, each with the path it belongs to (labels are best effort)."""
    path, in_hunk = None, False
    for line in patch.split(b"\n"):
        if line.startswith(b"diff --git "):
            path, in_hunk = None, False
        elif not in_hunk and line.startswith(b"+++ "):
            label = line[4:].decode("utf-8", "replace").strip('"')
            path = label[2:] if label.startswith("b/") else label
        elif line.startswith(b"@@"):
            in_hunk = True
        elif in_hunk and line.startswith(b"+"):
            yield path, line[1:]


def scan_staged(args, receipt, *, git_scope=isolated_git):
    """Scan what the writer adds, and the commit metadata, without a signer.

    Only the writer's additions are its responsibility: a repository whose existing tree
    holds test fixtures or documentation examples that match default rules must still
    accept commits. The writer cannot exempt its additions: allow comments and ignore
    files are disabled.
    """
    if getattr(args, "_gitleaks", None) is None:
        args._gitleaks = scanner_binary(args)
    # Resolve identities against the same original config/environment used by
    # the final commit; the private scanner view omits that configuration.
    with git_scope(args, receipt["repository_identity"]):
        identities = [factory.git_run(".", "var", name) for name in ("GIT_AUTHOR_IDENT", "GIT_COMMITTER_IDENT")]
        if any(value.returncode for value in identities):
            raise factory.FactoryError("Cannot resolve commit identities for secret scanning; configure valid Git author/committer identities before another attempt")
        identity_text = "\n".join(value.stdout for value in identities)
    with git_scope(args, receipt["repository_identity"], read_only=True):
        scope = factory.GIT_EXECUTION.get()
        if scope is None:
            raise factory.FactoryError("Secret scanning requires the isolated Git profile; no host fallback is allowed")
        prefix, environment = scope
        environment["PATH"] = os.defpath
        config = Path(environment["TMPDIR"]) / "scanner.toml"
        config.write_text("[extend]\nuseDefault = true\n")
        staged, parent = receipt["commit"]["staged_tree"], args.candidate
        paths = factory.git_run(".", "diff", "--name-only", "-z", "--no-renames", parent, staged)
        if paths.returncode or paths.stdout and not paths.stdout.endswith("\0"):
            raise factory.FactoryError("Cannot read changed filenames for secret scanning; inspect the retained tree before another attempt")
        report = Path(environment["TMPDIR"]) / "scan-report.json"
        try:
            # Build the additions ourselves rather than use the scanner's Git-diff parser:
            # some versions report success after Git stderr errors without scanning them.
            patch = subprocess.run([*prefix(), factory.git_binary(), "--no-replace-objects", "diff", "--no-ext-diff",
                "--no-textconv", "--text", "--unified=0", "--no-renames", "--no-color", parent, staged],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment, timeout=60, check=False)
            if patch.returncode:
                raise factory.FactoryError("Cannot read the writer's changes for secret scanning; inspect the retained tree before another attempt")
            header = (identity_text + "\n" + args.commit_message + "\n" + paths.stdout.replace("\0", "\n")).encode()
            sources, chunks = [], [header]
            line_number = header.count(b"\n") + 1
            for path, line in added_lines(patch.stdout):
                sources.append((line_number, path))
                chunks.append(line + b"\n")
                line_number += 1
            with tempfile.TemporaryFile(dir=environment["TMPDIR"]) as stream:
                stream.write(b"".join(chunks))
                stream.flush()
                stream.seek(0)
                result = subprocess.run([*prefix(), str(args._gitleaks), "stdin",
                    "--config", str(config), "--gitleaks-ignore-path", os.devnull, "--ignore-gitleaks-allow",
                    "--redact=100", "--no-banner", "--log-level", "error", "--timeout", "30",
                    "--report-format", "json", "--report-path", str(report)],
                    stdin=stream, env=environment, capture_output=True, timeout=35, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise factory.FactoryError("Secret scanner unavailable; inspect the retained staged tree and restore offline Gitleaks before another attempt") from None
        if result.returncode or result.stderr:
            raise Refusal("Secret scan refused the writer's additions" + scan_findings(report, sources)
                          + "; inspect them before another attempt. No commit was retried")


def scan_findings(report, sources):
    """Redacted rule and path for each finding, when the scanner wrote its report."""
    try:
        findings = json.loads(report.read_text())
    except (OSError, ValueError):
        return ""
    if not isinstance(findings, list) or not findings:
        return ""
    def where(line):
        if not isinstance(line, int):
            return "commit metadata"
        found = [path for start, path in sources if start <= line]
        return found[-1] if found and found[-1] else "commit metadata"
    labels = sorted({f'{item.get("RuleID", "unknown rule")} in {where(item.get("StartLine"))}'
                     for item in findings if isinstance(item, dict)})
    return ": " + ", ".join(labels[:20]) + (f" and {len(labels) - 20} more" if len(labels) > 20 else "")


def check_git_operation(args):
    markers = ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer", "BISECT_START")
    if any(os.path.lexists(args.worktree_git_dir / name) for name in markers):
        raise factory.FactoryError("In-progress Git operation; finish or abort it in the assigned worktree before a factory commit")


def discard(root, entries):
    """Remove the writer's ignored outputs without running Git on the host.

    The sandboxed git clean cannot write the directories Codex's sandbox protects (.codex/, .agents/), where
    an acr realize left output. The paths come from the sandboxed status; a link goes, its target stays.
    """
    for entry in entries:
        target = root / entry.rstrip("/")
        if (".." in Path(entry).parts or Path(entry).is_absolute()
                or any(parent.is_symlink() for parent in target.parents if parent != root and root in parent.parents)):
            raise factory.FactoryError("A writer's ignored output sits under a link or outside its checkout; inspect it before another attempt")
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        except OSError as exc:
            # A read-only tree (a node_modules with 0555 directories) must name itself, not pass as I/O trouble.
            raise factory.FactoryError(f"Cannot discard the writer's ignored output {entry!r} ({exc.strerror}); "
                                       "inspect it before another attempt") from None


def commit_changes(args, receipt, *, git_scope=isolated_git, scan=scan_staged):
    """Commit an opted-in writer's finished work in the factory Git sandbox."""
    check_git_operation(args)
    if factory.git_commit(".", "HEAD") != args.candidate:
        raise factory.FactoryError("Writer changed Git HEAD before the factory commit; inspect the original candidate and retained work without retrying")
    if factory.repository_identity(".") != receipt["repository_identity"] or factory.git_dir(".") != args.worktree_git_dir:
        raise factory.FactoryError("Writer repository/worktree changed; restore the original checkout before inspecting its commit")
    root = Path(factory.git_worktree("."))
    environment = factory.command_environment()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    policy = commit_policy(args, environment)
    def git(*command, **options):
        return factory.git_run(".", *policy["flags"], *command, **options)
    def check_filters(names):
        for start in range(0, len(names), 100):
            attributes = git("check-attr", "-z", "filter", "--", *names[start:start + 100])
            fields = attributes.stdout.split("\0")
            if attributes.returncode or fields[-1] or len(fields) % 3 != 1:
                raise factory.FactoryError("Cannot inspect configured hooks/signing; repair Git attribute access before another attempt")
            if any(value in policy["drivers"] for value in fields[2:-1:3]):
                raise factory.FactoryError("Unsupported configured hooks/signing: active Git filters require an inspected explicit local commit")
    # Even status can execute configured clean filters; retain intent first.
    paths = git("ls-files", "--cached", "--others", "--exclude-standard", "-z")
    if paths.returncode:
        raise factory.FactoryError("Cannot inspect writer source paths; restore Git access before staging changes")
    check_filters([name for name in paths.stdout.split("\0") if name])
    for name in paths.stdout.split("\0"):
        if not name:
            continue
        source = root / name
        if any(parent.is_symlink() for parent in source.parents if parent != root and root in parent.parents):
            raise factory.FactoryError("Writer contains a symlinked source directory; restore independent source paths inside its assigned worktree")
        try:
            info = source.lstat()
        except FileNotFoundError:
            continue  # Tracked deletions are part of the writer's change.
        if not stat.S_ISREG(info.st_mode):
            raise factory.FactoryError("Writer contains linked or non-regular source; inspect the retained changes and use independent regular files")
        if info.st_nlink > 1:
            # Materialize finished output without changing its other hard links.
            factory.independent_source(source)
    def ignored():
        status = git("status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored", environment=environment)
        if status.returncode:
            raise factory.FactoryError("Cannot read writer Git status; restore Git access before staging changes")
        return [entry[3:] for entry in status.stdout.split("\0") if entry.startswith("!! ")]
    # A provisioned worktree starts with no ignored files, so these are the writer's disposable
    # outputs (test caches, bytecode, installs). The factory discards them and records the paths.
    discarded = ignored()
    discard(root, discarded)
    if discarded and ignored():
        raise factory.FactoryError("Cannot discard the writer's ignored outputs; inspect them before another attempt")
    # Git cannot track empty directories (test suites leave them), so they are disposable too.
    for current, _, _ in os.walk(root, topdown=False):
        directory = Path(current)
        if directory != root and ".git" not in directory.relative_to(root).parts and not any(directory.iterdir()):
            directory.rmdir()
            discarded.append(directory.relative_to(root).as_posix() + "/")
    if discarded:
        receipt["commit"]["discarded"] = discarded
        factory.save(args.receipt, receipt)
    staged = git("add", "--all", "--", ".", environment=environment)
    if staged.returncode:
        raise factory.FactoryError("Cannot stage writer changes; inspect the retained commit intent, index and worktree before correcting it")
    if git("update-index", "--no-split-index", environment=environment).returncode:
        raise factory.FactoryError("Cannot prepare the assignment index; inspect its retained staged work before another attempt")
    entries = git("ls-files", "--stage", "-z")
    if entries.returncode:
        raise factory.FactoryError("Cannot inspect staged writer paths; restore Git access and inspect the retained attempt")
    try:
        for entry in entries.stdout.split("\0"):
            if not entry:
                continue
            metadata, name = entry.split("\t", 1)
            mode, _, stage = metadata.split()
            source = root / name
            if any(parent.is_symlink() for parent in source.parents if parent != root and root in parent.parents):
                raise factory.FactoryError("Writer staged a symlinked source directory; inspect its retained changes before committing")
            info = source.lstat()
            if mode not in {"100644", "100755"} or stage != "0" or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise factory.FactoryError("Writer staged linked or non-regular source; inspect the retained changes and use independent regular files")
    except ValueError:
        raise factory.FactoryError("Cannot decode the writer index; inspect Git compatibility and the retained changes before committing") from None
    tree = git("write-tree", environment=environment)
    if tree.returncode:
        raise factory.FactoryError("Cannot record the staged writer tree; inspect the retained index and commit intent")
    receipt["commit"].update(state="committing", staged_tree=tree.stdout.strip())
    factory.save(args.receipt, receipt)
    unchanged = git("diff", "--cached", "--quiet", "--exit-code", "--no-ext-diff", "--no-textconv", args.candidate)
    if unchanged.returncode == 0:
        receipt["commit"].update(state="unchanged", candidate=args.candidate)
        factory.save(args.receipt, receipt)
        return
    if unchanged.returncode != 1:
        raise factory.FactoryError("Cannot compare staged writer changes; inspect the retained index before another action")
    scan(args, receipt, git_scope=git_scope)
    hooked = None
    if policy["precommit"]:
        with git_scope(args, receipt["repository_identity"], read_only=True, config_paths=policy["config_paths"]):
            hooked = factory.git_run(".", "-c", "core.fsmonitor=false", "-c", "gc.auto=0",
                "-c", "maintenance.auto=false", "-c", "core.splitIndex=false",
                "hook", "run", "--ignore-missing", "pre-commit", environment=environment)
            checked = factory.git_run(".", "-c", "core.fsmonitor=false", "diff-index", "--cached", "--quiet",
                "--no-ext-diff", "--no-textconv", receipt["commit"]["staged_tree"])
            if checked.returncode:
                raise factory.FactoryError("Pre-commit changed its private index; inspect staged inputs before an explicit local commit")
        if hooked.returncode:
            raise factory.FactoryError("Writer commit failed: pre-commit refused" + git_said(hooked)
                                       + "; inspect the retained commit intent, Git HEAD and configured hooks/signing. No commit was retried")
    if commit_policy(args, environment) != policy:
        raise factory.FactoryError("Git configuration changed during pre-commit; inspect the retained work before another attempt")
    try:
        with git_scope(args, receipt["repository_identity"], signing=True):
            committed = git("commit", "--message", args.commit_message, environment=environment)
    except factory.FactoryError:
        raise factory.FactoryError("Writer commit outcome is uncertain; inspect the retained commit intent, Git HEAD and hooks before another action. No commit was retried") from None
    if committed.returncode:
        raise factory.FactoryError("Writer commit failed" + git_said(committed) + "; inspect the retained commit intent, "
                                   "Git HEAD and configured hooks/signing before authorizing a correction. No commit was retried")
    candidate = factory.git_commit(".", "HEAD")
    parent = git("show", "-s", "--format=%P", candidate)
    if parent.returncode or parent.stdout.strip() != args.candidate:
        raise factory.FactoryError("Writer commit parent differs; inspect the original worktree and commit intent before continuing")
    receipt["commit"].update(state="created", candidate=candidate)
    factory.save(args.receipt, receipt)
    return hooked


def check_commit_receipt(receipt, args, repository, *, historical=False):
    """Version 4 has no factory commit; version 5 declares its nullable intent."""
    if factory.repository_identity(repository) != receipt["repository_identity"]:
        raise factory.FactoryError("Admitted repository identity changed; restore its original repository marker before validating provenance")
    if not historical and (factory.repository_identity(str(args.cwd)) != receipt["repository_identity"]
            or factory.git_dir(str(args.cwd)) != args.worktree_git_dir):
        raise factory.FactoryError("Assignment worktree binding differs at collection; restore its original repository and Git directory")
    if args.write and not historical and factory.git_commit(str(args.cwd), "HEAD") != receipt["candidate"]:
        raise factory.FactoryError("Writer candidate differs from the assigned worktree HEAD; inspect its retained checkout before collection")
    if factory.git_commit(repository, receipt["candidate"]) != receipt["candidate"]:
        raise factory.FactoryError("Restore the original writer candidate objects before validating historical provenance")
    if factory.git_commit(repository, receipt["inputs"]["candidate"]) != receipt["inputs"]["candidate"]:
        raise factory.FactoryError("Restore the original assignment parent objects before validating provenance")
    tree_object = factory.git_run(repository, "cat-file", "-e", receipt["candidate"] + "^{tree}")
    if tree_object.returncode:
        raise factory.FactoryError("Restore the original candidate tree objects before validating provenance")
    commit = receipt.get("commit")
    if receipt["schema_version"] == 4:
        if "commit" in receipt or args.commit_message is not None:
            raise factory.FactoryError("Legacy receipt cannot supply a factory commit; inspect the original attempt without rewriting or relaunching it")
        return
    expected = args.commit_message is not None and receipt["report"]["outcome"] == "completed"
    if "commit" not in receipt or (commit is None and expected):
        raise factory.FactoryError("Factory commit evidence is missing; inspect the original receipt and Git HEAD before collection")
    if commit is None:
        return
    if (not expected or not args.write or not isinstance(commit, dict)
            or set(commit) - {"discarded"} != {"state", "parent", "message", "staged_tree", "candidate"}
            or "discarded" in commit and (not isinstance(commit["discarded"], list) or not commit["discarded"]
                                          or any(not isinstance(name, str) or not name for name in commit["discarded"]))
            or commit.get("state") not in {"created", "unchanged"}
            or commit.get("parent") != receipt["inputs"]["candidate"]
            or commit.get("candidate") != receipt["candidate"]
            or commit.get("message") != args.commit_message
            or not isinstance(commit.get("staged_tree"), str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", commit["staged_tree"])
            or (commit["state"] == "unchanged") != (commit["candidate"] == commit["parent"])):
        raise factory.FactoryError("Factory commit evidence differs from this assignment; inspect its original intent and Git history")
    if commit["state"] == "created":
        parent = factory.git_run(repository, "show", "-s", "--format=%P", commit["candidate"])
        if parent.returncode or parent.stdout.strip() != commit["parent"]:
            raise factory.FactoryError("Factory commit parent differs from the assignment; inspect its original Git history before collection")
    tree = factory.git_run(repository, "rev-parse", "--verify", commit["candidate"] + "^{tree}")
    if tree.returncode or tree.stdout.strip() != commit["staged_tree"]:
        raise factory.FactoryError("Factory commit staged tree differs from the candidate; inspect the original receipt and Git tree before collection")


def prepare_writer(args, sandbox, roots):
    """Pre-launch checks for an opted-in writer; raises before any native effect."""
    if any(sandbox.is_relative_to(root) for root in roots):
        raise factory.FactoryError("Use a trusted Codex installation outside the writer checkout and Git metadata")
    args.sandbox = sandbox
    args._git_python = trusted_git_runtime(args)


def prepare_commit(args, *, scan=scan_staged):
    """Pre-launch checks for a factory commit: clean Git state and the pinned scanner."""
    check_git_operation(args)
    if scan is scan_staged:
        args._gitleaks = scanner_binary(args)


def commit_completed(args, receipt, identity, *, git_scope=isolated_git, scan=scan_staged):
    """Record commit intent for a completed report, commit it, and return the resulting HEAD."""
    committing = args.commit_message is not None and receipt["report"]["outcome"] == "completed"
    if committing:
        receipt["commit"] = dict(state="preparing", parent=args.candidate, message=args.commit_message,
                                 staged_tree=None, candidate=None)
        factory.save(args.receipt, receipt)
    with git_scope(args, identity) if args.commit_message is not None else nullcontext():
        if committing:
            commit_changes(args, receipt, git_scope=git_scope, scan=scan)
        candidate = factory.git_commit(".", "HEAD")
        try:
            factory.check_checkout(".", ".", candidate, expected_identity=identity,
                                   expected_git_dir=args.worktree_git_dir)
        except factory.FactoryError as exc:
            outcome = receipt["report"]["outcome"]
            if args.commit_message is not None and outcome != "completed":
                # Two writers reported findings after a green gate and lost their commits to a misleading error.
                raise Refusal(f"The writer reported {outcome}, so the factory committed nothing and its edits remain "
                              "in the checkout; save them with factory.py reset and have a new writer finish with "
                              "outcome completed, listing residual findings in its artifacts") from None
            raise Refusal("The checkout is not clean after the run (" + str(exc) + ")") from None
    return candidate
