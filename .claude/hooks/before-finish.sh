#!/usr/bin/env bash
# Stop hook: run CI's tests job before Claude finishes. Exit 2 blocks the stop
# and hands the failing output back to Claude.
set -uo pipefail

input=$(cat)
if printf '%s' "$input" | grep -Eq '"stop_hook_active"[[:space:]]*:[[:space:]]*true'; then
  exit 0
fi

cd "${CLAUDE_PROJECT_DIR:-$(git rev-parse --show-toplevel)}" || exit 0
git_dir=$(git rev-parse --absolute-git-dir)

# Tree of the working copy, untracked files included, built in a scratch index.
tmp_index=$(mktemp)
trap 'rm -f "$tmp_index"' EXIT
cp "$git_dir/index" "$tmp_index" 2>/dev/null || true
tree=$(GIT_INDEX_FILE="$tmp_index" git add -A . && GIT_INDEX_FILE="$tmp_index" git write-tree) || exit 0

# CI has no path filters, so any change runs everything. Without a merge-base,
# run the checks rather than skip them.
base=$(git merge-base HEAD origin/main 2>/dev/null) || base=""
if [ -n "$base" ] && [ "$tree" = "$(git rev-parse "$base^{tree}")" ]; then
  exit 0
fi
stamp="$git_dir/claude-before-finish.pass"
if [ "$(cat "$stamp" 2>/dev/null)" = "$tree" ]; then
  exit 0
fi

py=""
for candidate in "$PWD/.venv/bin/python" python3.11 python3; do
  if command -v "$candidate" >/dev/null && "$candidate" -c 'import fastapi, pytest, ruff' 2>/dev/null; then
    py=$candidate
    break
  fi
done
if [ -z "$py" ]; then
  echo "before-finish: no Python with requirements-dev.txt installed; see CLAUDE.md." >&2
  [ "${CLAUDE_CODE_REMOTE:-}" = "true" ] && exit 2
  exit 0
fi

failures=""
check() {
  local out
  if ! out=$("$@" 2>&1); then
    failures+="\$ $*"$'\n'"$out"$'\n\n'
  fi
}

check "$py" scripts/validate_release.py
check bash -c "find . -type f -name '*.sh' -not -path './.git/*' -print0 | xargs -0 -r -n1 bash -n"
check git diff --check "$(git hash-object -t tree /dev/null)" "$tree"
check "$py" -m ruff check .
check "$py" scripts/run_tests.py

if [ -n "$failures" ]; then
  printf 'CI checks failed; fix these before finishing:\n\n%s' "$failures" >&2
  exit 2
fi
echo "$tree" > "$stamp"
