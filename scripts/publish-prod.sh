#!/usr/bin/env bash
# Publish apps/marketplace_integrations as the root of the derived, PR-only `prod` branch,
# so Frappe Cloud / `bench get-app` can install it (they need pyproject.toml at the repo root).
# See docs/frappe-cloud-publish.md.
#
# Usage (from the repo root, on a clean <source> branch equal to <remote>/<source>):
#   scripts/publish-prod.sh                  # DRY RUN: print the plan for whichever mode applies, change nothing outward
#   scripts/publish-prod.sh --pr             # push publish/<shortsha> (on top of <remote>/prod) and open a PR into prod
#   scripts/publish-prod.sh --pr --no-pr-create   # push the branch only, do not call gh
#   scripts/publish-prod.sh --init           # ONE-TIME: create prod (root commit) by direct push; only works while prod is unprotected
#   Add --dry-run to --init/--pr to print the plan only.
#   Options: --remote <name> (default origin), --source <branch> (default dev)
#
# Exit codes: 0 ok / already published, 1 refused (precondition or sanity check failed), 2 bad usage.
set -euo pipefail

APP_DIR="apps/marketplace_integrations"
remote="origin"
source_branch="dev"
mode=""          # init | pr | "" (dry run, auto-detect)
dry=0
create_pr=1

die() { echo "ERROR: $*" >&2; exit 1; }
need_arg() { [ "$1" -ge 2 ] || { echo "$2 needs a value" >&2; exit 2; }; }

while [ $# -gt 0 ]; do
  case "$1" in
    --init) mode=init ;;
    --pr) mode=pr ;;
    --dry-run) dry=1 ;;
    --no-pr-create) create_pr=0 ;;
    --remote) need_arg $# --remote; remote="$2"; shift ;;
    --source) need_arg $# --source; source_branch="$2"; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "Unknown argument: $1 (see --help)" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$mode" ] || dry=1

# --- Preconditions (refuse, never fix silently) ---
top=$(git rev-parse --show-toplevel 2>/dev/null) || die "not inside a git repository"
[ "$PWD" = "$top" ] || die "run from the repo root ($top)"
branch=$(git symbolic-ref --short -q HEAD || true)
[ "$branch" = "$source_branch" ] || die "current branch is '${branch:-detached HEAD}', must be '$source_branch'"
[ -z "$(git status --porcelain)" ] || die "working tree is not clean; commit or stash first"
git fetch --quiet "$remote" || die "git fetch $remote failed"
src_sha=$(git rev-parse "refs/heads/$source_branch")
remote_src=$(git rev-parse -q --verify "refs/remotes/$remote/$source_branch" || true)
[ -n "$remote_src" ] || die "$remote/$source_branch not found"
[ "$src_sha" = "$remote_src" ] || die "$source_branch ($src_sha) is not equal to $remote/$source_branch ($remote_src); push or pull first"

prod_tip=$(git rev-parse -q --verify "refs/remotes/$remote/prod" || true)
if [ -z "$mode" ]; then
  if [ -n "$prod_tip" ]; then mode=pr; else mode=init; fi
fi
if [ "$mode" = init ] && [ -n "$prod_tip" ]; then
  die "$remote/prod already exists; --init is only for the one-time bootstrap. Use --pr."
fi
if [ "$mode" = pr ] && [ -z "$prod_tip" ]; then
  die "$remote/prod does not exist yet; run --init first (before enabling branch rules)."
fi

# --- Build the commit ---
tree=$(git rev-parse "$source_branch:$APP_DIR") || die "$APP_DIR not found on $source_branch"
if [ "$mode" = pr ] && [ "$(git rev-parse "$prod_tip^{tree}")" = "$tree" ]; then
  echo "already published ($remote/prod = $prod_tip has the same tree)"
  exit 0
fi
short=$(git rev-parse --short "$src_sha")
parent_args=()
[ "$mode" = pr ] && parent_args=(-p "$prod_tip")
commit=$(git commit-tree "$tree" ${parent_args[@]+"${parent_args[@]}"} \
  -m "Publish $APP_DIR from $source_branch $short" -m "Source: $source_branch $src_sha")

# --- Sanity checks BEFORE any push ---
files=$(git ls-tree -r --name-only "$commit")
for f in pyproject.toml README.md marketplace_integrations/__init__.py marketplace_integrations/hooks.py; do
  printf '%s\n' "$files" | grep -qx -- "$f" || die "published tree is missing $f"
done
bad=$(printf '%s\n' "$files" | grep -E '(^|/)node_modules/|(^|/)__pycache__/|(^|/)\.env($|\.)|\.pyc$' || true)
[ -z "$bad" ] || die "published tree contains files that must not ship:
$bad"
py=""
for c in python3.14 python3.13 python3.12 python3.11 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import tomllib' 2>/dev/null; then py="$c"; break; fi
done
[ -n "$py" ] || die "no python with tomllib found (need Python >= 3.11 as python3.11+ or python3 on PATH)"
name=$(git show "$commit:pyproject.toml" | "$py" -c 'import sys, tomllib; print(tomllib.loads(sys.stdin.read()).get("project", {}).get("name", ""))') \
  || die "pyproject.toml could not be parsed with $py tomllib"
[ "$name" = "marketplace_integrations" ] || die "pyproject.toml name is '$name', expected 'marketplace_integrations'"

echo "Mode:    $mode$([ "$dry" -eq 1 ] && echo ' (dry run)')"
echo "Source:  $source_branch $src_sha"
echo "Commit:  $commit"
echo "Parent:  ${prod_tip:-<none, root commit>}"
echo "Files:   $(printf '%s\n' "$files" | wc -l | tr -d ' ')   name: $name"

# --- init: one-time direct push ---
if [ "$mode" = init ]; then
  echo "WARNING: --init pushes straight to $remote/prod. It only works while prod is NOT protected by branch rules."
  if [ "$dry" -eq 1 ]; then echo "Dry run: would run: git push $remote $commit:refs/heads/prod"; exit 0; fi
  git push "$remote" "$commit:refs/heads/prod"
  echo "Created $remote/prod."
  exit 0
fi

# --- pr: publish branch + PR ---
pub_branch="publish/$short"
if git ls-remote --exit-code --heads "$remote" "$pub_branch" >/dev/null 2>&1; then
  die "$remote/$pub_branch already exists; merge or delete it first (never forced)"
fi

# Source sha of the last publish is in the body of the prod tip commit.
last=$(git log -1 --format=%b "$prod_tip" | sed -n 's/^Source: [^ ]* \([0-9a-f]\{40\}\)$/\1/p' | head -n 1)
if [ -n "$last" ] && git cat-file -e "$last^{commit}" 2>/dev/null && git merge-base --is-ancestor "$last" "$src_sha"; then
  subjects=$(git log --no-merges --format=%s "$last..$src_sha")
  range="since last published source ${last:0:7}"
else
  subjects=""
  range="last published source sha not found in $remote/prod tip commit (or not an ancestor of $source_branch); commit list unavailable"
fi
issues=$(printf '%s\n' "$subjects" | grep -oE '\(#[0-9]+\)' | tr -d '(#)' | sort -un || true)
body="Publishes \`$APP_DIR\` from \`$source_branch\` @ \`$src_sha\` to \`prod\`.

Source commits $range:
$(printf '%s\n' "$subjects" | sed '/^$/d; s/^/- /')

Issues below were found as \`(#N)\` in those commit subjects (best-effort, check the list). \`prod\` is the default branch, so GitHub closes issues only when this PR is merged:
$(printf '%s\n' "$issues" | sed '/^$/d; s/^/Closes #/')"
title="Publish $source_branch $short to prod"

if [ "$dry" -eq 1 ]; then
  echo "Dry run: would run: git push $remote $commit:refs/heads/$pub_branch"
  if [ "$create_pr" -eq 1 ]; then
    echo "Dry run: would run: gh pr create --base prod --head $pub_branch --title \"$title\" --body <below>"
  fi
  echo "----- PR body -----"
  printf '%s\n' "$body"
  exit 0
fi

git push "$remote" "$commit:refs/heads/$pub_branch"
echo "Pushed $remote/$pub_branch."
if [ "$create_pr" -eq 1 ]; then
  gh pr create --base prod --head "$pub_branch" --title "$title" --body "$body"
else
  echo "Skipped PR creation (--no-pr-create). PR body would be:"
  printf '%s\n' "$body"
fi
