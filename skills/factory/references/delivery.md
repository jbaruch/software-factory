# Delivery

Use this workflow for an explicitly requested or required factory delivery record. Follow the repository's regular release process for other work.

Delivery starts after independent verifiers approve the task's current candidate. `SKILL_DIR` is the absolute realized `factory` skill directory. The factory never pushes; the line's foreman pushes the PR's branch in step 1, and specialists have no push or merge authority. `RELEASE-SKILL` is coding-policy's release skill where ACR or Tessl installed it: `.codex/skills/acr__jbaruch__coding-policy__release` or `.tessl/plugins/jbaruch/coding-policy/skills/release`. The full contract is in [docs/integration.md](https://github.com/jbaruch/software-factory/blob/main/docs/integration.md).

1. Open the PR from the task's provisioned worktree. This step only opens it; the release skill runs end to end, merge included, so it comes after the release judgment:

   ```sh
   git -c core.hooksPath=/dev/null push origin CANDIDATE:refs/heads/factory/TASK/ID
   gh pr create --repo OWNER/REPO --head factory/TASK/ID --title 'TYPE(SCOPE): SUMMARY' --body-file /absolute/body.md
   ```

   `CANDIDATE` is the task's selected candidate (`inputs.candidate` in `factory.py show --task TASK`), the commit the verifiers approved; `OWNER/REPO` is the admitted repository's GitHub origin. The body carries the issue lines the repository's release policy asks for, such as `Closes #N`. Once a correction or a `rebase-worktree` replay is the selected candidate, push it the same way to move the PR. A replay is no fast-forward, so add `--force-with-lease=refs/heads/factory/TASK/ID:PR_HEAD`, the head the PR shows. The release judgment refuses a PR whose head isn't the selected candidate or whose repository isn't the admitted origin.

2. Once the PR's reviewers have posted on its current head, run the release judgment. Check with one snapshot, `bash RELEASE-SKILL/poll-pr-reviews.sh OWNER REPO NUMBER`, never the blocking watcher: this stage reads the PR without merging it. If a lane has no current-head verdict yet, take up other work and check again on a later round. The snapshot's contract is in the header of `RELEASE-SKILL/poll-pr-reviews.sh`.

   The release judgment is a read-only `release` assignment over the complete PR feedback. Choose a suitable eligible model at its maximum effort. Claude and Antigravity need `max`, Grok and Codex the top effort of their model catalogs. Launch refuses a lower effort, except on Grok, whose worker fails at start instead. For example, on Claude:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-start --task TASK --id release --role release --purpose work \
     --goal 'Judge the PR for release' --cwd /absolute/clean/checkout --claude /absolute/claude \
     --model MODEL --effort max --release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-collect --task TASK --id release
   ```

   The judgment sees the task's specialist roster, with the candidate each specialist judged, and returns `blocked` when the only gap is a specialist the change's effects call for that never approved code still in the change, naming those roles first by their catalog IDs; `findings` are for defects. An approval of an earlier candidate covers an effect whose code later replays and corrections left as it was. Run each named specialist, then judge again under a new ID; a missing specialist is not an escalation.

   `codex-start`, `grok-start` and `antigravity-start` take the same `--release-pr`, `--gh` and `--release-scripts` flags.

3. Read the release judgment's `Pre-deploy:` artifact first. The release skill deploys right after it merges, so do each listed prerequisite outside the repository before merging, or have the operator do or confirm it when it needs authority the line lacks. Then, from the task's worktree, merge with `Skill(skill: "release", args: "existing-pr PR_URL")`. The factory keeps that worktree locked, so the release skill leaves it and its branch to step 5 (coding-policy 0.10.12 and later; an earlier release skill reports its `git worktree remove` failing after the merge, which is expected). Then confirm that it merged what the release judgment approved, naming every required approval with `--after`:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" delivered --task TASK \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   ```

   `delivered` refuses until the PR is merged, and refuses a merged head the release judgment never saw, which goes to the operator. Every `--after` approval must be `approved`, current for the task's exact candidate and requirements, read-only and collected from its original native evidence; approval contexts are distinct and independent of every writer that contributed. `RELEASE-SKILL` must be ignored and untracked. `delivered` records `TASK.RELEASE_ID.delivered.json` beside the task. This is the factory's delivery step, not a hand step.

If the branch conflicts with a moved base before step 1 or during review, run `python3 -B "$SKILL_DIR/runtime/factory.py" rebase-worktree --task TASK --onto BASE-COMMIT`, then a writer (`purpose: work`) whose goal applies the printed patch with `git apply --3way`, resolves the conflicts and re-runs the gate, and verify the produced commit with `--from-writer`: `integration`, the approvals you will name with `--after`, and each effect specialist whose code the resolution changed, then move the open PR to it (step 1). Writers cannot make merge commits, so the factory refreshes by replaying.

4. After the merge and any deploy, run the final acceptance judgment, before cleanup and before selecting other inputs: a new read-only `release` assignment, as in step 2 but with `--delivery ID` in place of `--release-pr`, collected the same way; its `--cwd` is the task's worktree, still at the candidate. `ID` is the release judgment's ID that `delivered` confirmed. Its brief freezes that delivery, and it judges the admitted acceptance against the merged and deployed result, including what the verifiers listed as not yet verifiable. Confirm it by repeating the step 3 `delivered` command with `--acceptance FINAL_ID` added, which prints the receipt with an `acceptance` object and refuses a judgment that isn't approved, assessed a different delivery, or comes from a writer's session. Findings become new work for the task. A blocked judgment names what it waits for, a normal path's next run or another task's delivery: once that has happened, run a new final acceptance, keeping the worktree until then.

5. After independently excluding direct checkout and cache users, run `python3 -B "$SKILL_DIR/runtime/factory.py" release-worktree --task TASK` from a terminal. Keep new users and descendants excluded through receipt readback. For completed pre-upgrade provisioning with no saved `checkout_inodes`, verify the named current checkout and branch before typing the task ID. This authorizes Git removal of that checkout and its admin directory. The branch, original owner and launch records, and caches in dependent worktrees stay retained. A changed or invalid recorded identity blocks release.

   After an interruption, repeat the command for the same owner. A surviving clean checkout still requires the full attended checks. A receipt alongside a surviving checkout, a remaining Git registration or admin directory, or a missing private receipt after removal requires owner inspection. Preserve those resources, the branch, any surviving `.saving-*` files and dependent caches. Keep unpublished files private and leave the original provisioning record intact.

   Retry reports `already_released` after Git deregistration, absence of both endpoints and validation of the published owner receipt. The legacy receipt's `observed_checkout` records only what the operator released. Readback grants no cache-retirement authority. If publication was interrupted after linking the receipt, verify the publisher has stopped and both endpoints are absent. Under the owner locks and recovery authority, remove only the temporary alias identified by the command after verifying both private names are the only links to the matching receipt. Preserve both names when the receipt has no recorded alias. Retry readback and rerun the read-only census.

   Legacy release and recovery leave the branch with its owner. For ordinary post-merge cleanup, delete the merged branch with `git branch -D factory/TASK/ID` after successful readback and proof from `delivered` that its head merged. A squash merge leaves `git branch -d` unable to tell. Otherwise the release policy's session-start leftover hook deletes it once origin holds its tip.

