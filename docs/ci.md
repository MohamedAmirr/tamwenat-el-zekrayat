# CI

## Stage 1 (built)

Workflow: `.github/workflows/stage-1.yml` (workflow name `Stage 1`, one job id `checks`, job name `Stage 1 checks`). No bench, no database; the first run on GitHub (PR #201) took about 19 seconds.

Runs on pull requests into `dev` and on manual `workflow_dispatch`. It does not run on PRs into `prod` (those are publish PRs of work already checked on `dev`).

What it checks (all in `apps/marketplace_integrations`):

1. No git merge-conflict markers (`<<<<<<< ` / `>>>>>>> `) in tracked files.
2. `ruff check .` (ruff 0.14.10, the same pin as `.pre-commit-config.yaml`; config in `pyproject.toml`).
3. `npm ci`, then `eslint --quiet` over the app's JS with the same exclusions as the pre-commit eslint hook (`public/dist`, `cypress`, `boilerplate`, `templates/includes`, `public/js/lib`, `node_modules`).
4. Every `*.test.js` with `node --test`. The `sales_order.test.js` case that needs `ERPNEXT_SALES_ORDER_JS` stays skipped here (last local run: 13 files, 178 tests, 177 pass, 1 skipped).

### Why no path filter
The job is meant to be a required status check. A required check that is skipped by a path filter never reports, and the PR cannot merge. So it runs on every PR into `dev`.

### Why no format checks
`ruff format --check` and `prettier --check` fail on pre-existing drift in files we left alone. Add them only after a dedicated formatting PR.

### Run the same commands locally
From the repo root:

```
git grep -n -E '^(<<<<<<<|>>>>>>>) ' -- apps/marketplace_integrations   # must print nothing
cd apps/marketplace_integrations
uvx ruff@0.14.10 check .
npm ci
npx eslint --quiet --ext .js --ignore-pattern 'marketplace_integrations/public/dist/**' --ignore-pattern 'cypress/**' --ignore-pattern '**/boilerplate/**' --ignore-pattern 'marketplace_integrations/templates/includes/**' --ignore-pattern 'marketplace_integrations/public/js/lib/**' marketplace_integrations
node --test $(find marketplace_integrations -name '*.test.js' -not -path '*/node_modules/*' | sort)
```

### Required check and the protect-dev ruleset
GitHub shows the check on a PR as `Stage 1 / Stage 1 checks` (workflow name / job name). In a ruleset's required status checks you pick it by the job name, `Stage 1 checks` (the check run is named after the job; confirm in the picker). Do not rename the workflow or the job afterwards, or the required check will never report.

Add the required status check to the `protect-dev` ruleset only after the workflow has run once on GitHub; the picker only lists checks it has seen.

## Stage 2 (full bench): not built yet

Facts from the survey of frappe/erpnext CI, for when it is built:

- Services: `mariadb:11.8`, redis.
- `bench init` with `--frappe-branch version-16`.
- `bench get-app erpnext` (same version branch) before the app, because the app depends on ERPNext.
- Our app lives at `apps/marketplace_integrations` in the monorepo, not at the repo root, so the app path must be given explicitly.
