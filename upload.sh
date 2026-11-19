#!/usr/bin/env bash
# Copy episodes to Google Drive via rclone (skips files already uploaded).
#   Drive layout:  Japanese Podcast/2026-11-18_ep001.mp3 ...
#                  Japanese Podcast/Transcripts/2026-11-18_ep001.html ...
#                  Japanese Podcast/patterns.html
# Settings (config.env): gdrive_remote=gdrive   gdrive_folder=Japanese Podcast
set -uo pipefail
DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
REMOTE=gdrive; FOLDER="Japanese Podcast"
if [[ -f "$DIR/config.env" ]]; then
  r=$(grep -E '^gdrive_remote=' "$DIR/config.env" | cut -d= -f2-); [[ -n "$r" ]] && REMOTE=$r
  f=$(grep -E '^gdrive_folder=' "$DIR/config.env" | cut -d= -f2-); [[ -n "$f" ]] && FOLDER=$f
fi

if ! command -v rclone >/dev/null; then
  echo "upload: rclone not installed; skipping Google Drive upload"; exit 0
fi
if ! rclone listremotes | grep -qx "$REMOTE:"; then
  echo "upload: no rclone remote named '$REMOTE' (run: rclone config); skipping"; exit 0
fi

DEST="$REMOTE:$FOLDER"
status=0
for f in "$DIR"/episodes/*/*.mp3;  do rclone copy "$f" "$DEST/"             || status=1; done
for f in "$DIR"/episodes/*/*.html; do rclone copy "$f" "$DEST/Transcripts/" || status=1; done
[[ -f "$DIR/patterns.html" ]] && { rclone copy "$DIR/patterns.html" "$DEST/" || status=1; }
if [[ $status -eq 0 ]]; then echo "upload: synced to Google Drive → $FOLDER"; else echo "upload: some files failed"; fi
exit $status
