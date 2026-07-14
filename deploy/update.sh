#!/usr/bin/env bash
# Self-Update fuer den VPS: origin/main holen, bei Aenderung hart nachziehen
# und den Server restarten. Laeuft als root via systemd-Timer (Repo ist
# public -> kein Token noetig). Lokale Aenderungen im Checkout gehen bewusst
# verloren -- /opt/conatus-map-server ist reine Deployment-Kopie.
set -euo pipefail

cd /opt/conatus-map-server
git fetch --quiet origin main
local_rev="$(git rev-parse HEAD)"
remote_rev="$(git rev-parse origin/main)"

if [[ "$local_rev" == "$remote_rev" ]]; then
	exit 0
fi

git reset --hard --quiet origin/main
systemctl restart conatus-map-server
echo "conatus-map-server aktualisiert: ${local_rev:0:9} -> ${remote_rev:0:9}"
