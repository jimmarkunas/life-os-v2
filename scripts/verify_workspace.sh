#!/usr/bin/env bash
set -euo pipefail

# Fail early when this checkout is attached to the wrong repository or when
# the sandbox cannot update Git metadata.  Run from any directory.
repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
cd "$repo_root"

expected_remote="https://github.com/jimmarkunas/life-os-v2.git"
actual_remote="$(git remote get-url origin)"
[[ "$actual_remote" == "$expected_remote" ]] || {
  echo "ERROR: origin is $actual_remote (expected $expected_remote)" >&2
  exit 1
}

git_dir="$(git rev-parse --git-dir)"
[[ -d "$git_dir" && -w "$git_dir" ]] || {
  echo "ERROR: Git metadata directory is not writable: $git_dir" >&2
  exit 1
}
# This is the authoritative check: permission bits can look writable while the
# sandbox still rejects the metadata operation.
git fetch --prune origin
[[ -e "$git_dir/FETCH_HEAD" && -w "$git_dir/FETCH_HEAD" ]] || {
  echo "ERROR: Git fetch completed but FETCH_HEAD is not writable: $git_dir/FETCH_HEAD" >&2
  exit 1
}
actual_main="$(git rev-parse origin/main)"
if [[ -n "${EXPECTED_ORIGIN_MAIN:-}" && "$actual_main" != "$EXPECTED_ORIGIN_MAIN" ]]; then
  echo "ERROR: origin/main is $actual_main (expected $EXPECTED_ORIGIN_MAIN)" >&2
  exit 1
fi

echo "workspace healthy: $repo_root"
echo "origin/main: $actual_main"
