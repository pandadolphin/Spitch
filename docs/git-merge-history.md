# How this repo lands on `main`

| Field | Value |
|-------|-------|
| **Date** | 2026-09-02 |
| **Status** | Practice note (operator preference, not product code) |
| **Trigger** | Landing the Grok / multi-provider stack, then rewriting `origin/main` from a merge commit to linear history |
| **Related** | `origin/main` at `9158c0f` after the rewrite; merge commit `6a8b486` still in the local object store, not on `main` |

## 中文摘要

- **结论（§6）**：这个仓库合进 `main` 时 **保留每一条原 commit 和消息**。提交已经写干净时用 **fast-forward**；大功能需要「这里合入过」的标记时用 **`--no-ff`**。**不要 squash。**
- **今天实际发生的事（§1）**：14 条提交（`0122394..9158c0f`）先被 `--no-ff` 合成 `6a8b486`（双亲 `0122394` + `9158c0f`），再 `reset --hard 9158c0f` + `push --force-with-lease`。`origin/main` 现为线性。`git rev-list --first-parent --count 0122394..6a8b486` 是 **1**；全图是 **15**；FF 之后 first-parent 是 **14**。
- **三种办法（§2）**：FF 移动指针、不长新 commit；`--no-ff` 长一个双亲 merge；squash 长一个单亲新 commit，原 SHA 不是祖先。只有 squash 会丢掉提交记录。
- **看丢历史的原因（§3）**：`--no-ff` 之后 `--first-parent` 只走 parent 1，像历史没了；`git log` 全图和 GitHub commits 页上 14 条一直都在。
- **别人怎么做（§4）**：GitHub / GitLab **默认按钮仍是 merge commit**。抽了若干项目 default branch 最近 30 条：React / VS Code / TypeScript / Deno / Next / PyTorch 几乎全是 squash；`git.git` / Linux / Rust / Kubernetes 多为 merge；Go / Django / Node 为线性、一条变更一个 commit。
- **前公司 vs 当前公司（§5）**：Gerrit 审的是 commit，大功能分支用 `--no-ff`，**每条消息都保留**。GitHub 产品团队审的是 PR，squash 方便，但 `main` 上只剩一句功能说明。AI 更需要高信号的逐步消息（意图和约束），不是 WIP，也不是一条 squash 摘要。

## 1. What happened on this repo (2026-09-02)

The Grok / multi-provider work sat on

`execute-plan/4f44be17-pr-4-docs-multi-provider-grok-stt-setup-and-language-ca`

as a descendant of `main` at `0122394` (`Pause MPRIS media while talking (v0.7.1)`). Tip was `9158c0f` (`ui: show live microphone level`).

```text
git rev-list --count 0122394..9158c0f     → 14
```

There was no GitHub pull request. The first landing used the PR-merge recipe anyway:

```text
git merge --no-ff <feature> -m "Merge branch '<feature>'"
git push origin main
```

That created `6a8b486` with two parents: `0122394` and `9158c0f`. Diff of that merge: 41 files, +7275 / −473.

`--first-parent` on that range showed **one** commit (the merge). Full `git log` and GitHub's commits page still listed all 14. That mismatch felt like history had been thrown away.

Requested shape: linear history, no merge commit. Already pushed, so:

```text
git switch main
git reset --hard 9158c0f
git push --force-with-lease origin main
# 6a8b486...9158c0f  main -> main (forced update)
```

`6a8b486` remains as a dangling object locally (`git cat-file -t 6a8b486` → `commit`) but is not an ancestor of `HEAD`. Anyone who fetched the merge needs to re-fetch.

`main` has since grown past `9158c0f` (sound-cues work). The 14 commits are still on the first-parent line.

## 2. The three landings

Same starting graph: `main` at `0122394`, feature lane of N clean commits ending at `9158c0f`.

| Method | Command (local) | GitHub button | New commit? | Original SHAs on `main` |
|--------|-----------------|---------------|-------------|-------------------------|
| Fast-forward | `git merge --ff-only` | (rebase-and-merge is the closest button; it rewrites SHAs) | No | Yes, on first-parent (FF). Rebase-and-merge: new SHAs, same patches. |
| Merge commit | `git merge --no-ff` | Create a merge commit | Yes, two parents | Yes, on parent 2 |
| Squash | `git merge --squash` then commit | Squash and merge | Yes, one parent, new SHA | No — not ancestors of `main` |

Default `git merge` already fast-forwards when the feature is a descendant of `main`. `--no-ff` is what forced the bubble this afternoon.

GitHub's web merge sets the committer to `GitHub <noreply@github.com>`. This operator merges locally and pushes, for identity, not because the graph shape requires it.

## 3. Why `--no-ff` looks like squash

`git log --first-parent` walks only parent 1: “how `main` itself moved.” After `--no-ff` that walk is: merge commit, then old `main`. The feature commits are still in the object store and in a default `git log`.

Measured on `6a8b486`:

```text
git rev-list --parents -n 1 6a8b486
# 6a8b486  0122394  9158c0f

git rev-list --first-parent --count 0122394..6a8b486   → 1
git rev-list --count 0122394..6a8b486                  → 15   (14 + merge)
git rev-list --first-parent --count 0122394..9158c0f   → 14
```

Squash is the method that actually drops the 14 SHAs from `main`. `--no-ff` only hides them from first-parent views (some GUIs' “simplify”, some mental models of `git log`).

`git revert` of a merge needs `-m 1`. Reverting a squash or a single FF commit is a normal revert.

## 4. What other people do

There is no global majority. The review unit predicts the merge method.

**Platform defaults.** GitHub's docs: the default button is a merge commit, implemented as `git merge --no-ff`. GitLab's default merge method is also a merge commit. If a repo never changes settings, that is what you get. GitHub remembers the last button used per user per repo; teams that want one method disable the others.

**Sample, 2026-09-02:** last 30 commits on each default branch, classified by parent count and message shape (`(#N)` ≈ GitHub squash):

| Cluster | Repos | Shape |
|---------|-------|-------|
| GitHub product | `facebook/react`, `microsoft/vscode`, `microsoft/TypeScript`, `denoland/deno`, `vercel/next.js`, `pytorch/pytorch` | Almost all squash (`title (#123)`, one parent) |
| Integrator / topic merges | `git/git`, `torvalds/linux`, `rust-lang/rust` (bors rollups), `kubernetes/kubernetes` | Many two-parent merges |
| Linear, one change one commit | `golang/go` (Gerrit), `django/django`, `nodejs/node` | One parent, no GitHub squash marker |

Rebase-and-merge is the least visible of GitHub's three buttons in that sample.

A blog claiming “60% of Stack Overflow 2023 respondents squash regularly” was not checked against the survey instrument and should not be cited.

## 5. Two company cultures

**Previous company (Gerrit).** The review unit is the **commit** (Change-Id). Feature-branch commits are tidied before submit. Recollection corrected in this session: **big feature branches land with `--no-ff`**, not fast-forward. Either way, **every commit message is kept**. First-parent is the list of landings; the full graph still has the small steps. That is the same family as Linux / `git.git` / Kubernetes.

**Current company (GitHub squash).** The review unit is the **PR**. Branch commits are scaffolding (`wip`, `fix tests`, `address review`). Squash is consistent with that: `main` becomes a changelog, one row per PR. Convenient. The cost is exactly the Gerrit objection: a large feature on `main` is one description, and the internal structure lives only on the PR page.

Neither is “more Git.” Squash is a workaround for not rewriting the branch. FF / `--no-ff` with preserved commits assumes that rewrite already happened.

**AI coding.** Models already read `git log`, `git blame`, and `git log -p`. A commit that records intent, constraints, and rejected paths is context the diff does not contain. Squash makes blame point at one blob. Dumping 20 `fix` commits onto `main` is worse context than one good squash message.

Worth keeping: **why and constraints**, not a restatement of the diff. Feature-level story belongs in a design note (`docs/…`); step-level why belongs in the commit. That pair is more useful to an agent than either a squash title or the code alone.

## 6. Rule for this checkout

When landing onto `main`:

1. **Do not squash.** Original SHAs and messages stay in the repository.
2. **Fast-forward** when the feature is already a descendant of `main` and the commits are the history you want to read (`git merge --ff-only`). Today's 14 commits were that case; default `git merge` would have fast-forwarded without `--no-ff`.
3. **`--no-ff`** when a large feature should show as one landing on first-parent **and** keep every inner commit on parent 2. That matches the previous company's big-feature rule. Then read the details with a full `git log`, not `--first-parent` alone.
4. Merge locally, then `git push origin main`. Do not use GitHub's merge button or `gh pr merge` (committer becomes GitHub).
5. If a merge commit has already been pushed and linear history is required, `reset --hard` to the feature tip and `push --force-with-lease`. Only on a branch nobody else should have built on.

Pros and cons in one line: FF if the commits *are* the log; `--no-ff` if you also want a landing bubble; squash only if the branch was never written as a log.
