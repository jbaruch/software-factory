# Delivery

Delivery starts after independent verifiers approve the task's current candidate. `SKILL_DIR` is the `factory` skill's directory, as in the skill. The factory never pushes; specialists have no push or merge authority. The full contract is in [docs/integration.md](https://github.com/jbaruch/software-factory/blob/main/docs/integration.md).

1. Push the writer's branch (`factory/TASK/WRITER_ID`) and open the PR through the project's installed release skill. PR preparation, issue declarations and feedback correction stay with that skill.
2. Run the release judgment: a read-only `release` assignment over the complete PR feedback, on the strongest model available on any harness, at that model's maximum effort. Claude and Antigravity need `max`, Grok and Codex the top effort of their model catalogs. Launch refuses a lower effort, except on Grok, whose worker fails at start instead. For example, on Claude:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-start --task TASK --id release --role release --purpose work \
     --goal 'Judge the PR for release' --cwd /absolute/clean/checkout --claude /absolute/claude \
     --model MODEL --effort max --release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   python3 -B "$SKILL_DIR/runtime/factory.py" claude-collect --task TASK --id release
   ```

   `RELEASE-SKILL` is coding-policy's release skill where ACR or Tessl installed it: `.codex/skills/acr__jbaruch__coding-policy__release` or `.tessl/plugins/jbaruch/coding-policy/skills/release`. `codex-start`, `grok-start` and `antigravity-start` take the same `--release-pr`, `--gh` and `--release-scripts` flags.

3. Merge, naming every required approval with `--after`. Where the repository meets the [prerequisites](#prerequisites), integrate:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" integrate --task TASK --id integration \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   ```

   Otherwise merge through the repository's release skill, which runs its own gates and any publication, then confirm that it merged what the release judgment approved:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" delivered --task TASK \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/RELEASE-SKILL
   ```

   `delivered` re-checks the approvals as `integrate` does, refuses until the PR is merged, and refuses a merged head the release judgment never saw, which goes to the operator. It records `TASK.RELEASE_ID.delivered.json` beside the task. This is the factory's delivery step for those repositories, not a hand step.

If the branch conflicts with a moved base before step 1 or during review, run `python3 -B "$SKILL_DIR/runtime/factory.py" rebase-worktree --task TASK --onto BASE-COMMIT`, then a writer (`purpose: work`) whose goal applies the printed patch with `git apply --3way`, resolves the conflicts and re-runs the gate, and verify the produced commit with `--from-writer`. Writers cannot make merge commits, so the factory refreshes by replaying.

4. After the merge, remove the task's provisioned worktree with `python3 -B "$SKILL_DIR/runtime/factory.py" release-worktree --task TASK`. It refuses while the worktree is dirty or an assignment there may still run, and keeps the branch: deleting branches belongs to the repository's release policy, so run the release skill's worktree prune afterwards, which removes a merged branch only after checking it has no unmerged commits.

## Prerequisites

`integrate` holds with an operator prerequisite, and never changes repository settings, unless all of these hold:

- Every `--after` approval is `approved`, current for the task's exact candidate and requirements, read-only and collected from its original native evidence. Approval contexts are distinct and independent of every writer that contributed.
- The release skill is installed, ignored and untracked, at the admitted repository's ACR (`.codex/skills/acr__jbaruch__coding-policy__release`) or Tessl (`.tessl/plugins/jbaruch/coding-policy/skills/release`) location.
- The repository's `checks.yml` defines the required `verify` job, and the canonical review watcher reports the PR ready, including its Claude review.
- The target branch has classic branch protection: strict required status checks, administrator enforcement, no force pushes or deletion, no bypass allowances, and required `verify` bound to the GitHub Actions app (ID 15368).

A repository that doesn't meet them delivers through its release skill and `delivered` (step 3).
