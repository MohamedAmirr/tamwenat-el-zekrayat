# GitHub issue conventions

How every feature gets turned into GitHub issues in this repo. Followed by the `feature-planner` agent (drafts) and `git-ops` agent (creates them).

## Structure: one Epic + ordered sub-issues

- One **Epic** issue per feature, titled `[Epic] <Feature Name>`, labeled `epic`.
- 2-4 **sub-issues** under it, titled `<Feature Name>: <Step>`, labeled with the feature area (e.g. `talabat`). Keep the count small — if a feature needs more than ~4 steps, the steps are probably too granular, not the feature too big.
- Sub-issues are ordered and often depend on the previous one(s) — state that dependency explicitly (see body template). Don't create a sub-issue that can't be started yet without saying so.
- Cross-feature dependencies (sub-issue in Epic B needs a sub-issue from Epic A) are allowed and must be called out the same way.

## Creation order

1. Create sub-issues first (need their numbers before the epic can reference them).
2. Create the epic last, with a checklist referencing each sub-issue.
3. Best-effort: also link them with GitHub's native sub-issue relation so the epic shows a progress bar:
   ```
   sub_id=$(gh api repos/{owner}/{repo}/issues/{sub_number} --jq .id)
   gh api repos/{owner}/{repo}/issues/{epic_number}/sub_issues -F sub_issue_id=$sub_id
   ```
   Use `-F` (typed field), not `-f` — `-f` sends `sub_issue_id` as a string and the API rejects it with a 422 ("not of type integer"). If it still errors after that (feature not enabled on the repo/plan), skip it silently — the markdown checklist in the epic body is the fallback and is sufficient on its own.

## Body template — optimized for a future Claude session reading it cold

Goal: whoever (human or Claude) opens this issue later gets full working context from the issue body alone, without re-reading the whole codebase, and without the issue re-explaining things that already live in `docs/`. Structured headers over prose — scanning beats parsing.

**Sub-issue body:**

```markdown
## Depends on
#<number> — <one line why>   (or: None)

## Scope
- <concrete deliverable>
- <concrete deliverable>

## Files
- `path/to/file` — <what changes, one line>

## Acceptance
- [ ] <concrete, checkable outcome>
- [ ] <concrete, checkable outcome>

## Out of scope
- <explicit non-goal, to stop future scope creep>

## Reference
<link to the doc(s) with the actual conventions/spec — never restate them here>
```

**Epic body:**

```markdown
## Goal
<1-2 lines: what this feature does and why>

## Sub-issues (in order)
1. #<number> — <title>
2. #<number> — <title>

## Reference
<links to relevant docs/>
```

Rules for keeping these cheap to read:
- Never restate content from `CLAUDE.md` or `docs/*.md` — link to it.
- No narrative/prose paragraphs explaining rationale at length; one line of "why" per item is enough.
- Every "Scope" and "Acceptance" bullet must be concrete (a file, a function, a behavior) — not "improve X" or "handle edge cases."
- Skip sections that don't apply rather than filling them with filler text.

## Branch naming

One branch per sub-issue, created when work actually starts on it (not when the issue is filed). Format:

`<type>/<issue-number>-<kebab-slug>`

- `<type>`: `feat` (new capability), `fix`, `chore`, `docs` — matches the issue's actual work; sub-issues here are almost always `feat`.
- `<issue-number>`: the sub-issue's own GitHub number, never the epic's.
- `<kebab-slug>`: 2-5 words from the issue title, lowercase, hyphenated.

Example: issue #1 "Talabat Authentication: Talabat Settings doctype" → `feat/1-talabat-settings-doctype`.

Create it with `gh issue develop <number> --name <branch> --base dev --checkout`, not a plain `git checkout -b` — this makes it a GitHub-linked branch that shows under the issue's "Development" section and links the PR to the issue. Always pass `--base dev`: without it the branch is cut from the default branch `prod`, which has no common history with `dev` and a different file layout. Fall back to a plain branch only if `gh issue develop` isn't available. Branch from an up-to-date `dev`, and open the PR with `--base dev` (default branch is `prod`). `Closes #N` in a PR merged into `dev` does not auto-close the issue; see `docs/frappe-cloud-publish.md`.

## Amending a sub-issue before it's merged

If planning/implementation surfaces a correction to a sub-issue's scope *before its PR has merged* (e.g. a design gap found while building it), don't file a new issue for the correction — edit the existing issue body in place (`gh issue edit <number> --body ...`, keeping the same section structure) and amend the same branch/PR rather than opening a second one. If the correction adds a genuinely new step (a new file/doctype/deliverable, not just a tweak to an existing one), file it as its own sub-issue instead, insert it in the right dependency order, and update the epic body's numbered list to match — epics are allowed to grow past their initial sub-issue count when real scope was missing, not just when it was mis-estimated.

## Labels

Ensure these exist before use (`gh label list`, create with `gh label create <name> --color <hex>` if missing): `epic`, and one label per feature area (e.g. `talabat`). Don't invent a new label per issue.
