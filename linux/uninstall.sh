#!/bin/sh
# Remove claude-dots: the panel applet, the watcher and its files.
set -eu

DIR=${DOTS_DIR:-$HOME/.local/share/dots}
ID=${DOTS_ID:-dots}
P=/org/mate/panel

list=$(dconf read $P/general/object-id-list)
new=$(printf '%s' "$list" | sed "s/, '$ID'//; s/'$ID', //; s/\['$ID'\]/@as []/")
[ "$new" = "$list" ] || dconf write $P/general/object-id-list "$new"
dconf reset -f "$P/objects/$ID/"
pkill -x dotsd 2>/dev/null || true
rm -rf "$DIR" "${XDG_RUNTIME_DIR:-/tmp}/dots"
echo "claude-dots removed."
