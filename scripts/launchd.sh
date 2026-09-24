#!/usr/bin/env bash
# Run the operations commands as macOS LaunchAgents (per-user, no sudo).
#
#   scripts/launchd.sh install     # write the plists and start them
#   scripts/launchd.sh uninstall   # stop them and remove the plists
#   scripts/launchd.sh status      # what launchd thinks is running
#
# Three agents:
#   com.mazzsmartlead.worker   python -m mazz.ops worker, kept alive
#   com.mazzsmartlead.health   python -m mazz.ops health --notify, every 15 min
#   com.mazzsmartlead.backup   python -m mazz.ops backup, daily at 03:00
#
# Each runs from the repository root, because settings read `.env` relative
# to the working directory -- including EMAIL_SENDER, which stays `stub`
# unless someone deliberately sets it to `google` there. Installing the
# worker does not switch real sending on.
#
# Logs: ~/Library/Logs/MazzSmartLead/<agent>.log

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-$REPO/.venv/bin/python}"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/MazzSmartLead"
DOMAIN="gui/$(id -u)"
LABELS=(com.mazzsmartlead.worker com.mazzsmartlead.health com.mazzsmartlead.backup)

xml_escape() {
  local s="$1"
  s="${s//&/&amp;}"; s="${s//</&lt;}"; s="${s//>/&gt;}"
  printf '%s' "$s"
}

# write_plist LABEL SCHEDULE_XML ARG...
write_plist() {
  local label="$1" schedule="$2"; shift 2
  local args="" a
  for a in "$PYTHON" -m mazz.ops "$@"; do
    args+="    <string>$(xml_escape "$a")</string>"$'\n'
  done
  cat > "$AGENTS_DIR/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$label</string>
  <key>ProgramArguments</key>
  <array>
$args  </array>
  <key>WorkingDirectory</key>
  <string>$(xml_escape "$REPO")</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
  </dict>
  <key>StandardOutPath</key>
  <string>$(xml_escape "$LOG_DIR/${label#com.mazzsmartlead.}.log")</string>
  <key>StandardErrorPath</key>
  <string>$(xml_escape "$LOG_DIR/${label#com.mazzsmartlead.}.log")</string>
$schedule
</dict>
</plist>
PLIST
  plutil -lint "$AGENTS_DIR/$label.plist" >/dev/null
}

install() {
  [[ -x "$PYTHON" ]] || { echo "no python at $PYTHON (set PYTHON=...)" >&2; exit 1; }
  "$PYTHON" -c "import mazz.ops" || { echo "mazz.ops does not import with $PYTHON" >&2; exit 1; }
  mkdir -p "$AGENTS_DIR" "$LOG_DIR"

  # The worker: always on, restarted if it dies. ExitTimeOut gives an
  # in-flight job time to finish after SIGTERM before launchd kills it.
  write_plist com.mazzsmartlead.worker "  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>30</integer>
  <key>ExitTimeOut</key>
  <integer>120</integer>" worker

  write_plist com.mazzsmartlead.health "  <key>RunAtLoad</key>
  <true/>
  <key>StartInterval</key>
  <integer>900</integer>" health --notify

  write_plist com.mazzsmartlead.backup "  <key>StartCalendarInterval</key>
  <dict>
    <key>Hour</key>
    <integer>3</integer>
    <key>Minute</key>
    <integer>0</integer>
  </dict>" backup

  local label
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENTS_DIR/$label.plist"
    echo "loaded $label"
  done
  echo "logs: $LOG_DIR"
}

uninstall() {
  local label
  for label in "${LABELS[@]}"; do
    launchctl bootout "$DOMAIN/$label" 2>/dev/null && echo "stopped $label" || true
    rm -f "$AGENTS_DIR/$label.plist"
  done
}

status() {
  local label
  for label in "${LABELS[@]}"; do
    if launchctl print "$DOMAIN/$label" >/dev/null 2>&1; then
      launchctl print "$DOMAIN/$label" | awk -v l="$label" '
        /^\tstate = /{s=$3} /^\tpid = /{p=$3} /last exit code = /{e=$NF}
        END{printf "%-28s state=%s pid=%s last_exit=%s\n", l, s, (p?p:"-"), (e?e:"-")}'
    else
      printf "%-28s not loaded\n" "$label"
    fi
  done
}

case "${1:-}" in
  install) install ;;
  uninstall) uninstall ;;
  status) status ;;
  *) echo "usage: $0 install|uninstall|status" >&2; exit 64 ;;
esac
