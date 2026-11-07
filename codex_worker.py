"""One explicit native Codex assignment; retain its original events and compact result."""
import argparse
import asyncio
from contextlib import contextmanager, ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.dont_write_bytecode = True

import factory

NATIVE_LINE_LIMIT = 1024 * 1024


def native_environment():
    """Keep native subscription configuration without host credentials."""
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES",
               "TMPDIR", "TMP", "TEMP", "TERM", "COLORTERM", "CODEX_HOME", "XDG_CONFIG_HOME",
               "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    return {**{key: value for key, value in os.environ.items() if key in allowed},
            "PYTHONDONTWRITEBYTECODE": "1", "GIT_NO_REPLACE_OBJECTS": "1"}


GIT_ENV_LAUNCHER = """import hashlib,json,os,sys
environment = {key: os.environ[key] for key in json.loads(sys.argv[2]) if key in os.environ}
environment.update(json.loads(sys.argv[1]))
if hashlib.sha256(json.dumps(environment, sort_keys=True).encode()).hexdigest() != sys.argv[3]:
    raise SystemExit(125)
os.execve(sys.argv[4], sys.argv[4:], environment)
"""


class WorkerError(ValueError):
    pass


def native_object(pairs):
    # Observed CLI web-search telemetry repeats its item and execution IDs.
    # Neither ID supplies evidence or authority; retain the raw line and omit both.
    if sum(key == "id" for key, _ in pairs) > 1 and dict(pairs).get("type") == "web_search":
        return factory.unique_object([(key, value) for key, value in pairs if key != "id"])
    return factory.unique_object(pairs)


def outcome_schema():
    return dict(type="object", additionalProperties=False,
                properties=dict(outcome=dict(type="string", enum=sorted(factory.RESULT_OUTCOMES)),
                                artifacts=dict(type="array", items=dict(type="string"))),
                required=["outcome", "artifacts"])


def parse_events(events) -> dict:
    """A native completion is a carrier for a specialist report, never task acceptance."""
    thread, phase, message, usage = None, "before-thread", None, None
    for event in events:
        if not isinstance(event, dict):
            raise WorkerError("Native output is not an event object; inspect the retained stream")
        kind = event.get("type")
        if kind == "thread.started":
            if phase != "before-thread" or not isinstance(event.get("thread_id"), str) or not event["thread_id"]:
                raise WorkerError("Native worker returned an ambiguous thread; inspect its original events")
            thread = event["thread_id"]
            phase = "before-turn"
        elif kind == "turn.started":
            if phase != "before-turn":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            phase = "active"
        elif kind in {"turn.failed", "error"}:
            raise WorkerError("Native worker failed; inspect its original events before another attempt")
        elif isinstance(kind, str) and kind.startswith("item."):
            item = event.get("item")
            # The native CLI emits nonfatal startup diagnostics after thread start.
            # They carry no report, turn or completion evidence.
            if phase == "before-turn" and kind == "item.completed" and isinstance(item, dict) and item.get("type") == "error":
                continue
            if phase != "active":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            if kind == "item.completed":
                if not isinstance(item, dict):
                    raise WorkerError("Native item is malformed; inspect the retained stream")
                if item.get("type") == "agent_message":
                    message = item.get("text")
        elif kind == "turn.completed":
            if phase != "active":
                raise WorkerError("Native lifecycle is out of order; reconcile the existing attempt")
            phase = "finished"
            usage = event.get("usage")
    if phase != "finished":
        raise WorkerError("Native completion is missing or ambiguous; reconcile the existing attempt")
    if not isinstance(message, str):
        raise WorkerError("Native final message is missing; inspect its original events")
    try:
        report = factory.decode_json(message)
    except (ValueError, TypeError):
        raise WorkerError("Native final message is not a structured result; inspect its original events") from None
    if (not isinstance(report, dict) or set(report) != {"outcome", "artifacts"}
            or not isinstance(report["outcome"], str) or report["outcome"] not in factory.RESULT_OUTCOMES
            or not isinstance(report["artifacts"], list) or not report["artifacts"]
            or any(not isinstance(ref, str) or not ref.strip() for ref in report["artifacts"])):
        raise WorkerError("Native result needs a supported outcome and original evidence references")
    if usage is not None and (not isinstance(usage, dict)
            or any(type(usage.get(key)) is not int or usage[key] < 0
                   for key in ("input_tokens", "output_tokens"))):
        raise WorkerError("Native usage is malformed; retain unknown consumption instead of inventing it")
    return dict(thread_id=thread, report=report, usage=usage)


def read_events(path):
    """Read versioned local records without changing the original native lines."""
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        raise WorkerError("Cannot read native events; restore the original stream and its access before collection") from None
    events = []
    for line in lines:
        try:
            record = factory.decode_json(line)
        except (ValueError, UnicodeError):
            raise WorkerError("Malformed native events; inspect the retained stream before collecting or retrying") from None
        if (not isinstance(record, dict) or type(record.get("schema_version")) is not int
                or record["schema_version"] != 1 or record.get("kind") != "codex-event-line"
                or not isinstance(record.get("data"), str)):
            raise WorkerError("Unsupported native event record; inspect the original stream and update the adapter")
        try:
            record["data"].encode("utf-8")  # Invalid native bytes remain escaped evidence, never valid events.
            events.append(factory.decode_json(record["data"], object_pairs_hook=native_object))
        except (ValueError, UnicodeError):
            raise WorkerError("Malformed native events; inspect the retained stream before collecting or retrying") from None
    return events


async def stop_child(process):
    if process.returncode is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
    except ProcessLookupError:
        await process.wait()
        return  # The owned process already exited.
    try:
        await asyncio.wait_for(process.wait(), timeout=5)
    except TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass  # The owned child exited before the signal; still observe its exit below.
        await process.wait()


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
    native = args.codex.resolve()
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
        prefix = [str(args.codex), "sandbox", "--permission-profile", name,
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
        except factory.FactoryError as exc:
            raise factory.FactoryError(str(exc) + "; inspect native Git sandbox support and offline hook/signing access before another attempt; no host fallback was used") from None


def trusted_git_runtime(args):
    """Require native launchers and a runtime outside writer-controlled paths."""
    roots = (Path(factory.git_worktree(".")), factory.git_common_dir("."), args.worktree_git_dir)
    python = Path(sys.executable).resolve()
    runtime = (python, args.codex.resolve(), Path(sys.prefix).absolute(), Path(sys.base_prefix).resolve())
    if any(path.is_relative_to(root) for path in runtime for root in roots):
        raise factory.FactoryError("Use a Python runtime outside the writer checkout and Git metadata before execution")
    for binary in (python, args.codex):
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
    return dict(fingerprint=hashlib.sha256(observed.stdout.encode()).hexdigest(),
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
        config = (factory.ROOT / ".pre-commit-config.yaml").read_text()
        pin = re.search(r"(?m)^  - repo: https://github.com/gitleaks/gitleaks\n    rev: v([0-9]+\.[0-9]+\.[0-9]+)\s*$", config)
        if pin is None:
            raise factory.FactoryError("Restore the pinned Gitleaks entry in .pre-commit-config.yaml before automatic commits")
        version = subprocess.run([str(binary), "version"], stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10, check=False, env=native_environment())
        verified = not version.returncode and not version.stderr and version.stdout.strip() == pin[1]
        if not version.returncode and not version.stderr and version.stdout.strip() == "version is set by build process":
            metadata = subprocess.run([str(tool("go")), "version", "-m", str(binary)], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=10, check=False, env=native_environment())
            module = "\tmod\tgithub.com/zricethezav/gitleaks/v8\tv" + pin[1] + "\t"
            verified = not metadata.returncode and not metadata.stderr and any(
                line.startswith(module) for line in metadata.stdout.splitlines())
        if not verified:
            raise factory.FactoryError("Scanner identity/version does not match the Gitleaks pin; install the pinned native release before starting a writer")
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise factory.FactoryError("Cannot verify the pinned scanner; restore its native installation and manifest before starting a writer") from None
    return binary


def scan_staged(args, receipt, *, git_scope=isolated_git):
    """Scan staged data and resolved commit metadata without a signer."""
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
        objects = factory.git_run(".", "ls-tree", "-r", "--format=%(objectname)", receipt["commit"]["staged_tree"])
        if objects.returncode or not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", oid)
                                         for oid in objects.stdout.splitlines()):
            raise factory.FactoryError("Cannot enumerate the staged tree for secret scanning; inspect the retained index before another attempt")
        paths = factory.git_run(".", "ls-tree", "-r", "--name-only", "-z", receipt["commit"]["staged_tree"])
        if paths.returncode or paths.stdout and not paths.stdout.endswith("\0"):
            raise factory.FactoryError("Cannot read staged filenames for secret scanning; inspect the retained tree before another attempt")
        try:
            # Avoid the scanner's Git-diff parser: some versions report success
            # after Git stderr errors without scanning its additions.
            with tempfile.TemporaryFile(dir=environment["TMPDIR"]) as blobs:
                blobs.write((identity_text + "\n" + args.commit_message + "\n" + paths.stdout.replace("\0", "\n")).encode())
                blobs.flush()
                copied = subprocess.run([*prefix(), factory.git_binary(), "--no-replace-objects",
                    "cat-file", "--batch"], input=objects.stdout.encode(), stdout=blobs,
                    stderr=subprocess.PIPE, env=environment, timeout=30, check=False)
                if copied.returncode:
                    raise factory.FactoryError("Cannot read staged blobs for secret scanning; inspect the retained tree before another attempt")
                blobs.seek(0)
                result = subprocess.run([*prefix(), str(args._gitleaks), "stdin",
                    "--config", str(config), "--gitleaks-ignore-path", os.devnull, "--ignore-gitleaks-allow",
                    "--redact=100", "--no-banner", "--log-level", "error", "--timeout", "30"],
                    stdin=blobs, env=environment, capture_output=True, timeout=35, check=False)
        except (OSError, subprocess.TimeoutExpired):
            raise factory.FactoryError("Secret scanner unavailable; inspect the retained staged tree and restore offline Gitleaks before another attempt") from None
        if result.returncode or result.stderr:
            raise factory.FactoryError("Secret scan refused the staged tree; inspect it with redacted Gitleaks output before another attempt. No commit was retried")


def check_git_operation(args):
    markers = ("CHERRY_PICK_HEAD", "MERGE_HEAD", "REVERT_HEAD", "rebase-apply", "rebase-merge", "sequencer", "BISECT_START")
    if any(os.path.lexists(args.worktree_git_dir / name) for name in markers):
        raise factory.FactoryError("In-progress Git operation; finish or abort it in the assigned worktree before a factory commit")


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
    status = git("status", "--porcelain=v1", "--untracked-files=all", "--ignored", environment=environment)
    if status.returncode or any(line.startswith("!!") for line in status.stdout.splitlines()):
        raise factory.FactoryError("Writer left ignored outputs or unreadable Git status; inspect and remove only its disposable outputs before authorizing a correction")
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
            raise factory.FactoryError("Writer commit failed; inspect the retained commit intent, Git HEAD and configured hooks/signing. No commit was retried")
    if commit_policy(args, environment) != policy:
        raise factory.FactoryError("Git configuration changed during pre-commit; inspect the retained work before another attempt")
    try:
        with git_scope(args, receipt["repository_identity"], signing=True):
            committed = git("commit", "--message", args.commit_message, environment=environment)
    except factory.FactoryError:
        raise factory.FactoryError("Writer commit outcome is uncertain; inspect the retained commit intent, Git HEAD and hooks before another action. No commit was retried") from None
    if committed.returncode:
        raise factory.FactoryError("Writer commit failed; inspect the retained commit intent, Git HEAD and configured hooks/signing before authorizing a correction. No commit was retried")
    candidate = factory.git_commit(".", "HEAD")
    parent = git("show", "-s", "--format=%P", candidate)
    if parent.returncode or parent.stdout.strip() != args.candidate:
        raise factory.FactoryError("Writer commit parent differs; inspect the original worktree and commit intent before continuing")
    receipt["commit"].update(state="created", candidate=candidate)
    factory.save(args.receipt, receipt)
    return hooked


def check_commit_receipt(receipt, args, repository):
    """Version 4 has no factory commit; version 5 declares its nullable intent."""
    if (factory.repository_identity(str(args.cwd)) != receipt["repository_identity"]
            or factory.git_dir(str(args.cwd)) != args.worktree_git_dir):
        raise factory.FactoryError("Assignment worktree binding differs at collection; restore its original repository and Git directory")
    if args.write and factory.git_commit(str(args.cwd), "HEAD") != receipt["candidate"]:
        raise factory.FactoryError("Writer candidate differs from the assigned worktree HEAD; inspect its retained checkout before collection")
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
            or set(commit) != {"state", "parent", "message", "staged_tree", "candidate"}
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


async def execute(args, *, spawn=asyncio.create_subprocess_exec, clock=time.time, timer=time.monotonic,
                  git_scope=isolated_git, scan=scan_staged):
    if args.receipt.exists() or args.events.exists():
        raise factory.FactoryError("Native attempt already has artifacts; collect or inspect it, never relaunch it blindly")
    try:
        binary = args.codex.resolve(strict=True)
    except OSError:
        raise factory.FactoryError(f"Cannot access native CLI {str(args.codex)!r}; inspect/restore the saved executable before authorizing another attempt") from None
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise factory.FactoryError(f"Native CLI {str(binary)!r} is not executable; inspect/restore the saved executable before authorizing another attempt")
    if type(args.seconds) is not int or not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS:
        raise factory.FactoryError(f"Supply a positive native execution allowance of at most {factory.MAX_NATIVE_SECONDS} seconds before creating an attempt")
    factory.check_commit_message(args.commit_message, write=args.write)
    if os.environ.get("CODEX_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise factory.FactoryError("Remove API-key overrides from this worker environment and use native ChatGPT sign-in")
    try:
        instructions = args.brief.read_text()
    except (OSError, UnicodeDecodeError):
        raise factory.FactoryError(f"Cannot read original brief {str(args.brief)!r}; inspect/restore the saved assignment input before authorizing another attempt") from None
    digest = hashlib.sha256(instructions.encode()).hexdigest()
    if digest != args.brief_sha256:
        raise factory.FactoryError("Assignment brief changed; inspect the original task before launching")
    if args.write:
        factory.check_role_write(args.role)
    if args.role is not None and instructions.split("\n", 1)[0] != "# Assignment: " + args.role:
        raise factory.FactoryError("Worker role differs from the bound assignment brief; inspect the original request")
    try:
        identity = factory.decode_json(args.repo_identity)
    except (ValueError, TypeError):
        raise factory.FactoryError("Cannot decode repository identity; inspect the original assignment before another attempt") from None
    if not factory.valid_repository_identity(identity):
        raise factory.FactoryError("Invalid repository identity; inspect the original assignment before another attempt")
    if not args.worktree_git_dir.is_absolute():
        raise factory.FactoryError("Restore the saved absolute worktree Git directory before launching")
    factory.check_checkout(str(args.repo), str(args.cwd), args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    if args.write and any(binary.is_relative_to(root) for root in (
            Path(factory.git_worktree(args.cwd)), factory.git_common_dir(args.cwd), args.worktree_git_dir)):
        raise factory.FactoryError("Use a trusted Codex installation outside the writer checkout and Git metadata")
    if args.write:
        args._git_python = trusted_git_runtime(argparse.Namespace(**{**vars(args), "codex": binary}))
    if args.commit_message is not None:
        check_git_operation(args)
    if args.commit_message is not None and scan is scan_staged:
        args._gitleaks = scanner_binary(args)
    factory.check_checkout(".", ".", args.candidate, expected_identity=identity,
                           expected_git_dir=args.worktree_git_dir)
    schema = args.receipt.with_suffix(".schema.json")
    # The receipt is this native effect's before-start intent, separate from the Rex launch.
    receipt = dict(schema_version=5, kind="codex-execution", job=args.job, state="starting",
                   inputs=dict(candidate=args.candidate, requirements=args.requirements),
                   repository_identity=identity,
                   worktree_git_dir=str(args.worktree_git_dir),
                   brief_sha256=digest, cwd=str(args.cwd), requested_model=args.model,
                   requested_effort=args.effort, binary=str(binary), writer=args.write,
                   created_at=clock(), finished_at=None,
                   elapsed_seconds=None, exit_code=None, thread_id=None, report=None, usage=None,
                   candidate=None, events=str(args.events), error=None, commit=None)
    # Claim the effect before writing any artifact; a concurrent caller cannot replace this receipt.
    fd = os.open(args.events, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        factory.save(schema, outcome_schema())
        factory.save(args.receipt, receipt)
    except OSError:
        os.close(fd)
        raise
    started, process = timer(), None
    events = []
    try:
        with os.fdopen(fd, "w") as stream:
            if args.commit_message is not None:
                instructions += "\nFactory commit mode: edit and test only; do not stage or commit Git changes. The factory commits after a completed report. Keep temporary outputs outside this checkout.\n"
            try:
                process = await spawn(str(binary), "--no-daemon", "exec", "--disable", "multi_agent", "--disable", "multi_agent_v2",
                                      "--json", "--color", "never",
                                      "--output-schema", str(schema), "--model", args.model,
                                      "--sandbox", "workspace-write" if args.write else "read-only",
                                      "-c", "model_reasoning_effort=" + json.dumps(args.effort),
                                      "--cd", ".", instructions,
                                      env=native_environment(),
                                      limit=NATIVE_LINE_LIMIT,
                                      stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE)
            except OSError:
                raise factory.FactoryError("Cannot start the saved native CLI; restore executable access/local process resources and inspect the retained attempt before authorizing another ID") from None

            async def consume():
                while True:
                    try:
                        line = await process.stdout.readline()
                    except ValueError:
                        raise factory.FactoryError("Native event exceeded the 1 MiB line limit; inspect the existing Rex pane and retained stream, then reduce command output or update the adapter before authorizing another attempt") from None
                    if not line:
                        break
                    text = line.decode("utf-8", errors="surrogateescape")
                    stream.write(json.dumps(dict(schema_version=1, kind="codex-event-line", data=text)) + "\n")
                    stream.flush()
                    try:
                        line.decode("utf-8")
                        event = factory.decode_json(text, object_pairs_hook=native_object)
                    except (UnicodeDecodeError, ValueError):
                        raise factory.FactoryError("Malformed native event; inspect the retained stream and update the adapter before another attempt") from None
                    print(text, end="", flush=True)
                    if not isinstance(event, dict):
                        raise factory.FactoryError("Native output is not an event object; inspect the retained stream")
                    events.append(event)
                    if (event.get("type") == "thread.started" and receipt["thread_id"] is None
                            and isinstance(event.get("thread_id"), str) and event["thread_id"]):
                        receipt["thread_id"] = event["thread_id"]
                return await process.wait()

            receipt["exit_code"] = await asyncio.wait_for(consume(), timeout=args.seconds)
            os.fsync(stream.fileno())
        if receipt["exit_code"] != 0:
            raise factory.FactoryError("Native Codex exited unsuccessfully; inspect the retained events and Rex pane")
        receipt.update(parse_events(events))
        committing = args.commit_message is not None and receipt["report"]["outcome"] == "completed"
        if committing:
            receipt["commit"] = dict(state="preparing", parent=args.candidate, message=args.commit_message,
                                     staged_tree=None, candidate=None)
            factory.save(args.receipt, receipt)
        git_args = argparse.Namespace(**{**vars(args), "codex": binary})
        with git_scope(git_args, identity) if args.commit_message is not None else nullcontext():
            if committing:
                commit_changes(git_args, receipt, git_scope=git_scope, scan=scan)
            candidate = factory.git_commit(".", "HEAD")
            factory.check_checkout(".", ".", candidate, expected_identity=identity,
                                   expected_git_dir=args.worktree_git_dir)
        if not args.write and candidate != args.candidate:
            raise factory.FactoryError("Read-only assignment's candidate changed; do not reuse its verification")
        receipt.update(candidate=candidate, state="completed")
    except TimeoutError:
        receipt.update(state="failed", error="Native execution allowance expired; inspect the existing attempt")
    except OSError:
        receipt.update(state="failed", error="Native process/event-stream I/O failed; inspect the retained stream, storage and Rex pane, restore access/space and reconcile this attempt before another launch")
    except (factory.FactoryError, ValueError, KeyError, TypeError) as exc:
        receipt.update(state="failed", error=str(exc))
    finally:
        if process is not None:
            await stop_child(process)
            receipt["exit_code"] = process.returncode
        receipt.update(finished_at=clock(), elapsed_seconds=timer() - started)
        factory.save(args.receipt, receipt)
    return receipt


def parser():
    command = argparse.ArgumentParser(description=__doc__)
    for field in ("codex", "repo", "cwd", "brief", "events", "receipt", "worktree-git-dir"):
        command.add_argument("--" + field, type=Path, required=True)
    for field in ("job", "candidate", "requirements", "brief-sha256", "model", "effort", "repo-identity"):
        command.add_argument("--" + field, required=True)
    command.add_argument("--role", help="Owner-selected role; required for writer execution")
    command.add_argument("--write", action="store_true")
    command.add_argument("--commit-message", help="With --write, let the factory stage/commit a completed result using this message")
    command.add_argument("--seconds", type=int, default=600, help="Initial execution allowance; no automatic retry")
    return command


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        receipt = asyncio.run(execute(args))
        if receipt["state"] != "completed":
            print("codex-worker: " + receipt["error"], file=sys.stderr)
            return 1
    except OSError:
        print("codex-worker: Cannot access execution artifacts; restore storage access/space and inspect the retained pane/attempt before retrying", file=sys.stderr)
        return 1
    except (factory.FactoryError, ValueError) as exc:
        print("codex-worker: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
