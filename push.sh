#!/bin/bash
# Commit and push all local changes to github.com/kf0uqr/irc
set -e
cd "$(dirname "$0")"

git add -A

if git diff --cached --quiet; then
  echo "No changes to push."
  exit 0
fi

msg="${1:-Update $(date '+%Y-%m-%d %H:%M')}"
git commit -m "$msg"
git push origin main
