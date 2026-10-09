# Publishing the app for Frappe Cloud (the `prod` branch)

## Why

Frappe tooling (`bench get-app`, Frappe Cloud) expects `pyproject.toml` at the repo root. This repo is a deployment repo and the app lives in `apps/marketplace_integrations/`, so installing straight from the integration branch fails with `pyproject.toml does not exist in app directory`.

## Branches

Repo: `MohamedAmirr/tamwenat-el-zekrayat`.

- `dev`: the integration branch. All work lands here via pull request. Because the DEFAULT branch is `prod`, always pass the base explicitly: `gh pr create --base dev`.
- `prod`: the DEFAULT branch. Derived: its tree root is `apps/marketplace_integrations/` of `dev` (so `pyproject.toml` is at the root). PR-only. Never edit by hand.

Rules:

- Nobody pushes directly to `dev` or `prod` (branch rules require pull requests, no bypass). The user merges PRs.
- PRs into `prod` come only from `scripts/publish-prod.sh`.
- Each publish commit has the current `origin/prod` tip as parent (so the PR shares history), tree = `<source>:apps/marketplace_integrations`, message `Publish apps/marketplace_integrations from <source> <shortsha>`, and a body line `Source: <source> <full sha>`. The next publish takes the newest `Source:` line anywhere in `origin/prod` history (`git log --topo-order`) as the last published source sha, so it still works after GitHub merges the publish PR with a merge commit (whose own message has no `Source:` line).

## Publish flow

From the repo root, on a clean `dev` equal to `origin/dev`:

```
scripts/publish-prod.sh                  # dry run (default): prints the plan, changes nothing outward
scripts/publish-prod.sh --pr             # pushes publish/<shortsha> on top of origin/prod, runs gh pr create --base prod
scripts/publish-prod.sh --pr --no-pr-create   # push the branch, skip gh (e.g. testing against a bare repo)
scripts/publish-prod.sh --init           # one-time bootstrap, see below
```

Options: `--source <branch>` (default `dev`), `--remote <name>` (default `origin`), `--dry-run` (with `--init`/`--pr`: plan only). With no mode flag it dry-runs `--init` if `origin/prod` does not exist, else `--pr`.

Then merge the publish PR into `prod` in GitHub. It refuses (exit 1) if: not at the repo root, tree dirty, not on the source branch, source differs from `<remote>/<source>`, `--init` while `prod` exists, `--pr` while `prod` does not exist, `publish/<shortsha>` already exists on the remote (never forced), the new tree lacks `pyproject.toml`/`README.md`/`marketplace_integrations/{__init__,hooks}.py`, contains `node_modules/`, `__pycache__/`, `.env` or `*.pyc`, or `pyproject.toml` `name` is not `marketplace_integrations` (parsed with `tomllib`, so a Python >= 3.11 must be on PATH). If `prod` already has the same tree it prints `already published` and exits 0.

The PR body lists the source commits since the last published source sha that touch `apps/marketplace_integrations` (commits elsewhere in the repo are omitted) and adds `Closes #N` for every `(#N)` found in those (app-touching) commit subjects (best-effort; check the list).

## One-time setup order

1. Bootstrap BEFORE enabling branch rules: `scripts/publish-prod.sh --init` creates the root commit and pushes it straight to `prod`. This only works while `prod` is unprotected; the script prints a warning.
2. Set the repo default branch to `prod`.
3. Enable the rulesets (PR required, no bypass) on `dev` and `prod`.
4. The integration branch is called `dev` (renamed from `main`).

## Branches and PRs (rules for everyone, agents included)

- Nobody pushes directly to `dev` or `prod`; both require a PR with no bypass. The user merges PRs.
- Feature branches are cut from an up-to-date `dev` (`git fetch`, fast-forward `dev`), never from `prod`.
- Every normal PR must be created with `gh pr create --base dev`; the default base is `prod`.
- `prod` PRs come only from `scripts/publish-prod.sh --pr`.
- Never open a PR from `dev`, or from any feature branch, into `prod`, and ignore GitHub's compare banner: `dev` and `prod` have no common history (GitHub reports "No common ancestor between prod and dev") so such a PR is impossible or, if forced, would put the monorepo layout into `prod` and break Frappe Cloud. The only PR into `prod` is the one `scripts/publish-prod.sh --pr` builds from a `publish/<sha>` branch.
- Release order: apply on `dev` first (PR, tests, migrate on the dev/test sites, manual checks), then publish `dev` to `prod` with `scripts/publish-prod.sh --pr`, merge that PR, then Update in the Frappe Cloud Bench Group. Never the reverse; `prod` gets no work of its own.
- `Closes #N` in a PR into `dev` does not auto-close the issue (see below).

## Closing issues

`Closes #N` in a `dev` PR does not auto-close the issue, because `dev` is not the default branch (GitHub only auto-closes on merges into the default branch). The publish PR carries the `Closes` lines, so issues close when it is merged into `prod`. You can also close issues manually.

## In Frappe Cloud (not verified from this repo; check the Frappe Cloud docs)

1. Add the app from GitHub: repo `MohamedAmirr/tamwenat-el-zekrayat`, branch `prod`. Give the Frappe Cloud GitHub app access to the (private) repo first.
2. After each publish PR is merged, use "Update Available" in the Bench Group, then update the site(s).

Frappe Cloud answered "Could not find a compatible Frappe version in pyproject.toml ... ensure `[tool.bench.frappe-dependencies]` is defined" the first time it read `prod`, so `apps/marketplace_integrations/pyproject.toml` declares `[tool.bench.frappe-dependencies]` with `frappe = ">=16.21.0,<17.0.0"`. The format and the floor are copied from ERPNext's own `pyproject.toml` (the app requires ERPNext through `required_apps` in `hooks.py`, so any bench that has the app already has Frappe 16.21 or newer). The app is tested only on Frappe 16.32 / ERPNext 16.33.

## Unverified prerequisites

- Resolved at the add-app step (reported by the user, 2026-10-09): Frappe Cloud's "Add app from GitHub" accepted the app from branch `prod` of the repo `tamwenat-el-zekrayat`, even though the repo name differs from the app name `marketplace_integrations`. Not verified beyond that step: building a Bench Group with the app and installing it on a site.

- The app declares `requires-python >=3.14` and targets Frappe/ERPNext v16. The Bench Group must run a compatible Python and Frappe/ERPNext v16. Not checked.
- The app's other dependencies (see `pyproject.toml`) must install on Frappe Cloud. Not checked.
