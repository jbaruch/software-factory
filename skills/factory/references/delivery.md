# Delivery

Delivery starts after independent verifiers approve the task's current candidate. `SKILL_DIR` is the `factory` skill's directory, as in the skill. The factory never pushes; specialists have no push or merge authority. The full contract is in [docs/integration.md](https://github.com/jbaruch/software-factory/blob/main/docs/integration.md).

1. Push the writer's branch (`factory/TASK/WRITER_ID`) and open the PR through the project's installed release skill. PR preparation, issue declarations and feedback correction stay with that skill.
2. Run the release judgment: a read-only Codex `release` assignment over the complete PR feedback. Only `codex-start` takes these flags.

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" codex-start --task TASK --id release --role release --purpose work \
     --goal 'Judge the PR for release' --cwd /absolute/clean/checkout --codex /absolute/codex \
     --model MODEL --effort EFFORT --release-pr 'https://github.com/OWNER/REPO/pull/NUMBER' \
     --gh /absolute/gh --release-scripts /absolute/repo/.codex/skills/acr__jbaruch__coding-policy__release
   python3 -B "$SKILL_DIR/runtime/factory.py" codex-collect --task TASK --id release
   ```

3. Integrate, naming every required approval with `--after`:

   ```sh
   python3 -B "$SKILL_DIR/runtime/factory.py" integrate --task TASK --id integration \
     --pr 'https://github.com/OWNER/REPO/pull/NUMBER' --review release --after QA_ID --after REVIEW_ID \
     --gh /absolute/gh --release-scripts /absolute/repo/.codex/skills/acr__jbaruch__coding-policy__release
   ```

If the branch conflicts with a moved base before step 1 or during review, run `python3 -B "$SKILL_DIR/runtime/factory.py" rebase-worktree --task TASK --onto BASE-COMMIT`, then a writer (`purpose: work`) whose goal applies the printed patch with `git apply --3way`, resolves the conflicts and re-runs the gate, and verify the produced commit with `--from-writer`. Writers cannot make merge commits, so the factory refreshes by replaying.

4. After the merge, remove the task's provisioned worktree with `python3 -B "$SKILL_DIR/runtime/factory.py" release-worktree --task TASK`. It refuses while the worktree is dirty or an assignment there may still run, and keeps the branch: deleting branches belongs to the repository's release policy, so run the release skill's worktree prune afterwards, which removes a merged branch only after checking it has no unmerged commits.

## Prerequisites

`integrate` holds with an operator prerequisite, and never changes repository settings, unless all of these hold:

- Every `--after` approval is `approved`, current for the task's exact candidate and requirements, read-only and collected from its original native evidence. Approval contexts are distinct and independent of every writer that contributed.
- The release skill is installed, ignored and untracked, at the admitted repository's `.codex/skills/acr__jbaruch__coding-policy__release`.
- The repository's `checks.yml` defines the required `verify` job, and the canonical review watcher reports the PR ready, including its Claude review.
- The target branch has classic branch protection: strict required status checks, administrator enforcement, no force pushes or deletion, no bypass allowances, and required `verify` bound to the GitHub Actions app (ID 15368).

When a prerequisite cannot be met, such as no Codex quota for the release judgment or a branch without that protection, file the gap at jbaruch/software-factory, merge through the repository's own release policy, and tell the user which step was done by hand.
