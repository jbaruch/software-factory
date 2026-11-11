"""Run an explicit native assignment plan (Codex or Claude) using the existing factory task store."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True  # Installed copies stay free of generated files.
import time

import codex_capacity
import factory
import native_codex

POLL_SECONDS = 5
CAPACITY_MAX_AGE = 60
PROBE_TIMEOUT = 30
RESERVED_ROLES = {"qa", "code-review", "investigator", "judge", "standby-reasoner"}
JOB_FIELDS = {"task", "id", "role", "goal", "cwd", "candidate", "requirements",
              "model", "effort", "seconds", "purpose", "write", "commit_message", "after"}
LIMIT_FIELDS = {"workers", "tasks", "reserve_workers", "minimum_remaining_percent"}
HARNESSES = factory.NATIVE_HARNESSES  # A job's optional "harness"; Codex when omitted.
# Optional per job: the harness, a turn budget (Claude/Grok) and a review base (not Codex).
OPTIONAL_FIELDS = {"harness", "turns", "base"}
SLUG = r"[a-z0-9][a-z0-9-]{0,63}"
# Process states that will never change again; `lost` is terminal without an exit status.
TERMINAL = {"exited", "failed", "lost"}


def validate_plan(value):
    """Schema 1 is operator input, never a second launch/result ledger."""
    if (not isinstance(value, dict) or set(value) != {"schema_version", "limits", "jobs"}
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise factory.FactoryError("Use ready-plan schema 1 with limits and jobs; update the runner for other formats")
    limits, jobs = value["limits"], value["jobs"]
    if (not isinstance(limits, dict) or set(limits) != LIMIT_FIELDS
            or any(type(limits[k]) is not int or not 1 <= limits[k] <= 1000 for k in ("workers", "tasks"))
            or type(limits["reserve_workers"]) is not int
            or not 0 <= limits["reserve_workers"] < limits["workers"]
            or type(limits["minimum_remaining_percent"]) not in (int, float)
            or not math.isfinite(limits["minimum_remaining_percent"])
            or not 0 < limits["minimum_remaining_percent"] < 100):
        raise factory.FactoryError("Set positive worker/task limits, a smaller verification reserve and headroom strictly between 0 and 100")
    if not isinstance(jobs, list) or len(jobs) > 1000:
        raise factory.FactoryError("Supply at most 1000 explicit assignment jobs")
    roles = {item["id"] for item in factory.decode_json(factory.ROLES_PATH.read_text())["roles"]}
    planned = {(row["task"], row["id"]) for row in jobs if isinstance(row, dict)
               and isinstance(row.get("task"), str) and isinstance(row.get("id"), str)}
    seen, inputs = set(), {}
    for job in jobs:
        if not isinstance(job, dict):
            raise factory.FactoryError("Each job must be an object of the documented ready-plan assignment fields")
        try:
            check_job(job, roles=roles, planned=planned, seen=seen, inputs=inputs)
        except factory.FactoryError as exc:
            # In a many-job plan the operator needs to know which job to repair.
            raise factory.FactoryError(f"Job {job.get('task')}/{job.get('id')}: {exc}") from None
    return {**value, "limits": dict(limits),
            "jobs": [{**job, "harness": job.get("harness", "codex"), "cwd": str(Path(job["cwd"]).resolve())} for job in jobs]}


def check_job(job, *, roles, planned, seen, inputs):
    """Validate one plan job against the earlier jobs; records it in seen and inputs."""
    missing, unexpected = sorted(JOB_FIELDS - set(job)), sorted(set(job) - JOB_FIELDS - OPTIONAL_FIELDS)
    problems = ([f"missing {', '.join(missing)}"] if missing else []) + (
        [f"unexpected {', '.join(unexpected)}"] if unexpected else []) + (
        [] if job.get("harness", "codex") in HARNESSES else ["harness must be one of " + ", ".join(HARNESSES)])
    if problems:
        raise factory.FactoryError("; ".join(problems) + ". Read-only jobs still set commit_message to null.")
    # An Antigravity job's null effort runs the model without --effort; some of its models refuse the flag.
    optional = {"effort"} if job.get("harness") == "antigravity" and job.get("effort") is None else set()
    if any(not isinstance(job[k], str) or not job[k].strip()
           for k in JOB_FIELDS - {"seconds", "write", "commit_message", "after"} - optional):
        raise factory.FactoryError("Supply nonempty assignment strings; credentials never belong in a plan")
    key = (job["task"], job["id"])
    if (not re.fullmatch(SLUG, job["task"]) or not re.fullmatch(SLUG, job["id"])
            or key in seen or job["role"] not in roles
            or not Path(job["cwd"]).is_absolute()
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", job["candidate"])
            or type(job["seconds"]) is not int or not 1 <= job["seconds"] <= factory.MAX_NATIVE_SECONDS
            or type(job["write"]) is not bool
            or job["purpose"] not in {"work", "correction", "investigation"}
            or (job["role"] == "investigator") != (job["purpose"] == "investigation")):
        raise factory.FactoryError("Repair job identity, role, candidate, absolute cwd, purpose, writer flag or execution allowance")
    factory.check_commit_message(job["commit_message"], write=job["write"])
    if job.get("harness") == "claude":
        import native_claude
        if job["effort"] not in native_claude.EFFORTS or (job["write"] and job["commit_message"] is None):
            raise factory.FactoryError("Claude jobs need a supported effort, and Claude writers need a commit message")
    if job.get("harness") == "antigravity":
        import native_antigravity
        if job["effort"] is not None and job["effort"] not in native_antigravity.EFFORTS:
            raise factory.FactoryError("Antigravity jobs need a supported effort, or null for a model that refuses one")
    if job.get("harness") in ("grok", "antigravity") and job["write"] and job["commit_message"] is None:
        raise factory.FactoryError("Grok and Antigravity writers need a commit message")
    if "turns" in job and (job.get("harness") not in ("claude", "grok")
                           or type(job["turns"]) is not int or not 1 <= job["turns"] <= 1000):
        raise factory.FactoryError("Only Claude and Grok jobs take turns, from 1 to 1000")
    if job.get("harness", "codex") == "codex":
        if "base" in job:
            raise factory.FactoryError("Codex jobs take no base; review a range on Claude, Grok or Antigravity")
    else:
        from review_evidence import valid_base
        if job["write"] and job.get("base") is not None:
            raise factory.FactoryError("Writers implement from the selected candidate; base is for code review")
        valid_base(job.get("base"), job["role"])
    after = job["after"]
    if (not isinstance(after, list) or any(not isinstance(item, str) or not re.fullmatch(SLUG, item) for item in after)
            or len(set(after)) != len(after)
            or any((job["task"], item) in planned and (job["task"], item) not in seen for item in after)):
        raise factory.FactoryError("Dependencies must name distinct earlier plan jobs or existing assignments in the same task")
    selected = dict(candidate=job["candidate"], requirements=job["requirements"])
    if job["task"] in inputs and inputs[job["task"]] != selected:
        raise factory.FactoryError("One plan must use one candidate/requirements pair per task; select a new phase explicitly after implementation")
    inputs[job["task"]] = selected
    seen.add(key)


def worker_harness(request):
    """The verified native worker behind a request, or None for any other command adapter."""
    return factory.native_harness(request["command"])


def native_request(request):
    return worker_harness(request) is not None


def writer(request):
    # Other command adapters have no verified read-only contract here.
    return not native_request(request) or "--write" in request["command"]


def reserved(request):
    return (request.get("purpose") in {"correction", "investigation"}
            or (request["role"] in RESERVED_ROLES and not writer(request)))


def capacity_reason(snapshot, binary, now, minimum, job):
    """Gate selected requests; metadata never selects a model or accepts work."""
    if (type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1 or snapshot.get("binary") != str(binary)
            or snapshot.get("route") != "codex-native"
            or snapshot.get("account", {}).get("type") != "chatgpt"
            or snapshot.get("ordinaryUsageAllowed") is not True):
        return "Native subscription/ordinary-usage evidence is unavailable; refresh the selected route"
    at = snapshot.get("observed_at")
    if type(at) not in (int, float) or not math.isfinite(at) or not 0 <= now - at <= CAPACITY_MAX_AGE:
        return "Capacity observation is stale or future-dated; refresh before launching"
    pools = snapshot.get("pools")
    if not pools or any(not pool.get("limit_id") for pool in pools):
        return "Native pool identity/capacity is unknown; inspect metadata before launching"
    for pool in pools:
        if pool.get("rate_limit_reached_type") is not None or pool.get("spend_control_reached") is not False:
            return "A native pool is blocked or its spend-control state is unknown; refresh after recovery"
        primary = pool.get("primary")
        if primary is None or primary.get("remaining_percent") is None:
            return "Primary quota is unknown; refresh before launching"
        for window in (primary, pool.get("secondary")):
            if window is not None and (window.get("remaining_percent") is None or window["remaining_percent"] <= minimum):
                return "Observed quota is at the configured reserve; wait for fresh recovered capacity"
    models = snapshot.get("models") or []
    if not any(model["model"] == job["model"] and job["effort"] in (model.get("effort_ids") or []) for model in models):
        return "Selected model/effort is not in the fresh native catalog; repair the explicit plan"
    return None


class Runner:
    def __init__(self, directory, plan, *, codex, rex, server, claude=None, grok=None, antigravity=None,
                 probe=codex_capacity.snapshot,
                 clock=time.time, from_writer=None):
        self.plan = validate_plan(plan)
        self.supplied_plan, self.from_writer = self.plan, from_writer
        if from_writer is not None:
            jobs = self.plan["jobs"]
            if (not isinstance(from_writer, str) or not re.fullmatch(SLUG, from_writer)
                    or len({job["task"] for job in jobs}) != 1
                    or any(job["role"] not in {"qa", "code-review", "test-automation"}
                           or job["write"] or job["commit_message"] is not None
                           or job["id"] == from_writer or from_writer not in job["after"] for job in jobs)):
                raise factory.FactoryError("--from-writer requires exactly one task with read-only independent verification jobs, distinct IDs and explicit writer dependencies")
        self.directory, self.binary = Path(directory).resolve(), Path(codex).resolve()
        self.claude = Path(claude).resolve() if claude else None
        self.grok = Path(grok).resolve() if grok else None
        self.antigravity = Path(antigravity).resolve() if antigravity else None
        if not Path(codex).is_absolute() or not Path(rex).is_absolute() or not server.startswith("unix://"):
            raise factory.FactoryError("Supply absolute Codex/Rex paths and an explicit unix:// Rex endpoint")
        if any(job["harness"] == "claude" for job in self.plan["jobs"]) and (claude is None or not Path(claude).is_absolute()):
            raise factory.FactoryError("Supply the absolute --claude executable for the plan's Claude jobs")
        if any(job["harness"] == "grok" for job in self.plan["jobs"]) and (grok is None or not Path(grok).is_absolute()):
            raise factory.FactoryError("Supply the absolute --grok executable for the plan's Grok jobs")
        if any(job["harness"] == "antigravity" for job in self.plan["jobs"]) and (antigravity is None or not Path(antigravity).is_absolute()):
            raise factory.FactoryError("Supply the absolute --antigravity executable for the plan's Antigravity jobs")
        self.rex, self.server, self.probe, self.clock = str(rex), server, probe, clock
        self.capacity, self.capacity_error, self.probed_at = None, None, None
        policy = factory.decode_json((factory.ROOT / "config/coordination.json").read_text())
        retries = policy.get("policies", {}).get("max_operational_retries")
        if type(retries) is not int or not 0 <= retries <= 1000:
            raise factory.FactoryError("Repair max_operational_retries in the coordination policy before running")
        self.probe_limit, self.probe_failures = 1 + retries, 0
        self.launch_errors = {}
        self.metrics = dict(polls=0, quota_probes=0, launched=0, collected=0, tabs_closed=0)
        self.tidy_issues = []

    def command(self, path, job):
        """The frozen worker command for a plan job on its harness."""
        # Absent turns keep each builder's default, so older plans freeze the same commands.
        optional = {key: job[key] for key in ("turns", "base") if key in job}
        if job["harness"] == "claude":
            return factory.claude_command(path, task=job["task"], assignment_id=job["id"], claude=str(self.claude),
                model=job["model"], effort=job["effort"], cwd=job["cwd"], role=job["role"], seconds=job["seconds"],
                write=job["write"], commit_message=job["commit_message"], sandbox=str(self.binary) if job["write"] else None,
                **optional)
        if job["harness"] == "grok":
            return factory.grok_command(path, task=job["task"], assignment_id=job["id"], grok=str(self.grok),
                model=job["model"], effort=job["effort"], cwd=job["cwd"], role=job["role"], seconds=job["seconds"],
                write=job["write"], commit_message=job["commit_message"], sandbox=str(self.binary) if job["write"] else None,
                **optional)
        if job["harness"] == "antigravity":
            return factory.antigravity_command(path, task=job["task"], assignment_id=job["id"], agy=str(self.antigravity),
                model=job["model"], effort=job["effort"], cwd=job["cwd"], role=job["role"], seconds=job["seconds"],
                write=job["write"], commit_message=job["commit_message"], sandbox=str(self.binary) if job["write"] else None,
                **optional)
        return factory.codex_command(path, task=job["task"], assignment_id=job["id"], codex=str(self.binary),
            model=job["model"], effort=job["effort"], cwd=job["cwd"], write=job["write"], seconds=job["seconds"],
            commit_message=job["commit_message"], role=job["role"])

    def select_writer_candidate(self):
        """Called under the execution lock; derive continuity from original evidence."""
        if self.from_writer is None:
            return
        jobs = self.supplied_plan["jobs"]
        original = dict(candidate=jobs[0]["candidate"], requirements=jobs[0]["requirements"])
        with factory.task_lock(self.directory, jobs[0]["task"]) as path:
            record = factory.load_task(path)
            source = record["assignments"].get(self.from_writer)
            if record["admission"] is None or source is None:
                raise factory.FactoryError("--from-writer needs an admitted task and an existing writer assignment")
            harness = worker_harness(source["request"])
            if (source["state"] != "launched" or harness is None
                    or not writer(source["request"]) or source["result"] is None
                    or source["result"]["outcome"] != "completed"):
                raise factory.FactoryError("--from-writer needs a collected, completed native writer")
            if source["request"].get("inputs") != original:
                raise factory.FactoryError("Verification plan inputs differ from the writer's frozen original pair")
            factory.check_repository(record["admission"])
            rex = factory.connect(record, self.rex, self.server)
            args, receipt, native = factory.evidence_reader(harness)(self.from_writer, record, source,
                observe=lambda: factory.status(rex, record, source))
            if not args.write or args.commit_message is None or receipt.get("commit") is None:
                raise factory.FactoryError("--from-writer needs an explicit validated factory commit")
            expected = dict(inputs=original, candidate=receipt["candidate"], outcome=native["report"]["outcome"],
                artifacts=list(dict.fromkeys([str(args.receipt), *native["report"]["artifacts"]])))
            if source["result"] != expected:
                raise factory.FactoryError("Collected writer result differs from its original native evidence")
            target = dict(candidate=receipt["candidate"], requirements=original["requirements"])
            if record["inputs"] not in (original, target):
                raise factory.FactoryError("Selected task inputs changed; refuse stale or third-party candidate/requirements")
            if record["inputs"] != target:
                if record["hold"] is not None:
                    raise factory.FactoryError("Task is paused: " + record["hold"])
                for identity, assignment in record["assignments"].items():
                    if identity == self.from_writer or assignment["state"] == "abandoned":
                        continue
                    # Even a saved result is no substitute for observing its process.
                    if assignment["state"] == "launching":
                        factory.reconcile(rex, record, assignment)
                    if factory.status(rex, record, assignment)["state"] not in TERMINAL:
                        raise factory.FactoryError("Another assignment is active or unknown; inspect its process before candidate selection: " + identity)
                factory.set_inputs(path, **target)
            self.plan = {**self.supplied_plan, "jobs": [{**job, **target} for job in jobs]}

    def observe(self):
        records, phases, active, issues = {}, {}, [], []
        self.tidy_issues = []
        paths = sorted(p for p in self.directory.glob("*.json") if re.fullmatch(SLUG, p.stem))
        for source in paths:
            try:
                with factory.task_lock(self.directory, source.stem) as path:
                    record = factory.load_task(path)
                    records[source.stem] = record
                    if not record["assignments"]:
                        continue
                    try:
                        rex = factory.connect(record)
                    except factory.FactoryError as exc:
                        for identity, assignment in record["assignments"].items():
                            phases[(source.stem, identity)] = dict(state="unknown", reason=str(exc))
                            active.append((source.stem, assignment["request"]))
                        continue
                    for identity, assignment in record["assignments"].items():
                        key = (source.stem, identity)
                        terminal = False
                        try:
                            if assignment["state"] == "launching":
                                factory.reconcile(rex, record, assignment)
                                factory.save(path, record)
                            observed = factory.status(rex, record, assignment)
                            terminal = observed["state"] in TERMINAL
                            phase = dict(state=observed["state"])
                            if observed["state"] in {"running", "unknown"}:
                                active.append((source.stem, assignment["request"]))
                            if observed["state"] == "lost":
                                phase["reason"] = observed["reason"]
                            if observed["state"] in {"exited", "lost"}:
                                # A lost pane defers to the worker's own completed receipt.
                                harness = worker_harness(assignment["request"])
                                if harness is not None and assignment["result"] is None:
                                    record = factory.collector(harness)(path, identity, rex, record, assignment)
                                    self.metrics["collected"] += 1
                                    assignment = record["assignments"][identity]
                                result = assignment["result"]
                                if result is not None:
                                    phase["state"] = result["outcome"] if factory.result_current(record, result) else "stale"
                            phases[key] = phase
                        except factory.FactoryError as exc:
                            phases[key] = dict(state="blocked" if terminal else "unknown", reason=str(exc))
                            if not terminal and not any(task == source.stem and request is assignment["request"] for task, request in active):
                                active.append((source.stem, assignment["request"]))
                    try:
                        # Finished tabs close after collection; their final status and output stay in the task.
                        self.metrics["tabs_closed"] += len(factory.tidy(path, rex, record)["closed"])
                    except (factory.FactoryError, OSError) as exc:
                        # Tidying never blocks work; it is reported and retried on the next tick.
                        self.tidy_issues.append(dict(task=source.stem, reason=str(exc)))
                    records[source.stem] = record
            except (factory.FactoryError, OSError, ValueError) as exc:
                issues.append(dict(task=source.stem, reason=str(exc)))
        return records, phases, active, issues

    def refresh_capacity(self):
        now = self.clock()
        if self.probe_failures >= self.probe_limit:
            return
        if self.probed_at is not None and 0 <= now - self.probed_at < CAPACITY_MAX_AGE:
            return
        self.metrics["quota_probes"] += 1
        self.probed_at = now
        try:
            self.capacity = asyncio.run(asyncio.wait_for(self.probe(self.binary), PROBE_TIMEOUT))
            self.capacity_error = None
            self.probe_failures = 0
        except codex_capacity.CapacityError as exc:
            self.capacity, self.capacity_error = None, str(exc)
            self.probe_failures = self.probe_limit
        except (native_codex.CodexProbeError, OSError, TimeoutError) as exc:
            self.capacity, self.capacity_error = None, str(exc)
            self.probe_failures += 1
            if self.probe_failures == self.probe_limit:
                self.capacity_error += "; metadata retry allowance exhausted: restore the service before restarting this controller"

    def tick(self) -> dict:
        self.metrics["polls"] += 1
        records, phases, active, issues = self.observe()
        limits = self.plan["limits"]
        jobs = self.plan["jobs"]
        # Starts already in the owner store break ties without a fairness ledger.
        remaining = list(range(len(jobs)))
        while remaining:
            index = min(remaining, key=lambda item: len(records.get(jobs[item]["task"], {}).get("assignments", {})))
            remaining.remove(index)
            job = jobs[index]
            key = (job["task"], job["id"])
            phase = phases.get(key)
            record = records.get(job["task"])
            if record is None or record["admission"] is None:
                reason = next((issue["reason"] for issue in issues if issue["task"] == job["task"]),
                              "Admit this task before executing its plan")
                phases[key] = dict(state="waiting", reason=reason)
                continue
            if record["inputs"] != dict(candidate=job["candidate"], requirements=job["requirements"]):
                phases[key] = dict(state="stale", reason="Selected task inputs differ from the plan; replan explicitly")
                continue
            if key in self.launch_errors:
                phases[key] = dict(state="blocked", reason=self.launch_errors[key])
                continue
            if phase is not None:
                request = record["assignments"][job["id"]]["request"]
                try:
                    expected = self.command(self.directory / (job["task"] + ".json"), job)
                    if (request.get("inputs") != dict(candidate=job["candidate"], requirements=job["requirements"])
                            or any(request[k] != job[k] for k in ("role", "goal", "cwd", "purpose"))
                            or request.get("dependencies", []) != sorted(job["after"]) or request["command"] != expected):
                        phases[key] = dict(state="conflict", reason="Plan differs from the saved immutable assignment; use a new explicit ID")
                except (factory.FactoryError, OSError) as exc:
                    phases[key] = dict(state="conflict", reason=str(exc))
                continue
            reason = None
            if record["hold"] is not None:
                reason = "Task is paused: " + record["hold"]
            elif any(dependency not in record["assignments"] and not any(
                    row["task"] == job["task"] and row["id"] == dependency for row in jobs) for dependency in job["after"]):
                phases[key] = dict(state="blocked", reason="Dependency is absent from both the plan and task history; repair the explicit plan")
                continue
            elif any(phases.get((job["task"], dependency), {}).get("state") in
                     {"findings", "blocked", "failed", "lost", "abandoned", "stale", "conflict"} for dependency in job["after"]):
                phases[key] = dict(state="blocked", reason="A dependency needs explicit correction, investigation or replanning; no automatic retry")
                continue
            elif any(phases.get((job["task"], dependency), {}).get("state") not in {"completed", "approved"} for dependency in job["after"]):
                reason = "Waiting for successful, current dependency reports and terminal processes"
            elif issues:
                reason = "Task-store occupancy is unavailable; repair the listed state before launching"
            elif len(active) >= limits["workers"]:
                reason = "Worker limit is occupied, including unknown processes"
            elif job["task"] not in {task for task, _ in active} and len({task for task, _ in active}) >= limits["tasks"]:
                reason = "Active task limit is occupied"
            else:
                request = dict(role=job["role"], purpose=job["purpose"], cwd=job["cwd"],
                    command=[sys.executable, "-E", "-s", str(factory.ROOT / (job["harness"] + "_worker.py"))]
                            + (["--write"] if job["write"] else []))
                if not reserved(request) and sum(not reserved(item) for _, item in active) >= limits["workers"] - limits["reserve_workers"]:
                    reason = "Ordinary worker slots are occupied; verification/recovery reserve remains available"
                else:
                    try:
                        checkout = factory.git_dir(job["cwd"])
                    except factory.FactoryError as exc:
                        self.launch_errors[key] = str(exc)
                        phases[key] = dict(state="blocked", reason=str(exc))
                        continue
                    try:
                        if any((job["write"] or writer(item)) and factory.git_dir(item["cwd"]) == checkout
                               for _, item in active):
                            reason = "An active writer/reader owns this checkout; use an independent worktree"
                    except factory.FactoryError as exc:
                        reason = "Worktree occupancy is unavailable: " + str(exc)
            if reason is None and job["harness"] == "codex":
                self.refresh_capacity()
                reason = self.capacity_error or (capacity_reason(self.capacity, self.binary, self.clock(),
                         limits["minimum_remaining_percent"], job) if self.capacity is not None else "Capacity is unavailable")
            if reason is not None:
                phases[key] = dict(state="waiting", reason=reason)
                continue
            try:
                with factory.task_lock(self.directory, job["task"]) as path:
                    current = factory.load_task(path)
                    if current["inputs"] != record["inputs"] or current["hold"] is not None:
                        raise factory.FactoryError("Task inputs/hold changed before launch; inspect current state before resuming")
                    command = self.command(path, job)
                    current, assignment = factory.launch(path, task=job["task"], assignment_id=job["id"],
                        role=job["role"], goal=job["goal"], cwd=job["cwd"], command=command,
                        binary=self.rex, server=self.server, purpose=job["purpose"], dependencies=job["after"])
                    records[job["task"]] = current
                    active.append((job["task"], assignment["request"]))
                    self.metrics["launched"] += 1
                    phases[key] = dict(state="running")
            except (factory.FactoryError, OSError, ValueError) as exc:
                # An intent may already exist. Re-observe it next tick, never retry it.
                try:
                    saved = factory.load_task(self.directory / (job["task"] + ".json"))
                except (factory.FactoryError, OSError, ValueError) as read_error:
                    phases[key] = dict(state="unknown", reason=str(exc))
                    issues.append(dict(task=job["task"], reason=str(read_error)))
                    records.pop(job["task"], None)
                    active.append((job["task"], request))
                    continue
                records[job["task"]] = saved
                if job["id"] in saved["assignments"]:
                    active.append((job["task"], saved["assignments"][job["id"]]["request"]))
                    phases[key] = dict(state="unknown", reason=str(exc))
                else:
                    self.launch_errors[key] = str(exc)
                    phases[key] = dict(state="blocked", reason=str(exc))
        rows = [dict(task=job["task"], id=job["id"], **phases[(job["task"], job["id"])]) for job in jobs]
        complete = all(row["state"] in {"completed", "approved"} for row in rows)
        settled = all(row["state"] in {"completed", "approved", "findings", "blocked", "failed", "lost", "abandoned", "stale", "conflict"} for row in rows)
        return dict(schema_version=1, result="complete" if complete else "needs_action" if settled else "pending",
                    jobs=rows, active_workers=len(active), issues=issues, tidy_issues=list(self.tidy_issues),
                    metrics=dict(self.metrics),
                    capacity=self.capacity, capacity_error=self.capacity_error)


def run(runner, *, seconds, once=False, timer=time.monotonic, sleep=time.sleep) -> dict:
    started = timer()
    with factory.execution_lock(runner.directory):
        runner.select_writer_candidate()
        while True:
            result = runner.tick()
            elapsed = timer() - started
            result["elapsed_seconds"] = elapsed
            if result["result"] != "pending" or once:
                return result
            if elapsed >= seconds:
                result["result"] = "pending_at_budget"
                return result
            sleep(min(POLL_SECONDS, seconds - elapsed))
            elapsed = timer() - started
            if elapsed >= seconds:
                result.update(result="pending_at_budget", elapsed_seconds=elapsed)
                return result


def main(argv=None):
    factory.relocate("ready_runner.py", sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=factory.default_state_dir())
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--from-writer", metavar="ASSIGNMENT_ID")
    parser.add_argument("--codex", required=True, help="Native Codex CLI; also sandboxes every writer's factory commit")
    parser.add_argument("--claude", help="Native Claude Code CLI; required when the plan has Claude jobs")
    parser.add_argument("--grok", help="Native Grok CLI; required when the plan has Grok jobs")
    parser.add_argument("--antigravity", help="Native Antigravity CLI (agy); required when the plan has Antigravity jobs")
    parser.add_argument("--rex", required=True)
    parser.add_argument("--server", required=True)
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.seconds <= factory.MAX_NATIVE_SECONDS:
            raise factory.FactoryError("Use an orchestration budget between 1 and 86400 seconds")
        plan = validate_plan(factory.decode_json(args.plan.read_text()))
        runner = Runner(args.state_dir, plan, codex=args.codex, claude=args.claude, grok=args.grok, antigravity=args.antigravity, rex=args.rex, server=args.server,
                        from_writer=args.from_writer)
        result = run(runner, seconds=args.seconds, once=args.once)
        print(json.dumps(result, indent=2, allow_nan=False))
        return 0 if result["result"] == "complete" else 1
    except (factory.FactoryError, native_codex.CodexProbeError, OSError, ValueError, TimeoutError) as exc:
        print("ready-runner: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
