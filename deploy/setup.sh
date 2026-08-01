#!/usr/bin/env bash
# Einmal-Setup auf dem VPS (Debian/Ubuntu, als root ausfuehren):
#   git clone https://github.com/DipsitterUG/conatus-map-server.git /opt/conatus-map-server
#   sudo bash /opt/conatus-map-server/deploy/setup.sh
# Idempotent: erneutes Ausfuehren repariert Units/Rechte, ueberschreibt aber
# weder /etc/conatus-map-server.env noch vorhandene Karten.
set -euo pipefail

REPO_DIR="/opt/conatus-map-server"
MAPS_DIR="/srv/conatus-maps/maps"
SNAPSHOTS_DIR="/srv/conatus-maps/snapshots"
LOGS_DIR="/srv/conatus-maps/logs"
ARRIVALS_DIR="/srv/conatus-maps/arrivals"
ENV_FILE="/etc/conatus-map-server.env"
# Zugaenge je PC (DR-035): "<token> <name>" je Zeile. Widerruf = Zeile
# loeschen, der Server laedt die Datei ohne Neustart nach.
TOKENS_FILE="/etc/conatus-map-server.tokens"

if [[ $EUID -ne 0 ]]; then
	echo "Bitte als root ausfuehren (sudo bash deploy/setup.sh)" >&2
	exit 1
fi

command -v git >/dev/null || { apt-get update && apt-get install -y git; }
command -v python3 >/dev/null || apt-get install -y python3
command -v rsync >/dev/null || apt-get install -y rsync

# Service-Nutzer (login-los); rsync vom Laptop laeuft ueber diesen Account.
id -u conatus >/dev/null 2>&1 || adduser --disabled-password --gecos "" conatus

mkdir -p "$MAPS_DIR" "$SNAPSHOTS_DIR" "$LOGS_DIR" "$ARRIVALS_DIR"
chown -R conatus:conatus /srv/conatus-maps

if [[ ! -d "$REPO_DIR/.git" ]]; then
	echo "Repo fehlt unter $REPO_DIR -- erst clonen (siehe Kopfzeile)." >&2
	exit 1
fi

if [[ ! -f "$ENV_FILE" ]]; then
	cat > "$ENV_FILE" <<EOF
CONATUS_MAPS_DIR=$MAPS_DIR
CONATUS_SNAPSHOTS_DIR=$SNAPSHOTS_DIR
CONATUS_LOGS_DIR=$LOGS_DIR
CONATUS_ARRIVALS_DIR=$ARRIVALS_DIR
CONATUS_PORT=8605
CONATUS_MAP_SECRET=$(openssl rand -hex 16)
CONATUS_BASE_URL=
CONATUS_MAP_TOKENS_FILE=$TOKENS_FILE
# 'all' = Uebergangsfenster (Pfad-Prefix oeffnet weiter alles).
# Nach dem Launcher-Rollout auf BEIDEN PCs auf 'maps' stellen: dann oeffnet der
# Prefix nur noch die Karten-Endpunkte, alles andere braucht ein Token.
CONATUS_LEGACY_SECRET_SCOPE=all
EOF
	chmod 600 "$ENV_FILE"
else
	grep -q '^CONATUS_SNAPSHOTS_DIR=' "$ENV_FILE" \
		|| printf '\nCONATUS_SNAPSHOTS_DIR=%s\n' "$SNAPSHOTS_DIR" >> "$ENV_FILE"
	grep -q '^CONATUS_LOGS_DIR=' "$ENV_FILE" \
		|| printf '\nCONATUS_LOGS_DIR=%s\n' "$LOGS_DIR" >> "$ENV_FILE"
	grep -q '^CONATUS_ARRIVALS_DIR=' "$ENV_FILE" \
		|| printf '\nCONATUS_ARRIVALS_DIR=%s\n' "$ARRIVALS_DIR" >> "$ENV_FILE"
	grep -q '^CONATUS_MAP_TOKENS_FILE=' "$ENV_FILE" \
		|| printf '\nCONATUS_MAP_TOKENS_FILE=%s\n' "$TOKENS_FILE" >> "$ENV_FILE"
	grep -q '^CONATUS_LEGACY_SECRET_SCOPE=' "$ENV_FILE" \
		|| printf '\nCONATUS_LEGACY_SECRET_SCOPE=all\n' >> "$ENV_FILE"
fi

# Token-Datei: je PC eine Zeile. Beim ersten Lauf zwei Zugaenge vorgeneriert,
# danach nie wieder angefasst (sonst wuerde jedes setup.sh alle PCs aussperren).
if [[ ! -f "$TOKENS_FILE" ]]; then
	cat > "$TOKENS_FILE" <<EOF
# Conatus Map-Server: Zugaenge. Eine Zeile je PC -- "<token> <name>".
# Widerruf: Zeile loeschen oder auskommentieren (wirkt sofort, ohne Neustart).
# Neuer Zugang: openssl rand -hex 24, Zeile anhaengen, Token auf den PC geben
# (dort %LOCALAPPDATA%/Conatus/map-server-token.txt).
$(openssl rand -hex 24) PC1
$(openssl rand -hex 24) PC2
EOF
fi
# Lesbar nur fuer root und den Service-Nutzer.
chown root:conatus "$TOKENS_FILE"
chmod 640 "$TOKENS_FILE"

cp "$REPO_DIR/deploy/conatus-map-server.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/conatus-map-server-update.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/conatus-map-server-update.timer" /etc/systemd/system/
cp "$REPO_DIR/deploy/conatus-map-server-backup.service" /etc/systemd/system/
cp "$REPO_DIR/deploy/conatus-map-server-backup.timer" /etc/systemd/system/
chmod +x "$REPO_DIR/deploy/update.sh" "$REPO_DIR/deploy/backup.sh"
systemctl daemon-reload
systemctl enable --now conatus-map-server
systemctl enable --now conatus-map-server-update.timer
systemctl enable --now conatus-map-server-backup.timer

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
	ufw allow "$(grep CONATUS_PORT "$ENV_FILE" | cut -d= -f2)/tcp"
fi

echo ""
echo "Fertig. Naechste Schritte:"
echo "  1) SSH-Pubkey des Laptops nach /home/conatus/.ssh/authorized_keys"
echo "     (fuer sync-map-server per rsync)"
echo "  2) Laptop: sync-map-server conatus@<host>:$MAPS_DIR"
echo "  3) Client-URL fuer map-server.txt (Karten-Download-Zugang):"
echo "     http://<host>:$(grep CONATUS_PORT "$ENV_FILE" | cut -d= -f2)/\$CONATUS_MAP_SECRET"
echo "     -> Wert: sudo grep CONATUS_MAP_SECRET $ENV_FILE"
echo "  4) Token je PC aus $TOKENS_FILE holen und dort ablegen als"
echo "     %LOCALAPPDATA%\\Conatus\\map-server-token.txt (nur das Token, eine Zeile)."
echo "     -> sudo cat $TOKENS_FILE"
echo "  5) Erst wenn BEIDE PCs den neuen Launcher haben:"
echo "     CONATUS_LEGACY_SECRET_SCOPE=maps in $ENV_FILE, dann"
echo "     systemctl restart conatus-map-server"
echo "  Status:  systemctl status conatus-map-server"
echo "  Backup:  systemctl list-timers conatus-map-server-backup.timer"
