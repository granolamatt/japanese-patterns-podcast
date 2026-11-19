#!/usr/bin/env bash
# Install a systemd user timer that generates an episode every morning.
#   ./schedule.sh          -> 06:30 daily
#   ./schedule.sh 07:15    -> custom time
#   ./schedule.sh off      -> remove the schedule
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [[ "${1:-}" == "off" ]]; then
  systemctl --user disable --now jp-podcast.timer || true
  rm -f "$UNIT_DIR"/jp-podcast.{service,timer}
  systemctl --user daemon-reload; echo "Schedule removed."; exit 0
fi
TIME="${1:-06:30}"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/jp-podcast.service" <<UNIT
[Unit]
Description=Generate today's beginner Japanese podcast episode
After=network-online.target
# a failed run (e.g. network down) retries every 20 min, up to 6 times
StartLimitIntervalSec=4h
StartLimitBurst=6

[Service]
Type=oneshot
ExecStart=$DIR/today
TimeoutStartSec=45min
Restart=on-failure
RestartSec=20min
UNIT
cat > "$UNIT_DIR/jp-podcast.timer" <<UNIT
[Unit]
Description=Daily beginner Japanese podcast at $TIME

[Timer]
OnCalendar=*-*-* $TIME:00
Persistent=true
RandomizedDelaySec=60

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now jp-podcast.timer
loginctl enable-linger "$USER" 2>/dev/null || true
systemctl --user list-timers jp-podcast.timer --no-pager
