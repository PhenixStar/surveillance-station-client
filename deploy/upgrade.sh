#!/usr/bin/env bash
# Upgrade a from-source install to the newest commit of its branch (default mce) or to a tag.
#
#   deploy/upgrade.sh [--check] [--tag v0.16.0-mce.1]
#
# Refuses while the app runs (single-instance GApplication: a running copy keeps the old code),
# backs up config.toml, pulls fast-forward only, prints dependency changes from pyproject.toml
# and reinstalls. Rollback: git -C "$SRC" checkout <old sha|tag> && .venv/bin/pip install --no-deps -e .
set -euo pipefail

PREFIX="${SS_CLIENT_PREFIX:-$HOME/.local/opt/surveillance-station-client}"
SRC="$PREFIX/src"
BRANCH="${SS_CLIENT_BRANCH:-mce}"
CHECK=0; TAG=""
while [ $# -gt 0 ]; do
  case "$1" in
    --check) CHECK=1; shift ;;
    --tag) TAG="$2"; shift 2 ;;
    *) echo "unknown option $1" >&2; exit 2 ;;
  esac
done
cd "$SRC"
[ -z "$(git status --porcelain --untracked-files=no)" ] || { echo "uncommitted changes in $SRC" >&2; exit 1; }

git fetch -q origin --tags
old=$(git rev-parse HEAD)
target="origin/$BRANCH"; [ -n "$TAG" ] && target="refs/tags/$TAG"
new=$(git rev-parse "$target")
echo "installed $(git describe --tags --always "$old"), target $(git describe --tags --always "$new")"
[ "$old" != "$new" ] || { echo "already current"; exit 0; }
git log --oneline "$old..$new" | head -40
[ "$CHECK" = 1 ] && exit 0

if pgrep -f "$SRC/.venv/bin/surveillance" >/dev/null; then
  echo "quit the Surveillance Station app first" >&2; exit 1
fi
cfg="$HOME/.config/surveillance-station"
if [ -d "$cfg" ]; then
  backup="$HOME/.config/surveillance-station-backup-$(date +%y%m%d-%H%M).tgz"
  tar czf "$backup" -C "$HOME/.config" surveillance-station && echo "config backup: $backup"
fi

if [ -n "$TAG" ]; then
  git checkout -q "$TAG"
else
  git switch -q "$BRANCH" 2>/dev/null || git switch -q -c "$BRANCH" "origin/$BRANCH"
  git merge -q --ff-only "origin/$BRANCH"
fi
echo "--- dependency changes (pyproject.toml) ---"
git diff "$old" "$new" -- pyproject.toml | grep -E '^[+-] ' | grep -vE 'version *=' || echo "none"
.venv/bin/pip install -q --no-deps -e .
echo "upgraded to $(git describe --tags --always)  (rollback: git checkout $old && .venv/bin/pip install --no-deps -e .)"
