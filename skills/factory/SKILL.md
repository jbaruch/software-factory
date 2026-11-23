---
name: factory
description: "Use when the user or project explicitly requests Software Factory work. Coordinate selected assignments, launch one specialist, continue or inspect a factory task, or record factory delivery."
---

# Factory

This skill is an action router — pick the step that matches the user's intent and execute only that step. Do not run other steps; do not parallelize.

Use the smallest factory operation that finishes the request. Follow the project's existing test, review, and release requirements. Add a model call only when its result changes the outcome.

## Step 1 — Coordinate assignments

Reuse the existing task and selected inputs for continuation. Admit a task and provision an isolated writer worktree when the work needs them. Select only the roles the task needs from the [role catalog](references/roles.json).

Use the [ready-plan contract](references/ready-plan.md) for inputs, provider routing, and result states. Run the plan with the controller command in the [command reference](references/commands.md). Inspect its JSON result and reconcile partial effects before retrying. Keep active work within the selected limits. Leave spare capacity idle. Finish here.

## Step 2 — Launch one assignment

Reuse an admitted task and its selected inputs. For a new task, use `admit` and `inputs` from the [command reference](references/commands.md).

Use the matching native start command from that reference. Supply the task, role, exact inputs, checkout, local Rex endpoint, and eligible model. Collect the result when requested. Finish here.

## Step 3 — Inspect a task

Use `show` for the saved record and `status` for a selected assignment's live process. Use the [command reference](references/commands.md) for inputs and output. Report current evidence, partial effects, and blockers. Finish here.

## Step 4 — Record factory delivery

Use the [delivery reference](references/delivery.md) when the task requires a factory delivery record. Use the repository's regular checks and review for ordinary delivery. Finish here.
