# Dashboard contract

Status: full application/UI acceptance contract. The [implementation inventory](implementation.md) tracks current coverage of the views, controls and measurements below.

## Views

- Delivery board: goal, stage, dependencies, candidate revision, current blockers, next owner and reason work can/cannot advance.
- Team: specialist assignments, native harness, model, effort, subscription route, live Rex pane links and last observation time.
- Capacity: verified allowance-sharing pools, available and reserved capacity, reset windows, usage freshness, unavailable/unknown accounts and eligible queued work.
- Decision inbox: owner, failed condition, attempts, original evidence, supported options and exact decision required. Separate operator, standby reasoner, investigator and judge queues.
- Task detail: original artifacts, findings, requirements, current validations and generated event/decision timeline. Agent-authored status reports are not required.
- Efficiency: occupancy, productive utilization, idle reasons, waits, overhead, rework and delivered outcomes, with task/provider/model/specialty drilldowns and comparable time windows.
- Improvement: current versus previous routing/prompt/workflow versions, observed quality/latency/consumption, exploration allocation, misroutes, sampled audits and rollback status.

Controls: prioritize, pause/resume, cancel, reassign and act on a concrete decision request, constrained by the current user's authority. Actions need traceable results and idempotent submission; refreshing a page must not repeat an effect.

The implemented Decision inbox groups operator, standby-reasoner, investigator
and judge proposals from existing owners. Task conditions are deterministic:
holds, current blocked/findings reports and exhausted admitted allowances. Finding
recipients and substantive options remain operator planning when unknown. Original
shadow proposals stay separate from recorded current native answers and accepted
outcomes. Generic Jev evidence can be unbound; its revision alone establishes no
freshness or task identity. Judge availability requires the original exact current
completed investigation and bounded dispute. An original blocked investigation
never becomes completed through later evidence. Missing or unavailable sources
never mean an empty/all-resolved inbox.

The only added budget control is an explicit human approval of exactly one
correction/investigation attempt, with a reason and observed limit. It uses the
task owner's existing lock/history/replay semantics, preserves holds and starts
no work. See [operator view](operator-view.md) for authentication and schema details.

Team distinguishes requested native identity from observed model and runtime
readiness. Show original assignment inputs/result freshness, saved Rex
host/session/window/block identity and the actual process read time separately
from page collection time. Supported owner-parsed worker commands supply request
metadata; generic/unsupported/damaged commands remain unknown. The pane link is
an authenticated relative read-only dashboard detail that revalidates the saved
scope before process/capture reads. Unavailable identities remain actionable and
unknown; partial output is explicitly marked. See the [operator view contract](operator-view.md).

## Definitions

Use timestamped, mutually exclusive activity intervals per worker/resource: useful execution, coordination, rework, idle, or unavailable. Concurrent agents contribute separate worker-seconds. Do not add their time and call it task wall-clock latency.

For an observation window:

| Metric | Definition |
| --- | --- |
| Available worker time | Useful + coordination + rework + idle worker-seconds |
| Occupied time | Useful + coordination + rework worker-seconds |
| Occupancy | Occupied / available worker time |
| Productive utilization | Useful / available worker time |
| Coordination share | Coordination / occupied time |
| Rework share | Rework / occupied time |
| Queue wait | Work eligible to start -> actual start, excluding time its dependencies were unmet |
| Cycle time | Admitted task -> required accepted outcome, measured as elapsed wall time |
| Allowance utilization | Verified consumed allowance / verified allowance for the same actual pool and reset window |

Unavailable time is shown separately and excluded from the available-worker denominator. Unknown capacity, missing observations and zero denominators render as unknown/not applicable, never fabricated 0% or 100%. Token usage and allowance consumption can be unavailable even when elapsed time is known. Do not add model tokens, worker-seconds and dollars into one invented efficiency score.

Keep subscription pools distinct from routes. Group shared pools once; overlapping hourly/weekly limits are constraints, not additive capacity. A budget for paid Jev calls differs from an existing subscription's remaining allowance. Display estimated values as estimates with their evidence and freshness.

Coordination includes scripts, Jev/standby routing, briefing, reporting, handovers, supervision, capacity probes, classification and dashboard/instrumentation work. Investigation and reviews are labeled by purpose: engineering investigation/independent verification can be useful work; recovery of coordination machinery and duplicated checking count toward overhead. Retain uncertainty and audit labels rather than asserting all busy time is productive.

Break down idle time by no useful ready work, dependencies, quota, tools/provider outage, protected resources, operator hold and coordination delay. High utilization alone is not a success condition. Report throughput alongside defects, rework and acceptance quality; compare like task cohorts and disclose sample size.

Every chart supports drilldown to underlying task/effect IDs and observation coverage. Statistics are computed from operational events and explicit resource observations. Jev may classify ambiguous activity, with uncertain labels visible and sampled for audit; it must not invent missing usage facts.

## Acceptance examples

A worker window with 60 useful, 20 coordination, 10 rework and 10 idle seconds has 90% occupancy, 60% productive utilization and 22.22% coordination share. An additional 40 unavailable seconds changes neither ratio; it appears separately. Two overlapping 60-second workers contribute 120 worker-seconds, while the wall-clock window is still 60 seconds.
