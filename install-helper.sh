#!/bin/bash
# One-line installer for the helper of the Spicetify Account Switcher (macOS).
# For users who installed the extension from the Spicetify Marketplace:
#
#   curl -fsSL https://raw.githubusercontent.com/JustNotesa/spicetify-account-switcher/main/install-helper.sh | bash
#
# Remove again:
#
#   curl -fsSL https://raw.githubusercontent.com/JustNotesa/spicetify-account-switcher/main/install-helper.sh | bash -s -- --uninstall
#
# It installs only the helper (a small Python script, standard library only) and a LaunchAgent
# that lets macOS start it on demand (socket activation on 127.0.0.1:48655). Nothing runs in
# the background until you use the switcher, and it exits again after 60 s idle.
set -euo pipefail

BASE_URL="https://raw.githubusercontent.com/JustNotesa/spicetify-account-switcher/main"
LABEL="com.spotify-multiaccount.switcher"
MA_DIR="$HOME/Library/Application Support/spotify-multiaccount"
BIN_DIR="$MA_DIR/bin"
SCRIPT="$BIN_DIR/spotify_accounts_helper.py"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PORT=48655

if [ "$(uname -s)" != "Darwin" ]; then
  echo "This helper is for macOS only." >&2
  exit 1
fi

bootout_wait() {
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1 || break
    sleep 0.5
  done
}

if [ "${1:-}" = "--uninstall" ]; then
  bootout_wait
  rm -f "$PLIST"
  rm -rf "$BIN_DIR"
  echo "Helper removed. (Your Spotify profiles were not touched.)"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading helper ..."
curl -fsSL "$BASE_URL/helper/spotify_accounts_helper.py" -o "$TMP/helper.py"
curl -fsSL "$BASE_URL/helper/$LABEL.plist" -o "$TMP/helper.plist"
/usr/bin/python3 -c 'import sys; compile(open(sys.argv[1]).read(), sys.argv[1], "exec")' "$TMP/helper.py"

mkdir -p "$BIN_DIR" "$HOME/Library/LaunchAgents"
chmod 700 "$MA_DIR"
install -m 644 "$TMP/helper.py" "$SCRIPT"

/usr/bin/python3 - "$TMP/helper.plist" "$PLIST" "$SCRIPT" "$MA_DIR" <<'PY'
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

bootout_wait
launchctl bootstrap "$DOMAIN" "$PLIST"

for _ in $(seq 1 20); do
  if curl -fsS --max-time 3 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "Done. The account switcher in Spotify is ready - open its menu again."
    exit 0
  fi
  sleep 0.5
done
echo "Installed, but the helper does not answer yet. See $MA_DIR/helper.log" >&2
exit 1
