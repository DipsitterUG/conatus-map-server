# conatus-map-server

Karten-Download-Server fuer Conatus (Recoil/Spring-RTS): liefert `.sd7`-Map-
Archive an den Conatus-Launcher und den Engine-Downloader (pr-downloader).
Dies ist die **Deployment-Quelle fuer den VPS** — stdlib-only Python, keine
Abhaengigkeiten. Die Publish-/Sync-Werkzeuge (Karten packen, Index bauen,
rsync) leben im Studio-Repo (`conatus-studio`, Modul `map_server`).

## Endpunkte

| Pfad | Nutzer | Inhalt |
|------|--------|--------|
| `/maps.json` | Launcher (Start-Sync) | `[{springname, filename, size, md5}]` |
| `/json.php?category=map&springname=…` | Engine pr-downloader (`PRD_HTTP_SEARCH_URL`) | springfiles-Format (Vertrag: RecoilEngine `HttpDownloader.cpp` `ParseResult`) |
| `/maps/<datei>` | beide | statisches Archiv |

Mit gesetztem Secret liegen alle Pfade unter `/<secret>/…`; Clients tragen
nur die Basis-URL inkl. Secret ein (`map-server.txt`), die Endpunkte werden
angehaengt.

## Betrieb (VPS)

```bash
git clone https://github.com/DipsitterUG/conatus-map-server.git /opt/conatus-map-server
sudo bash /opt/conatus-map-server/deploy/setup.sh
```

`setup.sh` legt den `conatus`-Nutzer, `/srv/conatus-maps/maps`,
`/etc/conatus-map-server.env` (inkl. generiertem Secret) sowie die
systemd-Units an und startet Server + **Self-Update-Timer** (zieht alle
5 Minuten `origin/main` und restartet nur bei Aenderung — Push hier im Repo
= VPS aktualisiert sich selbst, wie der Content-Kanal der Spiel-PCs).

Karten kommen **nicht** ueber dieses Repo (grosse Binaerdateien), sondern
per rsync vom Laptop:

```bash
# im Studio-Repo:
PYTHONPATH=src python3 -m conatus_studio.cli sync-map-server \
    conatus@<host>:/srv/conatus-maps/maps
```

Der Server liest ausschliesslich das mitgespiegelte `maps.json`
(Index-Datei-Modus) und uebernimmt Aenderungen ohne Neustart.

## Lokal testen

```bash
python3 -m unittest discover -s tests
python3 -m map_server.server --maps-dir /pfad/zu/maps --port 8605
```

## Kontext

Teil des Conatus-Workspace (Projekt-Kopf: `conatus-studio`). Bausteine
`map-download-server` / `map-server-vps`; Betriebs-Doku im Studio unter
`docs/map-server.md`. Bewusste MVP-Grenzen: Secret-Pfad statt echter Auth,
HTTP ohne TLS (Upgrade-Pfad: Caddy/nginx davor + `CONATUS_BASE_URL`).
