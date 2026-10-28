#!/bin/sh
# Install claude-dots: copy the watcher to ~/.local/share/dots and add a MATE
# "Command" applet to the top panel, right after the items on its left side.
# Safe to run again: it updates the files and leaves an existing applet alone.
#
#   DOTS_PANEL=<panel id>      another panel (ids: dconf read /org/mate/panel/general/toplevel-id-list)
#   DOTS_POSITION=<pixels>     another position on that panel
set -eu

DIR=${DOTS_DIR:-$HOME/.local/share/dots}
ID=${DOTS_ID:-dots}
SRC=$(cd "$(dirname "$0")" && pwd)
P=/org/mate/panel

if ! command -v dconf >/dev/null || [ -z "$(dconf read $P/general/object-id-list)" ]; then
    echo "No MATE panel configuration found: claude-dots needs the MATE panel." >&2
    exit 1
fi
if [ ! -e /usr/share/mate-panel/applets/org.mate.applets.CommandApplet.mate-panel-applet ]; then
    echo "The MATE Command applet is missing: install the mate-applets package." >&2
    exit 1
fi
command -v xprop >/dev/null ||
    echo "Note: xprop not found (package x11-utils), so a ◆ won't clear when you focus its window." >&2

mkdir -p "$DIR"
install -m 755 "$SRC/dots" "$SRC/dotsd.py" "$DIR/"
[ -e "$DIR/panel-backup.dconf" ] || dconf dump $P/ > "$DIR/panel-backup.dconf"

ids() { dconf read "$1" | tr -d "[],'" | sed 's/^@as //'; }

list=$(dconf read $P/general/object-id-list)
case "$list" in
*"'$ID'"*)
    echo "The panel already has the applet; files updated."
    ;;
*)
    panel=${DOTS_PANEL:-}
    if [ -z "$panel" ]; then
        for t in $(ids $P/general/toplevel-id-list); do
            [ -n "$panel" ] || panel=$t
            if [ "$(dconf read "$P/toplevels/$t/orientation")" = "'top'" ]; then panel=$t; break; fi
        done
    fi
    [ -n "$panel" ] || { echo "No MATE panel found." >&2; exit 1; }

    pos=${DOTS_POSITION:-}
    if [ -z "$pos" ]; then
        pos=0
        for o in $(ids $P/general/object-id-list); do
            [ "$(dconf read "$P/objects/$o/toplevel-id")" = "'$panel'" ] || continue
            [ "$(dconf read "$P/objects/$o/panel-right-stick")" != true ] || continue
            p=$(dconf read "$P/objects/$o/position")
            p=${p:-0}
            [ "$p" -lt "$pos" ] || pos=$((p + 10))
        done
    fi

    o=$P/objects/$ID
    dconf write "$o/prefs/command" "'\"$DIR/dots\"'"
    dconf write "$o/prefs/interval" 1
    dconf write "$o/prefs/width" 60
    dconf write "$o/prefs/show-icon" false
    dconf write "$o/object-type" "'applet'"
    dconf write "$o/applet-iid" "'CommandAppletFactory::CommandApplet'"
    dconf write "$o/toplevel-id" "'$panel'"
    dconf write "$o/position" "$pos"
    dconf write "$o/panel-right-stick" false
    dconf write "$o/locked" true
    case "$list" in
    *"[]") new="['$ID']" ;;
    *) new=$(printf '%s' "$list" | sed "s/]\$/, '$ID']/") ;;
    esac
    dconf write $P/general/object-id-list "$new"
    echo "Added the applet to the '$panel' panel."
    ;;
esac

pkill -x dotsd 2>/dev/null || true  # the applet restarts it with the new files within a second
echo "Installed in $DIR. Which dot is which: $DIR/dotsd.py --legend"
