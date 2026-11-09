"""Git revision evidence embedded in the factory's existing frozen native specialist brief."""
import json
import os
import re
import subprocess

import factory

MARKER = "\n## Factory revision evidence\n\n"


def valid_base(base, role):
    if base is None and role != "code-review":
        return
    if not isinstance(base, str) or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", base):
        raise factory.FactoryError("Supply --base with a full existing commit ID for a native specialist code review; the factory never guesses the review range")


def git_bytes(cwd, *arguments):
    environment = factory.git_environment()
    environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                       GIT_CONFIG_NOSYSTEM="1", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    try:
        result = subprocess.run([factory.git_binary(), "--no-pager", "--no-replace-objects", "-C", factory.git_worktree(cwd),
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull, *arguments],
            env=environment, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise factory.FactoryError("Cannot gather native specialist revision evidence; restore local Git and object access before launching") from None
    if result.returncode:
        raise factory.FactoryError("Cannot gather native specialist revision evidence; fetch the exact base/candidate objects before launching")
    return result.stdout


def render(cwd, *, candidate, requirements, base, role):
    valid_base(base, role)
    if not factory.valid_inputs(dict(candidate=candidate, requirements=requirements)):
        raise factory.FactoryError("Select a full candidate commit ID and requirements revision before gathering native specialist evidence")
    try:
        paths = git_bytes(cwd, "ls-tree", "-rz", "--name-only", candidate, "--").decode("utf-8")
        evidence: dict = dict(schema_version=1, candidate=candidate, requirements=requirements,
                        base=base, paths=paths.split("\0")[:-1], changed_paths=None, diff=None)
        if base is not None:
            resolved = git_bytes(cwd, "rev-parse", "--verify", "--end-of-options", base + "^{commit}").decode("ascii").strip()
            if resolved != base:
                raise factory.FactoryError("Native specialist review base is not the selected commit; supply its full commit ID before launching")
            options = ("diff", "--no-ext-diff", "--no-textconv", "--no-renames")
            changed = git_bytes(cwd, *options, "--name-only", "-z", base, candidate, "--").decode("utf-8")
            evidence.update(changed_paths=changed.split("\0")[:-1],
                            diff=git_bytes(cwd, *options, "--binary", "--full-index", base, candidate, "--").decode("utf-8"))
    except UnicodeError:
        raise factory.FactoryError("Native specialist revision evidence is not UTF-8; select a route that can inspect the original binary/filename encoding") from None
    return MARKER + json.dumps(evidence, ensure_ascii=True, allow_nan=False) + "\n"


def validate(instructions, *, candidate, requirements, base, role):
    valid_base(base, role)
    head, marker, body = instructions.rpartition(MARKER)
    if not marker and base is None and role != "code-review":
        return  # Historical non-review briefs remain unchanged.
    try:
        value = factory.decode_json(body)
    except ValueError:
        raise factory.FactoryError("Native specialist revision evidence is damaged; restore the original frozen brief without relaunching") from None
    if (not head or not isinstance(value, dict) or type(value.get("schema_version")) is not int
            or value["schema_version"] != 1 or value.get("candidate") != candidate
            or value.get("requirements") != requirements or "base" not in value or value["base"] != base
            or not isinstance(value.get("paths"), list)
            or any(not isinstance(path, str) or not path for path in value["paths"])
            or (base is None and (value.get("changed_paths") is not None or value.get("diff") is not None))
            or (base is not None and (not isinstance(value.get("changed_paths"), list)
                or any(not isinstance(path, str) or not path for path in value["changed_paths"])
                or not isinstance(value.get("diff"), str)))):
        raise factory.FactoryError("Native specialist revision evidence differs from the frozen review range; inspect the original brief and assignment")
