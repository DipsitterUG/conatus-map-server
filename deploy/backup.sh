#!/usr/bin/env bash
# Taegliches Backup des Weltzustands (snapshots/ + arrivals/) auf dem VPS.
#
# Warum: der Weltzustand ist das einzige Unwiederbringliche auf diesem Server.
# Karten kommen per rsync vom Laptop nach, Code aus Git, Logs sind Diagnose --
# aber ein geloeschter oder ueberschriebener Snapshot ist der Spielfortschritt.
# Genau das stand bis 2026-08-01 hinter der /logs-Traversal-Luecke auf dem Spiel.
#
# Bewusst simpel: tar.gz, taeglich, lokale Aufbewahrung. Kein Off-Site-Ziel --
# das waere ein eigener Baustein (Zugangsdaten, Kosten, Wiederherstellungstest).
set -euo pipefail

SRC_DIR="${CONATUS_STATE_DIR:-/srv/conatus-maps}"
DEST_DIR="${CONATUS_BACKUP_DIR:-/var/backups/conatus-map-server}"
KEEP="${CONATUS_BACKUP_KEEP:-14}"

mkdir -p "$DEST_DIR"
chmod 700 "$DEST_DIR"

stamp="$(date -u +%Y%m%d-%H%M%S)"
archive="$DEST_DIR/world-$stamp.tar.gz"

# --ignore-failed-read: ein Snapshot, der genau waehrend des Laufs ersetzt wird,
# darf das ganze Backup nicht scheitern lassen (atomic_write ersetzt Dateien).
present=()
for name in snapshots arrivals; do
	[[ -d "$SRC_DIR/$name" ]] && present+=("$name")
done

if [[ ${#present[@]} -eq 0 ]]; then
	echo "conatus-backup: nichts zu sichern in $SRC_DIR" >&2
	exit 0
fi

tar --ignore-failed-read -czf "$archive" -C "$SRC_DIR" "${present[@]}"
chmod 600 "$archive"

# Aufbewahrung: die juengsten $KEEP Archive behalten.
mapfile -t stale < <(ls -1t "$DEST_DIR"/world-*.tar.gz 2>/dev/null | tail -n "+$((KEEP + 1))")
for old in "${stale[@]:-}"; do
	[[ -n "$old" ]] && rm -f "$old"
done

echo "conatus-backup: $archive ($(du -h "$archive" | cut -f1)), ${#present[@]} Verzeichnisse"
