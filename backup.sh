#!/usr/bin/env bash
# Commit new episode scripts/transcripts + progress to the project repo and push.
# Refuses to push if anything that looks like a key or personal info is staged.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[[ -d .git ]] || { echo "backup: not a git repo; skipping"; exit 0; }
git add -A
if git diff --cached --quiet; then echo "backup: nothing new"; exit 0; fi
if git grep --cached -qIE "AIza[0-9A-Za-z_-]{20,}|sk-[A-Za-z0-9]{20,}|ghp_[A-Za-z0-9]{20,}|github_pat_|PRIVATE KEY|@gmail\.com|/home/$USER/" -- $(git diff --cached --name-only | grep -vx backup.sh); then
  git reset -q
  echo "backup: ABORTED — staged files look like they contain a key or personal info. Nothing pushed."
  exit 1
fi
latest=$(python3 -c "import json;e=json.load(open('progress.json'))['episodes'];print(e[-1]['number'] if e else 0)")
git commit -q -F - <<MSG
Episode $latest: scripts, transcripts and progress

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01KJMeKZSBszfH9eZDJtSnKE
MSG
git push -q && echo "backup: pushed episode $latest to GitHub"
