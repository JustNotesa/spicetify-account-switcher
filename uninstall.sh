#!/bin/bash
# Removes the Spotify multi-account switcher v2.
#
#   ./uninstall.sh           remove helper + extension, KEEP data (profiles.json, logs)
#   ./uninstall.sh --purge   also delete ~/Library/Application Support/spotify-multiaccount
#   ./uninstall.sh --apply   additionally run `spicetify apply` (restarts Spotify!)
#
# The Spotify profiles themselves (~/Library/Application Support/Spotify-<id>) are never
# touched; they belong to Spotify. Remove unwanted ones in the menu before uninstalling.
set -euo pipefail

PURGE=0
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --apply) APPLY=1 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

LABEL="com.spotify-multiaccount.switcher"
V1_LABEL="com.spotify-multiaccount.helper"
MA_DIR="$HOME/Library/Application Support/spotify-multiaccount"
AGENTS_DIR="$HOME/Library/LaunchAgents"
DOMAIN="gui/$(id -u)"

echo "==> Helper"
for label in "$LABEL" "$V1_LABEL"; do
  launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
  rm -f "$AGENTS_DIR/$label.plist"
done
rm -rf "$MA_DIR/bin"
echo "    LaunchAgent removed"

echo "==> Spicetify extension"
if command -v spicetify >/dev/null 2>&1; then
  CONFIG_FILE="$(spicetify -c)"
  EXT_DIR="$(dirname "$CONFIG_FILE")/Extensions"
  current_list="$(sed -n 's/^[[:space:]]*extensions[[:space:]]*=[[:space:]]*//p' "$CONFIG_FILE" | head -n 1)"
  case "|$current_list|" in
    *"|accountSwitcher.js|"*) spicetify config extensions accountSwitcher.js-
                              echo "    removed from Spicetify config" ;;
    *) echo "    not listed in Spicetify config" ;;
  esac
  rm -f "$EXT_DIR/accountSwitcher.js"
  if [ "$APPLY" = 1 ]; then
    echo "    running spicetify apply (Spotify restarts)"
    spicetify apply
  else
    echo "    NOTE: run 'spicetify apply' (restarts Spotify) to unload the extension from Spotify."
  fi
else
  echo "    spicetify not found - skipped"
fi

if [ "$PURGE" = 1 ]; then
  rm -rf "$MA_DIR"
  echo "==> Deleted $MA_DIR"
else
  echo "==> Data kept in $MA_DIR (use --purge to delete)"
fi
echo "Spotify profiles (Spotify-<id>) were not touched."
echo "Done."
