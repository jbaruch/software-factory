"""Curate native Codex capacity metadata; no eligibility or readiness claim."""
import argparse
import asyncio
import json
import math
from pathlib import Path
import sys

import native_codex


class CapacityError(ValueError):
    """An observation cannot safely supply capacity facts."""


def refuse(field):
    raise CapacityError(f"Invalid or conflicting {field}; inspect native metadata and update the capacity adapter before scheduling")


def obj(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        refuse(field)
    return value


def text(value, field):
    if value is not None and (not isinstance(value, str) or not value.strip()):
        refuse(field)
    return value


def number(value, field, *, integer=False, maximum=None):
    if value is not None:
        if (type(value) not in ((int,) if integer else (int, float))
                or value < 0 or (maximum is not None and value > maximum)):
            refuse(field)
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            refuse(field)
    return value


def window(value, field):
    if value is None:
        return None
    data = obj(value, field)
    used = number(data.get("usedPercent"), field + ".usedPercent", maximum=100)
    return dict(used_percent=used, remaining_percent=None if used is None else 100 - used,
                duration_minutes=number(data.get("windowDurationMins"), field + ".windowDurationMins", integer=True),
                resets_at=number(data.get("resetsAt"), field + ".resetsAt", integer=True))


def curate(observation) -> dict:
    """Return a fresh allowlisted snapshot from one native_codex.probe result.

    Primary and secondary windows constrain the same pool concurrently. Missing
    fields remain None; remaining percent is only the complement of observed use.
    """
    data = obj(observation, "observation")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        refuse("native observation schema_version (expected 1)")
    route = text(data.get("route"), "route")
    if route not in (None, "codex-native"):
        refuse("route")
    binary = text(data.get("binary"), "binary")
    if binary is not None and not Path(binary).is_absolute():
        refuse("binary (expected absolute path)")
    account_response = obj(data.get("account"), "account")
    account = obj(account_response.get("account"), "account.account")
    limits = obj(data.get("rate_limits"), "rate_limits")
    allowed = limits.get("ordinaryUsageAllowed")
    if allowed is not None and type(allowed) is not bool:
        refuse("rate_limits.ordinaryUsageAllowed")
    plans = []
    for source, label in ((account, "account.account"), (account_response, "account"),
                          (limits, "rate_limits")):
        plan = text(source.get("planType"), label + ".planType")
        if plan is not None:
            plans.append(plan)
    pools, originals = [], {}

    def add_pool(raw, key=None):
        if not isinstance(raw, dict):
            refuse("rate limit snapshot")
        identity = text(raw.get("limitId"), "rate limit limitId")
        if key is not None:
            text(key, "rateLimitsByLimitId key")
            if identity is not None and identity != key:
                refuse("rateLimitsByLimitId key/limitId")
            identity = key
        plan = text(raw.get("planType"), "rate limit planType")
        if plan is not None:
            plans.append(plan)
        pool = dict(limit_id=identity, primary=window(raw.get("primary"), "primary"),
                    secondary=window(raw.get("secondary"), "secondary"))
        if identity in originals:
            if originals[identity] != pool:
                refuse("duplicate rate limit snapshot")
            return
        originals[identity] = pool
        pools.append(pool)

    default = limits.get("rateLimits")
    if default is not None:
        add_pool(default)
    by_id = limits.get("rateLimitsByLimitId")
    if by_id is not None:
        if not isinstance(by_id, dict):
            refuse("rateLimitsByLimitId")
        for key, raw in by_id.items():
            add_pool(raw, key)
    if len(set(plans)) > 1:
        refuse("account planType")
    models = data.get("models")
    curated_models = None
    if models is not None:
        if not isinstance(models, list):
            refuse("models")
        curated_models, seen = [], set()
        for raw in models:
            if not isinstance(raw, dict):
                refuse("model entry")
            model = text(raw.get("model"), "model.model")
            if model is None or model in seen:
                refuse("missing or duplicate model.model")
            seen.add(model)
            efforts = raw.get("supportedReasoningEfforts")
            ids = None
            if efforts is not None:
                if not isinstance(efforts, list):
                    refuse("supportedReasoningEfforts")
                ids = []
                for effort in efforts:
                    if not isinstance(effort, dict):
                        refuse("supportedReasoningEfforts entry")
                    identity = text(effort.get("reasoningEffort"), "reasoningEffort")
                    if identity is None or identity in ids:
                        refuse("missing or duplicate reasoningEffort")
                    ids.append(identity)
            default_effort = text(raw.get("defaultReasoningEffort"), "defaultReasoningEffort")
            if ids is not None and default_effort is not None and default_effort not in ids:
                refuse("defaultReasoningEffort absent from supportedReasoningEfforts")
            curated_models.append(dict(model=model, id=text(raw.get("id"), "model.id"),
                                       effort_ids=ids, default_effort_id=default_effort))
    return dict(schema_version=1, observed_at=number(data.get("observed_at"), "observed_at"),
                binary=binary, route=route,
                account=dict(type=text(account.get("type"), "account.account.type"),
                             plan=plans[0] if plans else None),
                ordinaryUsageAllowed=allowed,
                pools=pools if default is not None or by_id is not None else None,
                models=curated_models)


async def snapshot(binary) -> dict:
    """Probe once, without retries, and curate without saving local state."""
    if not Path(binary).is_absolute():
        raise CapacityError("Supply an absolute native Codex executable path via --codex")
    return curate(await native_codex.probe(binary))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        print(json.dumps(asyncio.run(snapshot(args.codex)), indent=2, allow_nan=False))
    except (CapacityError, native_codex.CodexProbeError, OSError, TimeoutError) as exc:
        print("codex-capacity: " + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
