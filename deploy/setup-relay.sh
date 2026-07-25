#!/usr/bin/env bash
# Relay-Engine-Setup (Baustein internet-hosting): installiert/aktualisiert das
# spring-dedicated-Paket (Binary + base/) fuer den Relay-Manager auf dem VPS.
#
# Quelle des Pakets ist das GitHub-Release-Asset recoil-linux-dedicated.tar.gz
# aus DipsitterUG/Conatus (Workflow "Linux Dedicated Engine Release"). Das Repo
# ist privat -> entweder Datei vorher per scp hochladen ODER Token mitgeben:
#
#   sudo bash deploy/setup-relay.sh /pfad/zu/recoil-linux-dedicated.tar.gz
#   sudo CONATUS_GH_TOKEN=ghp_... bash deploy/setup-relay.sh
#
# Idempotent: erneutes Ausfuehren ersetzt die Engine atomar (tmp + mv) und
# startet den Map-Server-Service neu (der Manager liest das Verzeichnis lazy).
set -euo pipefail

REPO="DipsitterUG/Conatus"
RELEASE_TAG="${CONATUS_RELAY_ENGINE_TAG:-engine-linux-dedicated-current}"
ASSET_NAME="recoil-linux-dedicated.tar.gz"
ENGINE_DIR="/opt/conatus-relay/engine"
RUN_DIR="/srv/conatus-maps/relay"
ENV_FILE="/etc/conatus-map-server.env"
TARBALL="${1:-}"

if [[ $EUID -ne 0 ]]; then
	echo "Bitte als root ausfuehren (sudo bash deploy/setup-relay.sh)" >&2
	exit 1
fi

workdir="$(mktemp -d)"
trap 'rm -rf "$workdir"' EXIT

if [[ -z "$TARBALL" ]]; then
	token="${CONATUS_GH_TOKEN:-}"
	if [[ -z "$token" ]]; then
		echo "Kein Tarball-Pfad und kein CONATUS_GH_TOKEN -- eine Quelle wird gebraucht." >&2
		exit 1
	fi
	echo "Lade $ASSET_NAME aus $REPO@$RELEASE_TAG ..."
	asset_id=$(curl -sf -H "Authorization: Bearer $token" \
		"https://api.github.com/repos/$REPO/releases/tags/$RELEASE_TAG" \
		| python3 -c "import json,sys; assets=json.load(sys.stdin)['assets']; print(next(a['id'] for a in assets if a['name']=='$ASSET_NAME'))") \
		|| { echo "Release/Asset nicht gefunden ($RELEASE_TAG / $ASSET_NAME)" >&2; exit 1; }
	TARBALL="$workdir/$ASSET_NAME"
	curl -sfL -H "Authorization: Bearer $token" -H "Accept: application/octet-stream" \
		-o "$TARBALL" "https://api.github.com/repos/$REPO/releases/assets/$asset_id" \
		|| { echo "Asset-Download fehlgeschlagen" >&2; exit 1; }
fi

[[ -s "$TARBALL" ]] || { echo "Tarball fehlt/leer: $TARBALL" >&2; exit 1; }

echo "Entpacke nach $ENGINE_DIR ..."
stage="$workdir/engine"
mkdir -p "$stage"
tar -xzf "$TARBALL" -C "$stage"
[[ -f "$stage/spring-dedicated" ]] || { echo "spring-dedicated fehlt im Tarball" >&2; exit 1; }
[[ -d "$stage/base" ]] || { echo "base/ fehlt im Tarball (Engine kann Scripts sonst nicht parsen)" >&2; exit 1; }
chmod +x "$stage/spring-dedicated"

mkdir -p "$(dirname "$ENGINE_DIR")"
rm -rf "$ENGINE_DIR.new"
mv "$stage" "$ENGINE_DIR.new"
rm -rf "$ENGINE_DIR.old"
[[ -d "$ENGINE_DIR" ]] && mv "$ENGINE_DIR" "$ENGINE_DIR.old"
mv "$ENGINE_DIR.new" "$ENGINE_DIR"
rm -rf "$ENGINE_DIR.old"

mkdir -p "$RUN_DIR"
chown -R conatus:conatus "$RUN_DIR"

# UDP-Ports der Relay-Sessions freigeben (falls ufw aktiv).
ports="8452-8461"
[[ -f "$ENV_FILE" ]] && grep -q '^CONATUS_RELAY_PORTS=' "$ENV_FILE" \
	&& ports="$(grep '^CONATUS_RELAY_PORTS=' "$ENV_FILE" | cut -d= -f2)"
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
	ufw allow "${ports/-/:}/udp"
fi

# Service-Unit auf aktuellen Repo-Stand bringen (Relay-Args) + Neustart.
cp /opt/conatus-map-server/deploy/conatus-map-server.service /etc/systemd/system/
systemctl daemon-reload
systemctl restart conatus-map-server

echo ""
echo "Fertig. Pruefen:"
echo "  systemctl status conatus-map-server"
echo "  curl http://127.0.0.1:\$(grep CONATUS_PORT $ENV_FILE | cut -d= -f2)/\$(grep CONATUS_MAP_SECRET $ENV_FILE | cut -d= -f2)/relay/health"
echo "  -> erwartet: dedicated_ready=true"
