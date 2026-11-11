#!/bin/bash
# Installs the Spotify multi-account switcher v2 (socket-activated helper LaunchAgent +
# Spicetify extension).
#
#   ./install.sh           install/update helper, copy extension, register it in Spicetify
#   ./install.sh --apply   additionally run `spicetify apply` (restarts Spotify!)
#   ./install.sh --helper-only   only the helper (extension installed via Spicetify Marketplace)
set -euo pipefail

APPLY=0
HELPER_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --helper-only) HELPER_ONLY=1 ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.spotify-multiaccount.switcher"
V1_LABEL="com.spotify-multiaccount.helper"
MA_DIR="$HOME/Library/Application Support/spotify-multiaccount"
V1_DIR="$HOME/Library/Application Support/Spotify-Accounts"
BIN_DIR="$MA_DIR/bin"
SCRIPT="$BIN_DIR/spotify_accounts_helper.py"
AGENTS_DIR="$HOME/Library/LaunchAgents"
PLIST="$AGENTS_DIR/$LABEL.plist"
V1_PLIST="$AGENTS_DIR/$V1_LABEL.plist"
DOMAIN="gui/$(id -u)"
PORT=48655
EXT_SRC="$PROJECT_DIR/extension/accountSwitcher.js"

bootout_wait() {  # $1 = label
  launchctl bootout "$DOMAIN/$1" 2>/dev/null || true
  # bootout is asynchronous; wait until the job is gone
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/$1" >/dev/null 2>&1 || break
    sleep 0.5
  done
}

echo "==> v1 cleanup"
if launchctl print "$DOMAIN/$V1_LABEL" >/dev/null 2>&1 || [ -e "$V1_PLIST" ]; then
  bootout_wait "$V1_LABEL"
  rm -f "$V1_PLIST"
  echo "    old LaunchAgent $V1_LABEL removed"
else
  echo "    nothing to do"
fi
if [ -d "$V1_DIR" ]; then
  echo "    NOTE: v1 data is left untouched in $V1_DIR (not used by v2;"
  echo "          it contains copies of Spotify logins - delete it yourself when no longer needed)."
fi

echo "==> Helper"
mkdir -p "$BIN_DIR"
chmod 700 "$MA_DIR"
chmod 755 "$BIN_DIR"
install -m 644 "$PROJECT_DIR/helper/spotify_accounts_helper.py" "$SCRIPT"

mkdir -p "$AGENTS_DIR"
/usr/bin/python3 - "$PROJECT_DIR/helper/$LABEL.plist" "$PLIST" "$SCRIPT" "$MA_DIR" <<'PY'
import sys
from xml.sax.saxutils import escape
src, dst, script, ma_dir = sys.argv[1:5]
text = open(src, encoding="utf-8").read()
text = text.replace("__SCRIPT__", escape(script)).replace("__MA_DIR__", escape(ma_dir))
with open(dst, "w", encoding="utf-8") as fh:
    fh.write(text)
PY
chmod 644 "$PLIST"
plutil -lint "$PLIST" >/dev/null

bootout_wait "$LABEL"
launchctl bootstrap "$DOMAIN" "$PLIST"

# The first connection makes launchd start the helper (socket activation).
ok=0
for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then ok=1; break; fi
  sleep 0.5
done
if [ "$ok" = 1 ]; then
  echo "    helper answers on 127.0.0.1:$PORT (started on demand, exits when idle)"
else
  echo "    WARNING: helper not reachable, see $MA_DIR/helper.log and launchd.log" >&2
fi

echo "==> Spicetify extension"
if [ "$HELPER_ONLY" = 1 ]; then
  echo "    skipped (--helper-only)"
elif [ ! -f "$EXT_SRC" ]; then
  echo "    extension/accountSwitcher.js not found - skipped"
elif ! command -v spicetify >/dev/null 2>&1; then
  echo "    spicetify not found - skipped"
else
  CONFIG_FILE="$(spicetify -c)"
  EXT_DIR="$(dirname "$CONFIG_FILE")/Extensions"
  mkdir -p "$EXT_DIR"
  install -m 644 "$EXT_SRC" "$EXT_DIR/accountSwitcher.js"
  echo "    copied to $EXT_DIR"
  current_list="$(sed -n 's/^[[:space:]]*extensions[[:space:]]*=[[:space:]]*//p' "$CONFIG_FILE" | head -n 1)"
  case "|$current_list|" in
    *"|accountSwitcher.js|"*) echo "    already listed in Spicetify config" ;;
    *) spicetify config extensions accountSwitcher.js   # appends, keeps other extensions
       echo "    added to Spicetify config" ;;
  esac
  if [ "$APPLY" = 1 ]; then
    echo "    running spicetify apply (Spotify restarts)"
    spicetify apply
  else
    echo "    NOTE: run 'spicetify apply' (restarts Spotify) or './install.sh --apply' to activate it."
  fi
fi

echo "Done."
