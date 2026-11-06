"""Small reference decisions for the design. No I/O or live agent execution."""


def next_owner(*, stage, answer, investigated=False, evidence_current=True,
               objection=False, authority=True, corrections_remaining=True):
    """Route recorded judgments; this does not assess engineering correctness."""
    if not authority:
        return "operator"
    if stage == "coordination":
        return {"uncertain": "standby-reasoner", "clear": "script"}[answer]
    if stage == "correction-limit":
        return "operator" if investigated else "investigator"
    if stage != "post-investigation":
        raise ValueError("Unknown decision stage")
    if not investigated or not evidence_current:
        return "investigator"
    if answer not in {"resolved", "unresolved", "uncertain"}:
        raise ValueError("Unknown investigation resolution")
    if objection or answer in {"unresolved", "uncertain"}:
        return "judge"
    return "script" if corrections_remaining else "operator"


def eligible_routes(routes, *, capability, now):
    """Filter actual observations before sending candidates to Jev."""
    return [route["id"] for route in routes
            if route["ready"]
            and capability in route["capabilities"]
            and route["pool"] is not None
            and route["remaining"] is not None
            and route["remaining"] > route["reserve"]
            and route["observed_at"] is not None
            and 0 <= now - route["observed_at"] <= route["max_age"]]


def select_route(eligible, ranked):
    """Use an externally evaluated ranking; never invent a provider fallback."""
    return next((route for route in ranked if route in eligible), None)


def integration_ready(*, head, required, approvals, objections, contributors):
    """A passed old revision or contributor's own approval cannot release work."""
    if not required or objections:
        return False
    return all(role in approvals and approvals[role]["head"] == head
               and approvals[role]["worker"] not in contributors
               and approvals[role]["outcome"] == "approved"
               for role in required)


def efficiency(*, useful=0, coordination=0, rework=0, idle=0, unavailable=0):
    """Worker-time ratios; unavailable time and unknown denominators stay visible."""
    values = (useful, coordination, rework, idle, unavailable)
    if any(value < 0 for value in values):
        raise ValueError("Activity durations cannot be negative")
    available = useful + coordination + rework + idle
    occupied = useful + coordination + rework
    return {
        "available_seconds": available,
        "unavailable_seconds": unavailable,
        "occupancy": occupied / available if available else None,
        "productive_utilization": useful / available if available else None,
        "coordination_share": coordination / occupied if occupied else None,
        "rework_share": rework / occupied if occupied else None,
    }
