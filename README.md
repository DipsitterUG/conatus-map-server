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
| `/snapshots/<cell_id>` | Launcher/Uploader | `GET`/`PUT` eines Zell-Snapshots (`text/plain`, last-writer-wins) |
| `/presence/<cell_id>` | Launcher/Uploader | `GET`/`PUT` aktuelle Host-IP/Port mit kurzer TTL |
| `/arrivals/<cell_id>` | Launcher (Zellwechsel) | `PUT` haengt EINE Arrival-Zeile an (`text/plain`), Antwort enthaelt die vom Server vergebene `id`; `GET` liefert alle offenen Arrivals (`{cell_id, count, arrivals:[{id, line}]}`), aeltestes zuerst |
| `/arrivals/<cell_id>/<id>` | Launcher (Zellwechsel) | `DELETE` quittiert genau diesen Eintrag nach erfolgtem Spawn — idempotent, unbekannte/schon quittierte `id` ist Erfolg |
| `/logs` | Diagnose | `GET` Index: `{player: [{run_id, kinds}, ...]}`, neueste zuerst |
| `/logs/<player>/<run_id>/<kind>` | Launcher/Diagnose | `GET`/`PUT` Log-Datei (`kind` = `launcher` \| `engine`), last-writer-wins pro Run, nur die letzten 3 Runs pro Spieler bleiben erhalten |
| `/relay/health` | Launcher (Status-Indikator) | `GET` `{ok, dedicated_ready, active_sessions, ports_free}` |
| `/relay/sessions` | Diagnose | `GET` Liste laufender Relay-Sessions |
| `/relay/sessions/<cell_id>` | Launcher (internet-hosting) | `PUT` Startscript (Marker `HostPort=0;`) -> spawnt `spring-dedicated`, Antwort mit UDP-Port; `DELETE` stoppt |

Mit gesetztem Secret liegen alle Pfade unter `/<secret>/…`; Clients tragen
nur die Basis-URL inkl. Secret ein (`map-server.txt`), die Endpunkte werden
angehaengt.

**Pfad-Segmente**: `<cell_id>`, `<player>`, `<run_id>` und `<id>` sind
Whitelist-gefiltert (`[A-Za-z0-9_.-]`) und duerfen **nicht mit `.` beginnen** —
sonst zeigen `.` und `..` aus dem Zielverzeichnis heraus. Verstoss = `400`.
Zusaetzlich prueft das Log-Pruning vor jedem Loeschen, dass sein Ziel ein
direktes Kind von `logs/` ist (zwei unabhaengige Schranken, weil hier
`rmtree` laeuft).

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

### Arrivals (Zellwechsel)

Ein Arrival ist eine Einheit, die von Zelle A nach Zelle B wechselt. Der Kanal
ist bewusst **append-only und getrennt vom Snapshot**: laege das Arrival wie
frueher in einer Sektion des Ganzdatei-Snapshots, wuerde der Launcher von A
mit seiner veralteten Kopie von `conatus_cell_B.txt` den laufenden Fortschritt
von B ueberschreiben (last-writer-wins).

- Ablauf: A `PUT`tet die Zeile nach `/arrivals/B` → Server vergibt die `id`;
  B holt beim Start per `GET /arrivals/B` alle offenen Eintraege und quittiert
  jeden nach erfolgtem Spawn mit `DELETE /arrivals/B/<id>`. Zustellgarantie:
  ohne Quittung bleibt der Eintrag liegen (auch ueber Serverneustarts).
- Datei-Layout: `arrivals/<cell>/<id>.txt`, eine Datei pro Eintrag — dadurch
  koennen zwei Nachbarzellen gleichzeitig in dieselbe Zelle schreiben, ohne
  sich zu ueberschreiben.
- `id`: Nanosekunden-Zeitstempel mit fester Breite, unter Lock strikt
  hochgezaehlt → monoton, lexikografisch sortierbar, kollisionsfrei auch bei
  zwei Uploads in derselben Sekunde.
- Grenzen: `MAX_ARRIVAL_LINE_BYTES` = 4 KiB pro Zeile,
  `MAX_ARRIVALS_PER_CELL` = 512 offene Eintraege pro Zelle. Ist der Deckel
  erreicht, antwortet der Server `507` statt den aeltesten Eintrag zu
  verwerfen (ein Arrival ist Spieler-Besitz; eine Absage ist reparierbar, ein
  stiller Verlust nicht).

### Relay (internet-hosting)

Der Relay-Manager spawnt pro MapCell einen `spring-dedicated`-Prozess
(reduced mode: keine Spiel-/Map-Archive noetig, `MapHash`/`ModHash` kommen im
Startscript). Engine-Paket installieren/aktualisieren:

```bash
sudo bash /opt/conatus-map-server/deploy/setup-relay.sh /pfad/zu/recoil-linux-dedicated.tar.gz
# oder mit GitHub-Token direkt vom Release engine-linux-dedicated-current:
sudo CONATUS_GH_TOKEN=... bash /opt/conatus-map-server/deploy/setup-relay.sh
```

Ohne Engine-Paket meldet `/relay/health` `dedicated_ready=false` (Indikator
auf den Clients wird rot). Die UDP-Session-Ports (Default 8452-8461) muessen
eingehend offen sein; das HTTP-Secret schuetzt nur die Manager-API, die
UDP-Ports selbst sind offen (MVP-Grenze, wie bisher beim Spieler-Host).

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
Snapshot-Sync ist bewusst MVP-einfach: last-writer-wins, keine Historie, keine
Merge-Logik, Presence nur als TTL-Hinweis statt Lease. Logs genauso einfach:
kein Auth ueber den Secret-Pfad hinaus, keine Redaction -- nur fuer den
privaten Freundeskreis-Betrieb gedacht.
