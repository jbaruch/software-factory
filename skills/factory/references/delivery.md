# Delivery

Delivery starts after independent verifiers approve the task's current candidate. `SKILL_DIR` is the `factory` skill's directory, as in the skill. The factory never pushes; the line's foreman pushes the PR's branch in step 1, and specialists have no push or merge authority. `RELEASE-SKILL` is coding-policy's release skill where ACR or Tessl installed it: `.codex/skills/acr__jbaruch__coding-policy__release` or `.tessl/plugins/jbaruch/coding-policy/skills/release`. The full contract is in [docs/integration.md](https://github.com/jbaruch/software-factory/blob/main/docs/integration.md).

1. Open the PR from the task's provisioned worktree. This step only opens it; the release skill runs end to end, merge included, so it comes after the release judgment:

   ```sh
   git -c core.hooksPath=/dev/null push origin CANDIDATE:refs/heads/factory/TASK/ID
   gh pr create --repo OWNER/REPO --head factory/TASK/ID --title 'TYPE(SCOPE): SUMMARY' --body-file /absolute/body.md
   ```

   `CANDIDATE` is the task's selected candidate (`inputs.candidate` in `factory.py show --task TASK`), the commit the verifiers approved; `OWNER/REPO` is the admitted repository's GitHub origin. The body carries the issue lines the repository's release policy asks for, such as `Closes #N`. Once a correction or a `rebase-worktree` replay is the selected candidate, push it the same way to move the PR. A replay is no fast-forward, so add `--force-with-lease=refs/heads/factory/TASK/ID:PR_HEAD`, the head the PR shows. The release judgment refuses a PR whose head isn't the selected candidate or whose repository isn't the admitted origin.

2. Once the PR's reviewers have posted on its current head, run the release judgment. Check with one snapshot, `bash RELEASE-SKILL/poll-pr-reviews.sh OWNER REPO NUMBER`, never the blocking watcher: this stage reads the PR without merging it. If a lane has no current-head verdict yet, take up other work and check again on a later round. The snapshot's contract is in the header of `RELEASE-SKILL/poll-pr-reviews.sh`.

   The release judgment is a read-only `release` assignment over the complete PR feedback, on the strongest model available on any harness, at that model's maximum effort. Claude and Antigravity need `max`, Grok and Codex the top effort of their model catalogs. Launch refuses a lower effort, except on Grok, whose worker fails at start instead. For example, on Claude:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-start --task TASK --id release --role release --purpose work \
     --goal 'Judge the PR for release' --cwd /absolute/clean/checkout --claude /absolute/claude \
     --model MODEL --effort max --release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-collect --task TASK --id release
   ```

   The judgment sees the task's specialist roster, with the candidate each specialist judged, and returns `findings` naming any specialist the change's effects call for that never approved code still in the change. An approval of an earlier candidate covers an effect whose code later replays and corrections left as it was. Run each named specialist, then judge again under a new ID; a missing specialist is not an escalation.

   `codex-start`, `grok-start` and `antigravity-start` take the same `--release-pr`, `--gh` and `--release-scripts` flags.

3. Merge, naming every required approval with `--after`. Where the repository meets the [prerequisites](#prerequisites), integrate:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" integrate --task TASK --id integration \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   ```

   Otherwise, from the task's worktree, merge with `Skill(skill: "release", args: "existing-pr PR_URL")`, then confirm that it merged what the release judgment approved:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" delivered --task TASK \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   ```

   `delivered` re-checks the approvals as `integrate` does, refuses until the PR is merged, and refuses a merged head the release judgment never saw, which goes to the operator. It records `TASK.RELEASE_ID.delivered.json` beside the task. This is the factory's delivery step for those repositories, not a hand step.

If the branch conflicts with a moved base before step 1 or during review, run `python3 -B "$SKILL_DIR/runtime/factory.py" rebase-worktree --task TASK --onto BASE-COMMIT`, then a writer (`purpose: work`) whose goal applies the printed patch with `git apply --3way`, resolves the conflicts and re-runs the gate, and verify the produced commit with `--from-writer`: `integration`, the approvals you will name with `--after`, and each effect specialist whose code the resolution changed, then move the open PR to it (step 1). Writers cannot make merge commits, so the factory refreshes by replaying.

4. After the merge and any deploy, run the final acceptance judgment, before cleanup and before selecting other inputs: a new read-only `release` assignment, as in step 2 but with `--delivery ID` in place of `--release-pr`, collected the same way; its `--cwd` is the task's worktree, still at the candidate. `ID` is the integration's ID after `integrate`, or the release judgment's ID that `delivered` confirmed. Its brief freezes that delivery, and it judges the admitted acceptance against the merged and deployed result, including what the verifiers listed as not yet verifiable. Confirm it by repeating the step 3 command with `--acceptance FINAL_ID` added, which prints the receipt with an `acceptance` object and refuses a judgment that isn't approved, assessed a different delivery, or comes from a writer's session. Findings become new work for the task.

5. Then remove the task's provisioned worktree with `python3 -B "$SKILL_DIR/runtime/factory.py" release-worktree --task TASK`. It refuses while the worktree is dirty or an assignment there may still run, and keeps the branch: deleting branches belongs to the repository's release policy, so run the release skill's worktree prune afterwards, which removes a merged branch only after checking it has no unmerged commits.

## Prerequisites

`integrate` holds with an operator prerequisite, and never changes repository settings, unless all of these hold:

- Every `--after` approval is `approved`, current for the task's exact candidate and requirements, read-only and collected from its original native evidence. Approval contexts are distinct and independent of every writer that contributed.
- The release skill is installed, ignored and untracked, at the admitted repository's ACR (`.codex/skills/acr__jbaruch__coding-policy__release`) or Tessl (`.tessl/plugins/jbaruch/coding-policy/skills/release`) location.
- The repository's `checks.yml` defines the required `verify` job, and the canonical review watcher reports the PR ready, including its Claude review.
- The target branch has classic branch protection: strict required status checks, administrator enforcement, no force pushes or deletion, no bypass allowances, and required `verify` bound to the GitHub Actions app (ID 15368).

A repository that doesn't meet them merges with `Skill(skill: "release", args: "existing-pr PR_URL")` and confirms with `delivered` (step 3).
