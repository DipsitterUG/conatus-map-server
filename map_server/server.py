from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from map_server.index import INDEX_FILENAME, MapEntry, MapIndex
from map_server.relay import DEFAULT_PORT_RANGE, MAX_SCRIPT_BYTES, RelayManager

DEFAULT_PORT = 8605
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
PRESENCE_TTL_SECONDS = 90
MAX_LOG_BYTES = 8 * 1024 * 1024
LOG_RUNS_PER_PLAYER = 3
LOG_KINDS = ("launcher", "engine")
# Arrivals (Zellwechsel einer Einheit): eine Zeile pro Eintrag, deshalb klein.
MAX_ARRIVAL_LINE_BYTES = 4 * 1024
# Deckel pro Zelle. Entscheidung: bei Ueberschreitung 507 statt "aeltesten
# verwerfen" -- ein Arrival ist Spieler-Besitz, und der Absender loescht seine
# Einheit erst nach erfolgreichem PUT. Eine Absage ist damit reparierbar (der
# Absender behaelt die Einheit und versucht es spaeter erneut), ein stilles
# Verwerfen des aeltesten Eintrags waere dagegen ein echter Verlust.
MAX_ARRIVALS_PER_CELL = 512
# Feste Breite der id: time_ns() hat heute 19 Stellen, 20 Stellen halten die
# lexikografische Sortierung == numerische Sortierung bis weit ins Jahr 2286.
ARRIVAL_ID_DIGITS = 20

# --- Zugang (DR-035) -------------------------------------------------------
# Zwei Wege mit ABSICHTLICH unterschiedlicher Reichweite:
#
#   1. Bearer-Token im Authorization-Header. Pro PC handvergeben, serverseitig
#      einem Namen zugeordnet, einzeln widerrufbar (Zeile aus der Token-Datei
#      loeschen). Gilt fuer alle Endpunkte.
#   2. Pfad-Prefix (--secret). Der einzige Weg, den der Engine-Downloader gehen
#      KANN: pr-downloader setzt keine eigenen Auth-Header und kennt keine
#      Konfiguration dafuer (RecoilEngine, tools/pr-downloader/src/Downloader/
#      CurlWrapper.cpp AddHeader -- nur X-Prd-Retry-Num/Cache-Control/If-None-
#      Match; HttpDownloader.cpp getRequestUrl nimmt PRD_HTTP_SEARCH_URL roh).
#      Deshalb bleibt der Prefix dauerhaft bestehen -- aber nach dem
#      Uebergangsfenster nur noch fuer die Karten-Endpunkte.
#
# Genau diese Aufteilung ist der Kern von DR-035: der Prefix landet
# unvermeidbar im Engine-infolog (HttpDownloader.cpp LOG_ERROR "Error
# downloading %s") und damit in den hochgeladenen Logs. Wer ihn sieht, kommt
# danach trotzdem nicht mehr an den Weltzustand.
LEGACY_SCOPE_ALL = "all"    # Uebergangsfenster: Prefix oeffnet alles (Verhalten bis 2026-08-01)
LEGACY_SCOPE_MAPS = "maps"  # Zielzustand: Prefix oeffnet nur noch Karten-Downloads
LEGACY_SCOPES = (LEGACY_SCOPE_ALL, LEGACY_SCOPE_MAPS)
# Kurze Token sind kein Fehler (handvergeben), aber eine Warnung wert.
MIN_TOKEN_LENGTH = 24


def is_map_download_path(path: str) -> bool:
    """Endpunkte, die der Engine-Downloader ohne Header erreichen muss."""
    return path in ("/maps.json", "/json.php") or path.startswith("/maps/")


def parse_tokens(text: str) -> dict[str, str]:
    """Token-Datei -> {token: name}. Eine Zeile je Zugang: "<token> <name>".

    `#` leitet einen Kommentar ein, Leerzeilen werden ignoriert. Der Name ist
    die serverseitige Zuordnung "Schluessel -> wer" (DR-035); er taucht nirgends
    im Protokoll auf, sondern sagt dem Betreiber, welche Zeile er loeschen muss,
    um GENAU diesen PC auszusperren.
    """
    tokens: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        token = parts[0]
        name = parts[1].strip() if len(parts) > 1 else "unnamed"
        tokens[token] = name
    return tokens


class TokenStore:
    """Token-Datei mit Nachladen bei Aenderung.

    Widerrufen heisst: Zeile loeschen. Das muss ohne Serverneustart wirken,
    sonst kostet das Aussperren eines PCs eine Downtime fuer alle. Deshalb
    prueft jeder Request mtime+Groesse der Datei (ein stat(), wie MapIndex es
    fuer maps.json schon macht).

    Fail-closed: ist die Datei weg oder kaputt, gilt KEIN Token mehr -- nicht
    "alle duerfen". `enabled` haengt am konfigurierten Pfad, nicht am Inhalt.
    """

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._tokens: dict[str, str] = {}
        self._stamp: tuple[int, int] | None = None
        self._error = ""

    @property
    def enabled(self) -> bool:
        return self.path is not None

    def refresh(self) -> dict[str, str]:
        if self.path is None:
            return {}
        try:
            stat = self.path.stat()
            stamp = (stat.st_mtime_ns, stat.st_size)
        except OSError as error:
            self._tokens = {}
            self._stamp = None
            self._error = str(error)
            return {}
        if stamp != self._stamp:
            try:
                self._tokens = parse_tokens(self.path.read_text(encoding="utf-8"))
                self._error = ""
            except (OSError, UnicodeDecodeError) as error:
                self._tokens = {}
                self._error = str(error)
            self._stamp = stamp
        return self._tokens

    def client_for(self, presented: str) -> str | None:
        """Name hinter dem Token, oder None."""
        if not presented:
            return None
        for token, name in self.refresh().items():
            if hmac.compare_digest(token, presented):
                return name
        return None

    def describe(self) -> str:
        if self.path is None:
            return "aus (kein --tokens-file)"
        tokens = self.refresh()
        if self._error:
            return f"{self.path}: NICHT LESBAR ({self._error}) -- kein Token gilt"
        names = ", ".join(sorted(set(tokens.values()))) or "leer"
        return f"{self.path}: {len(tokens)} Zugaenge ({names})"


def springfiles_result(entry: MapEntry, public_base: str) -> dict[str, object]:
    """Ein Ergebnis-Objekt im springfiles-Format, wie pr-downloader es parst.

    Vertrag aus RecoilEngine tools/pr-downloader HttpDownloader.cpp
    ParseResult: Pflicht category/springname/filename/mirrors, optional
    md5 (aktiviert Hash-Pruefung) und size.
    """
    return {
        "category": "map",
        "springname": entry.springname,
        "filename": entry.filename,
        "mirrors": [f"{public_base}/maps/{quote(entry.filename)}"],
        "md5": entry.md5,
        "size": entry.size,
    }


_SAFE_CELL_ID = re.compile(r"^[A-Za-z0-9_.-]{1,96}$")
_SAFE_LOG_SEGMENT = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def safe_cell_id(value: str) -> str | None:
    if _SAFE_CELL_ID.fullmatch(value):
        return value
    return None


# Wer ein Segment als PFAD-BESTANDTEIL verwendet (Verzeichnis- oder Dateiname),
# darf sich nicht auf die Whitelist allein verlassen: sie erlaubt Punkte, weil
# Karten- und Dateinamen sie tragen ("nature.0.2", "infolog.txt") -- damit
# passen aber auch "." und "..", und die zeigen aus dem Zielverzeichnis heraus.
# Deshalb hier zentral der Punkt-Guard am Segment-Anfang.
#
# Vorfall 2026-08-01: safe_log_segment liess ".." durch, _log_path schrieb damit
# nach logs_dir/../<run>/ und _prune_old_runs raeumte anschliessend das
# ELTERNverzeichnis von logs/ auf -- also Snapshots, Arrivals und Maps.
def safe_log_segment(value: str) -> str | None:
    if _SAFE_LOG_SEGMENT.fullmatch(value) and not value.startswith("."):
        return value
    return None


def safe_dir_name(value: str) -> str | None:
    """Cell-id, die als Verzeichnisname dient (arrivals/<cell>, relay/<cell>).

    Anders als bei Snapshots/Presence faengt hier keine angehaengte Endung den
    Traversal ab, deshalb dieselbe Punkt-Regel wie bei den Log-Segmenten.
    """
    safe = safe_cell_id(value)
    if safe is None or safe.startswith("."):
        return None
    return safe


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp-{os.getpid()}-{time.time_ns()}")
    try:
        tmp.write_bytes(data)
        tmp.replace(path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass


def next_arrival_id(newest_existing: str | None, last_issued_ns: int) -> tuple[str, int]:
    """Naechste Arrival-id: monoton steigend und lexikografisch sortierbar.

    Strategie: Nanosekunden-Zeitstempel (time.time_ns) mit fester Breite
    ARRIVAL_ID_DIGITS, dadurch sortiert die Log-Retention-Regel (rein
    lexikografisch, siehe _prune_old_runs) auch hier korrekt. Zwei PUTs in
    derselben Sekunde koennen nicht kollidieren, weil die Aufloesung
    Nanosekunden ist UND der Wert unter dem arrivals_lock zusaetzlich immer
    strikt hochgezaehlt wird: gegen den letzten selbst vergebenen Wert
    (gleiche Nanosekunde bei zwei Threads) und gegen die neueste id, die schon
    auf Platte liegt (Serverneustart oder zurueckgestellte Uhr).
    """
    candidate = time.time_ns()
    if last_issued_ns >= candidate:
        candidate = last_issued_ns + 1
    if newest_existing:
        try:
            on_disk = int(newest_existing)
        except ValueError:
            on_disk = 0
        if on_disk >= candidate:
            candidate = on_disk + 1
    return f"{candidate:0{ARRIVAL_ID_DIGITS}d}", candidate


class MapServerHandler(BaseHTTPRequestHandler):
    # ThreadingHTTPServer-Attribute, von create_server gesetzt:
    #   self.server.map_index: MapIndex
    #   self.server.base_url: str | None   (oeffentliche Basis OHNE Secret-Anteil)
    #   self.server.secret: str | None     (Pfad-Prefix als Minimal-Zugriffsschutz)
    #   self.server.snapshots_dir: Path
    #   self.server.presence_dir: Path
    #   self.server.logs_dir: Path
    #   self.server.arrivals_dir: Path
    #   self.server.arrivals_lock: threading.Lock
    #   self.server.token_store: TokenStore
    #   self.server.legacy_secret_scope: str  (LEGACY_SCOPE_ALL | LEGACY_SCOPE_MAPS)

    protocol_version = "HTTP/1.1"

    # Wer diesen Request geschickt hat, aufgeloest aus dem Bearer-Token
    # ("Schluessel -> wer", DR-035). Absichtlich noch nirgends ausgegeben --
    # Zeitstempel und Identitaet in der Log-Zeile sind Baustein
    # diagnostics-log-platform.
    auth_client = ""

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        self._handle_request()

    def do_PUT(self) -> None:  # noqa: N802 (http.server API)
        self._handle_request()

    def do_DELETE(self) -> None:  # noqa: N802 (http.server API)
        self._handle_request()

    def _handle_request(self) -> None:
        try:
            self._route()
        except BrokenPipeError:
            pass
        except ValueError as error:
            self._send_json(400, {"error": str(error)})

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization") or ""
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer":
            return ""
        return value.strip()

    def _authorized(self, path: str, client: str | None, prefix_ok: bool) -> bool:
        """True = weiterrouten. Bei False ist die Antwort schon geschickt."""
        if client is not None:
            self.auth_client = client
            return True
        if not self.server.token_store.enabled:
            # Kein Token-Betrieb konfiguriert -> unveraendertes Verhalten
            # (Dev/LAN und der Stand vor DR-035).
            self.auth_client = "legacy-secret" if self.server.secret else "anonymous"
            return True
        if prefix_ok and self.server.secret and (
                self.server.legacy_secret_scope == LEGACY_SCOPE_ALL
                or is_map_download_path(path)):
            self.auth_client = "legacy-secret"
            return True
        self._send_json(401, {"error": "unauthorized"})
        return False

    def _route(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        # Token zuerst: ein gueltiges Token kommt auch OHNE Pfad-Prefix durch.
        # Damit braucht ein Werkzeug (oder ein spaeterer Account-Client) den
        # Prefix gar nicht erst zu kennen.
        client = self.server.token_store.client_for(self._bearer_token())
        prefix_ok = True
        secret = self.server.secret
        if secret:
            prefix = "/" + secret
            prefix_ok = path == prefix or path.startswith(prefix + "/")
            if prefix_ok:
                path = path[len(prefix):] or "/"
            elif client is None:
                # Tarnung wie bisher: ohne Prefix und ohne Token gibt es hier nichts.
                self._send_json(404, {"error": "not found"})
                return
        if not self._authorized(path, client, prefix_ok):
            return
        if path == "/maps.json":
            self._serve_maps_json()
            return
        if path == "/json.php":
            self._serve_search(parse_qs(url.query))
            return
        if path.startswith("/maps/"):
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return
            self._serve_map_file(unquote(path[len("/maps/"):]))
            return
        if path.startswith("/snapshots/"):
            self._route_snapshot(unquote(path[len("/snapshots/"):]))
            return
        if path.startswith("/presence/"):
            self._route_presence(unquote(path[len("/presence/"):]))
            return
        if path.startswith("/arrivals/"):
            self._route_arrivals(unquote(path[len("/arrivals/"):]))
            return
        if path == "/relay/health":
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return
            self._send_json(200, self.server.relay_manager.health())
            return
        if path == "/relay/sessions":
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return
            self._send_json(200, self.server.relay_manager.list_sessions())
            return
        if path.startswith("/relay/sessions/"):
            self._route_relay_session(unquote(path[len("/relay/sessions/"):]))
            return
        if path == "/logs":
            if self.command != "GET":
                self._send_json(405, {"error": "method not allowed"})
                return
            self._serve_log_index()
            return
        if path.startswith("/logs/"):
            self._route_log_entry(unquote(path[len("/logs/"):]))
            return
        if self.command != "GET":
            self._send_json(405, {"error": "method not allowed"})
            return
        self._send_json(404, {"error": "not found"})

    # Oeffentliche Basis fuer Mirror-URLs: --base-url (Betrieb hinter Domain/
    # Proxy/TLS) oder der Host-Header der Anfrage; Secret-Prefix kommt dazu,
    # damit die Mirror-Downloads durch denselben Schutz laufen.
    def _public_base(self) -> str:
        base = self.server.base_url
        if not base:
            host = self.headers.get("Host") or f"{self.server.server_name}:{self.server.server_port}"
            base = f"http://{host}"
        if self.server.secret:
            base += "/" + self.server.secret
        return base

    def _serve_maps_json(self) -> None:
        entries = self.server.map_index.refresh()
        self._send_json(200, [entry.to_dict() for entry in entries])

    def _serve_search(self, query: dict[str, list[str]]) -> None:
        category = query.get("category", ["map"])[0]
        springname = query.get("springname", [""])[0]
        if category != "map" or not springname:
            self._send_json(200, [])
            return
        entry = self.server.map_index.find(springname)
        if entry is None:
            self._send_json(200, [])
            return
        self._send_json(200, [springfiles_result(entry, self._public_base())])

    def _serve_map_file(self, filename: str) -> None:
        maps_dir: Path = self.server.map_index.maps_dir
        target = (maps_dir / filename).resolve()
        if target.parent != maps_dir.resolve() or not target.is_file():
            self._send_json(404, {"error": "unknown map file"})
            return
        size = target.stat().st_size
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.end_headers()
        with target.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                self.wfile.write(chunk)

    def _snapshot_path(self, cell_id: str) -> Path | None:
        safe = safe_cell_id(cell_id)
        if safe is None:
            return None
        return self.server.snapshots_dir / f"{safe}.txt"

    def _presence_path(self, cell_id: str) -> Path | None:
        safe = safe_cell_id(cell_id)
        if safe is None:
            return None
        return self.server.presence_dir / f"{safe}.json"

    def _read_body(self, limit: int) -> bytes:
        length_text = self.headers.get("Content-Length")
        if not length_text:
            raise ValueError("Content-Length required")
        try:
            length = int(length_text)
        except ValueError as error:
            raise ValueError("invalid Content-Length") from error
        if length < 0 or length > limit:
            raise ValueError("request body too large")
        return self.rfile.read(length)

    def _route_snapshot(self, cell_id: str) -> None:
        path = self._snapshot_path(cell_id)
        if path is None:
            self._send_json(400, {"error": "invalid cell id"})
            return
        if self.command == "GET":
            self._serve_snapshot(cell_id, path)
            return
        if self.command == "PUT":
            self._put_snapshot(cell_id, path)
            return
        self._send_json(405, {"error": "method not allowed"})

    def _serve_snapshot(self, cell_id: str, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "snapshot not found", "cell_id": cell_id})
            return
        stat = path.stat()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("X-Conatus-Cell-ID", cell_id)
        self.send_header("X-Conatus-Updated-At", str(int(stat.st_mtime)))
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                self.wfile.write(chunk)

    def _put_snapshot(self, cell_id: str, path: Path) -> None:
        body = self._read_body(MAX_SNAPSHOT_BYTES)
        if not body.strip():
            self._send_json(400, {"error": "empty snapshot", "cell_id": cell_id})
            return
        atomic_write(path, body)
        self._send_json(200, {
            "ok": True,
            "cell_id": cell_id,
            "size": len(body),
            "updated_at": int(path.stat().st_mtime),
        })

    def _route_presence(self, cell_id: str) -> None:
        path = self._presence_path(cell_id)
        if path is None:
            self._send_json(400, {"error": "invalid cell id"})
            return
        if self.command == "GET":
            self._serve_presence(cell_id, path)
            return
        if self.command == "PUT":
            self._put_presence(cell_id, path)
            return
        if self.command == "DELETE":
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            self._send_json(200, {"ok": True, "cell_id": cell_id, "hosted": False})
            return
        self._send_json(405, {"error": "method not allowed"})

    # "Wird diese Zelle gerade gespielt?" ist eine Frage ueber die SESSION, nicht
    # ueber den letzten Heartbeat. Der Heartbeat kommt von einem Spielclient und
    # ueberlebt ihn um bis zu PRESENCE_TTL_SECONDS -- wer in diesem Fenster die
    # Zelle betritt, wird auf eine tote Adresse geschickt (Symptom 3 der
    # 2-PC-Abnahme 2026-07-30). Der Server weiss es besser: die Engine beendet
    # den dedicated sofort, sobald der letzte Client weg ist ("No clients
    # connected, shutting down server", gemessen 2026-07-31).
    #
    # Deshalb in dieser Reihenfolge:
    #   1. laeuft eine Relay-Session -> gehostet, und IHR Port ist die Wahrheit
    #      (auch wenn der Heartbeat laengst abgelaufen ist -- der Host kann
    #      gegangen sein, waehrend ein Gast weiterspielt).
    #   2. kann dieser Server Relay-Sessions fahren und laeuft keine -> NICHT
    #      gehostet, egal wie frisch der Heartbeat ist. Ueber den Relay laeuft
    #      jedes Hosting, also gibt es ohne Session auch nichts zu betreten.
    #   3. ohne Relay-Faehigkeit (Dev/LAN) bleibt der Heartbeat mit seiner TTL
    #      die einzige Quelle -- unveraendertes Verhalten.
    # Host-Anteil dieses Servers -- die Relay-Sessions laufen im selben Prozess,
    # also ist das auch ihre Adresse. --base-url gewinnt (Betrieb hinter
    # Domain/Proxy/TLS), sonst der Host-Header der Anfrage.
    def _relay_host(self) -> str:
        base = self.server.base_url
        if base:
            return urlsplit(base).hostname or ""
        host = self.headers.get("Host") or self.server.server_name
        return host.rsplit(":", 1)[0] if ":" in host else host

    def _serve_presence(self, cell_id: str, path: Path) -> None:
        payload: dict[str, object] = {}
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}

        manager = self.server.relay_manager
        session = manager.session_for(cell_id)
        if session is not None:
            payload["cell_id"] = cell_id
            payload["hosted"] = True
            payload["host_port"] = session.port
            # Die Session laeuft auf DIESEM Server, also kann er die Adresse
            # selbst beantworten. Noetig, wenn der Initiator sich abgemeldet hat
            # (Presence geloescht) und ein Gast weiterspielt: ohne host_ip haelt
            # der Client die Zelle fuer frei, betritt sie als Host und rechnet
            # ihr Offline-Zeit an, obwohl sie gerade laeuft. Eine bereits
            # bekannte Adresse bleibt stehen -- hinter Proxy/Domain ist sie
            # genauer als der Host-Header.
            if not str(payload.get("host_ip") or "").strip():
                payload["host_ip"] = self._relay_host()
            payload["ttl_seconds"] = PRESENCE_TTL_SECONDS
            payload["source"] = "relay session"
            self._send_json(200, payload)
            return

        if manager.dedicated_ready():
            self._send_json(200, {
                "cell_id": cell_id,
                "hosted": False,
                "reason": "no relay session",
            })
            return

        updated_at = int(payload.get("updated_at") or 0)
        if not payload or int(time.time()) - updated_at > PRESENCE_TTL_SECONDS:
            self._send_json(200, {"cell_id": cell_id, "hosted": False})
            return
        payload["cell_id"] = cell_id
        payload["hosted"] = True
        payload["ttl_seconds"] = PRESENCE_TTL_SECONDS
        self._send_json(200, payload)

    def _put_presence(self, cell_id: str, path: Path) -> None:
        try:
            payload = json.loads(self._read_body(16 * 1024).decode("utf-8"))
        except json.JSONDecodeError as error:
            raise ValueError("invalid JSON") from error
        host_ip = str(payload.get("host_ip") or "").strip()
        host_port = int(payload.get("host_port") or 0)
        player_name = str(payload.get("player_name") or "").strip()[:64]
        if not host_ip:
            host_ip = self.client_address[0]
        if host_port <= 0 or host_port > 65535:
            self._send_json(400, {"error": "invalid host_port", "cell_id": cell_id})
            return
        stored = {
            "cell_id": cell_id,
            "hosted": True,
            "host_ip": host_ip,
            "host_port": host_port,
            "player_name": player_name,
            "updated_at": int(time.time()),
        }
        atomic_write(path, json.dumps(stored, sort_keys=True).encode("utf-8"))
        stored["ttl_seconds"] = PRESENCE_TTL_SECONDS
        self._send_json(200, stored)

    # Arrivals (Zellwechsel): eigener append-only Kanal, bewusst GETRENNT vom
    # Snapshot. Der Snapshot ist last-writer-wins -- laege ein Arrival darin,
    # wuerde die absendende Zelle mit ihrer veralteten Kopie den laufenden
    # Fortschritt der Nachbarzelle ueberschreiben. Zustellgarantie: ein Arrival
    # bleibt liegen, bis der Empfaenger es nach erfolgreichem Spawn per DELETE
    # quittiert.
    #
    # Datei-Layout: arrivals_dir/<cell>/<id>.txt, eine Datei pro Eintrag mit
    # der rohen Zeile. Eine Datei pro Eintrag statt einer gemeinsamen Datei,
    # weil dadurch zwei Zellen gleichzeitig in dieselbe Nachbarzelle schreiben
    # koennen, ohne sich zu ueberschreiben (kein read-modify-write), eine
    # Quittung genau einen Eintrag entfernt und alles einen Serverneustart
    # ueberlebt (kein Zustand im Prozess ausser der id-Vergabe).
    # Anders als bei Snapshots/Logs ist die Zelle hier ein VERZEICHNIS-Name,
    # deshalb safe_dir_name statt safe_cell_id (Punkt-Guard, siehe oben).
    def _arrivals_cell_dir(self, cell_id: str) -> Path | None:
        safe = safe_dir_name(cell_id)
        if safe is None:
            return None
        return self.server.arrivals_dir / safe

    def _list_arrival_ids(self, cell_dir: Path) -> list[str]:
        if not cell_dir.is_dir():
            return []
        # Lexikografisch sortiert = aeltestes zuerst (feste id-Breite).
        return sorted(entry.stem for entry in cell_dir.glob("*.txt") if entry.is_file())

    def _route_arrivals(self, rest: str) -> None:
        parts = rest.split("/")
        if len(parts) == 1:
            cell_dir = self._arrivals_cell_dir(parts[0])
            if cell_dir is None:
                self._send_json(400, {"error": "invalid cell id"})
                return
            if self.command == "GET":
                self._serve_arrivals(parts[0], cell_dir)
                return
            if self.command == "PUT":
                self._put_arrival(parts[0], cell_dir)
                return
            self._send_json(405, {"error": "method not allowed"})
            return
        if len(parts) == 2:
            cell_dir = self._arrivals_cell_dir(parts[0])
            arrival_id = safe_log_segment(parts[1])
            if cell_dir is None or arrival_id is None:
                self._send_json(400, {"error": "invalid cell id or arrival id"})
                return
            if self.command == "DELETE":
                self._delete_arrival(parts[0], cell_dir, arrival_id)
                return
            self._send_json(405, {"error": "method not allowed"})
            return
        self._send_json(400, {"error": "expected /arrivals/<cell> or /arrivals/<cell>/<id>"})

    def _serve_arrivals(self, cell_id: str, cell_dir: Path) -> None:
        arrivals: list[dict[str, object]] = []
        for arrival_id in self._list_arrival_ids(cell_dir):
            try:
                line = (cell_dir / f"{arrival_id}.txt").read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            arrivals.append({"id": arrival_id, "line": line.strip("\n")})
        self._send_json(200, {"cell_id": cell_id, "count": len(arrivals), "arrivals": arrivals})

    def _put_arrival(self, cell_id: str, cell_dir: Path) -> None:
        body = self._read_body(MAX_ARRIVAL_LINE_BYTES)
        try:
            line = body.decode("utf-8")
        except UnicodeDecodeError:
            self._send_json(400, {"error": "arrival must be UTF-8", "cell_id": cell_id})
            return
        line = line.strip("\r\n")
        if not line.strip():
            self._send_json(400, {"error": "empty arrival", "cell_id": cell_id})
            return
        if "\n" in line or "\r" in line:
            self._send_json(400, {"error": "arrival must be a single line", "cell_id": cell_id})
            return
        # Der Lock schuetzt nur id-Vergabe + Deckel-Pruefung; das Schreiben
        # selbst trifft eine eigene Datei und braucht keine Serialisierung.
        with self.server.arrivals_lock:
            existing = self._list_arrival_ids(cell_dir)
            if len(existing) >= MAX_ARRIVALS_PER_CELL:
                self._send_json(507, {
                    "error": "arrival queue full",
                    "cell_id": cell_id,
                    "count": len(existing),
                    "limit": MAX_ARRIVALS_PER_CELL,
                })
                return
            arrival_id, issued_ns = next_arrival_id(
                existing[-1] if existing else None,
                self.server.arrivals_last_ns,
            )
            self.server.arrivals_last_ns = issued_ns
            # mkdir passiert in atomic_write (parents/exist_ok): ein nach einem
            # Deploy fehlendes arrivals_dir darf den Server nicht lahmlegen.
            atomic_write(cell_dir / f"{arrival_id}.txt", (line + "\n").encode("utf-8"))
        self._send_json(200, {
            "ok": True,
            "cell_id": cell_id,
            "id": arrival_id,
            "size": len(body),
        })

    def _delete_arrival(self, cell_id: str, cell_dir: Path, arrival_id: str) -> None:
        # Idempotent: eine verlorene Quittung (Netzabbruch nach dem Spawn) darf
        # beim Wiederholen nicht als Fehler zurueckkommen.
        try:
            (cell_dir / f"{arrival_id}.txt").unlink()
        except FileNotFoundError:
            pass
        self._send_json(200, {"ok": True, "cell_id": cell_id, "id": arrival_id})

    # Relay-Sessions (Baustein internet-hosting): der Launcher des initiierenden
    # Spielers PUTtet das komplette Startscript; der Manager startet einen
    # spring-dedicated-Prozess und antwortet mit dem UDP-Port. Beide Spieler
    # verbinden sich dann als normale Clients (IsHost=0) zu <relay-host>:<port>.
    # safe_dir_name statt safe_cell_id: RelayManager.start legt sein
    # Session-Verzeichnis als run_dir/<cell_id> an (relay.py) und schreibt dort
    # script.txt/springsettings.cfg/dedicated.log -- ein "." oder ".." landete
    # damit ausserhalb des Relay-Verzeichnisses.
    def _route_relay_session(self, cell_id: str) -> None:
        if safe_dir_name(cell_id) is None:
            self._send_json(400, {"error": "invalid cell id"})
            return
        manager: RelayManager = self.server.relay_manager
        if self.command == "PUT":
            body = self._read_body(MAX_SCRIPT_BYTES)
            try:
                script_text = body.decode("utf-8")
            except UnicodeDecodeError:
                self._send_json(400, {"error": "script must be UTF-8", "cell_id": cell_id})
                return
            if not script_text.strip():
                self._send_json(400, {"error": "empty script", "cell_id": cell_id})
                return
            status, payload = manager.start(cell_id, script_text)
            self._send_json(status, payload)
            return
        if self.command == "DELETE":
            status, payload = manager.stop(cell_id)
            self._send_json(status, payload)
            return
        self._send_json(405, {"error": "method not allowed"})

    # Diagnose-Logs (Launcher + Engine infolog) pro Spieler, damit ein Agent oder
    # ein Mitspieler sie per GET nachvollziehen kann, ohne dass jemand sie manuell
    # von den Windows-PCs kopieren muss. Bewusst nur die letzten LOG_RUNS_PER_PLAYER
    # Durchlaeufe pro Spieler (last-writer-wins wie Snapshots), keine Historie.
    def _log_path(self, player: str, run_id: str, kind: str) -> Path | None:
        safe_player = safe_log_segment(player)
        safe_run = safe_log_segment(run_id)
        if safe_player is None or safe_run is None or kind not in LOG_KINDS:
            return None
        return self.server.logs_dir / safe_player / safe_run / f"{kind}.log"

    def _route_log_entry(self, rest: str) -> None:
        parts = rest.split("/")
        if len(parts) != 3:
            self._send_json(400, {"error": "expected /logs/<player>/<run_id>/<kind>"})
            return
        player, run_id, kind = parts
        path = self._log_path(player, run_id, kind)
        if path is None:
            self._send_json(400, {"error": "invalid player, run_id or kind"})
            return
        if self.command == "GET":
            self._serve_log(path)
            return
        if self.command == "PUT":
            self._put_log(player, run_id, path)
            return
        self._send_json(405, {"error": "method not allowed"})

    def _serve_log(self, path: Path) -> None:
        if not path.is_file():
            self._send_json(404, {"error": "log not found"})
            return
        stat = path.stat()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(stat.st_size))
        self.send_header("X-Conatus-Updated-At", str(int(stat.st_mtime)))
        self.end_headers()
        with path.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                self.wfile.write(chunk)

    def _put_log(self, player: str, run_id: str, path: Path) -> None:
        body = self._read_body(MAX_LOG_BYTES)
        atomic_write(path, body)
        # Aufgeraeumt wird im Verzeichnis der eben geschriebenen Datei, NICHT
        # ueber den Rohwert aus der URL: path stammt aus dem geprueften
        # _log_path, path.parent.parent kann deshalb nicht aus logs_dir
        # herauszeigen (Vorfall 2026-08-01, siehe safe_log_segment).
        self._prune_old_runs(path.parent.parent)
        self._send_json(200, {"ok": True, "player": player, "run_id": run_id, "size": len(body)})

    def _prune_old_runs(self, player_dir: Path) -> None:
        # Zweite Reissleine hinter der Segment-Pruefung: hier wird geloescht,
        # also muss das Ziel nachweislich ein direktes Kind von logs_dir sein.
        # resolve() rechnet ".." und Symlinks vorher weg.
        try:
            resolved = player_dir.resolve()
            if not resolved.is_dir() or resolved.parent != self.server.logs_dir.resolve():
                return
        except OSError:
            return
        player_dir = resolved
        runs = sorted(
            (entry for entry in player_dir.iterdir() if entry.is_dir()),
            key=lambda entry: entry.name,
            reverse=True,
        )
        for stale in runs[LOG_RUNS_PER_PLAYER:]:
            shutil.rmtree(stale, ignore_errors=True)

    def _serve_log_index(self) -> None:
        logs_dir: Path = self.server.logs_dir
        index: dict[str, list[dict[str, object]]] = {}
        if logs_dir.is_dir():
            for player_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
                runs = []
                for run_dir in sorted(
                    (entry for entry in player_dir.iterdir() if entry.is_dir()),
                    key=lambda entry: entry.name,
                    reverse=True,
                ):
                    kinds = sorted(p.stem for p in run_dir.glob("*.log"))
                    if kinds:
                        runs.append({"run_id": run_dir.name, "kinds": kinds})
                if runs:
                    index[player_dir.name] = runs
        self._send_json(200, index)

    def _send_json(self, status: int, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # Die Request-Line enthaelt bei gesetztem Secret den Pfad-Prefix -- und
    # journald ist nicht der Ort dafuer (jeder mit Journal-Lesezugriff haette
    # sonst den Zugang). Das Bearer-Token steht im Header, nie in der
    # Request-Line, und kann hier deshalb gar nicht auftauchen.
    # Zeitstempel und Identitaet ergaenzt Baustein diagnostics-log-platform.
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        text = format % args
        secret = getattr(self.server, "secret", None)
        if secret:
            text = text.replace(secret, "<secret>")
        print(f"[map-server] {self.client_address[0]} {text}", flush=True)


def create_server(
    maps_dir: Path,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    base_url: str | None = None,
    secret: str | None = None,
    snapshots_dir: Path | None = None,
    presence_dir: Path | None = None,
    logs_dir: Path | None = None,
    arrivals_dir: Path | None = None,
    relay_engine_dir: Path | None = None,
    relay_run_dir: Path | None = None,
    relay_ports: str = DEFAULT_PORT_RANGE,
    tokens_file: Path | None = None,
    legacy_secret_scope: str = LEGACY_SCOPE_ALL,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MapServerHandler)
    server.map_index = MapIndex(maps_dir)
    server.base_url = base_url.rstrip("/") if base_url else None
    server.secret = secret.strip("/") if secret else None
    server.token_store = TokenStore(tokens_file)
    if legacy_secret_scope not in LEGACY_SCOPES:
        raise ValueError(f"unknown legacy secret scope: {legacy_secret_scope}")
    server.legacy_secret_scope = legacy_secret_scope
    server.snapshots_dir = snapshots_dir or (maps_dir.parent / "snapshots")
    server.presence_dir = presence_dir or (maps_dir.parent / "presence")
    server.logs_dir = logs_dir or (maps_dir.parent / "logs")
    server.arrivals_dir = arrivals_dir or (maps_dir.parent / "arrivals")
    # ThreadingHTTPServer bearbeitet jede Anfrage in einem eigenen Thread
    # (siehe RelayManager.lock als Vorbild): der Lock serialisiert nur die
    # id-Vergabe der Arrivals, nicht das Schreiben.
    server.arrivals_lock = threading.Lock()
    server.arrivals_last_ns = 0
    server.relay_manager = RelayManager(
        engine_dir=relay_engine_dir,
        run_dir=relay_run_dir or (maps_dir.parent / "relay"),
        port_range=relay_ports)
    return server


def serve_maps(
    maps_dir: Path,
    snapshots_dir: Path | None = None,
    presence_dir: Path | None = None,
    logs_dir: Path | None = None,
    arrivals_dir: Path | None = None,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    base_url: str | None = None,
    secret: str | None = None,
    relay_engine_dir: Path | None = None,
    relay_run_dir: Path | None = None,
    relay_ports: str = DEFAULT_PORT_RANGE,
    tokens_file: Path | None = None,
    legacy_secret_scope: str = LEGACY_SCOPE_ALL,
) -> None:
    maps_dir.mkdir(parents=True, exist_ok=True)
    resolved_snapshots = snapshots_dir or (maps_dir.parent / "snapshots")
    resolved_presence = presence_dir or (maps_dir.parent / "presence")
    resolved_logs = logs_dir or (maps_dir.parent / "logs")
    resolved_arrivals = arrivals_dir or (maps_dir.parent / "arrivals")
    resolved_relay_run = relay_run_dir or (maps_dir.parent / "relay")
    resolved_snapshots.mkdir(parents=True, exist_ok=True)
    resolved_presence.mkdir(parents=True, exist_ok=True)
    resolved_logs.mkdir(parents=True, exist_ok=True)
    resolved_arrivals.mkdir(parents=True, exist_ok=True)
    resolved_relay_run.mkdir(parents=True, exist_ok=True)
    server = create_server(
        maps_dir,
        host,
        port,
        base_url=base_url,
        secret=secret,
        snapshots_dir=resolved_snapshots,
        presence_dir=resolved_presence,
        logs_dir=resolved_logs,
        arrivals_dir=resolved_arrivals,
        relay_engine_dir=relay_engine_dir,
        relay_run_dir=resolved_relay_run,
        relay_ports=relay_ports,
        tokens_file=tokens_file,
        legacy_secret_scope=legacy_secret_scope,
    )
    entries = server.map_index.refresh()
    if not (maps_dir / INDEX_FILENAME).is_file():
        print(f"[map-server] HINWEIS: {maps_dir}/{INDEX_FILENAME} fehlt noch "
              f"-- erst sync-map-server vom Laptop ausfuehren", flush=True)
    print(f"[map-server] {len(entries)} Karten in {maps_dir}", flush=True)
    for entry in entries:
        print(f"[map-server]   {entry.springname} ({entry.filename}, {entry.size} bytes)", flush=True)
    # Prefix NICHT ausdrucken -- diese Zeile landet in journald.
    prefix = "/<secret>" if server.secret else ""
    print(f"[map-server] lauscht auf http://{host}:{port}{prefix} "
          f"(maps.json | json.php?category=map&springname=... | maps/<datei> | "
          f"snapshots/<cell> | presence/<cell> | arrivals/<cell>[/<id>] | "
          f"logs | logs/<player>/<run>/<kind> | "
          f"relay/health | relay/sessions[/<cell>])", flush=True)
    print(f"[map-server] token-auth: {server.token_store.describe()}", flush=True)
    for token, name in sorted(server.token_store.refresh().items(), key=lambda item: item[1]):
        if len(token) < MIN_TOKEN_LENGTH:
            print(f"[map-server]   WARNUNG: Token fuer '{name}' ist kuerzer als "
                  f"{MIN_TOKEN_LENGTH} Zeichen", flush=True)
    if tokens_file is not None:
        try:
            mode = tokens_file.stat().st_mode & 0o077
        except OSError:
            mode = 0
        if mode:
            print(f"[map-server]   WARNUNG: {tokens_file} ist fuer Gruppe/andere "
                  f"lesbar -- chmod 640 und chown root:conatus", flush=True)
    scope_note = ("Pfad-Prefix oeffnet noch ALLES (Uebergangsfenster) -- nach dem "
                  "Rollout auf --legacy-secret-scope maps umstellen"
                  if server.legacy_secret_scope == LEGACY_SCOPE_ALL
                  else "Pfad-Prefix oeffnet nur noch die Karten-Endpunkte")
    print(f"[map-server] legacy-secret-scope={server.legacy_secret_scope}: {scope_note}",
          flush=True)
    relay_state = ("bereit, engine=" + str(relay_engine_dir)
                   if server.relay_manager.dedicated_ready()
                   else "OHNE dedicated-Binary (health meldet dedicated_ready=false)")
    print(f"[map-server] relay: {relay_state}, ports={relay_ports}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="conatus-map-server")
    parser.add_argument("--maps-dir", type=Path, required=True,
                        help="Verzeichnis mit .sd7 + maps.json (rsync-Ziel von sync-map-server)")
    parser.add_argument("--snapshots-dir", type=Path, default=None,
                        help="Verzeichnis fuer zentrale Zell-Snapshots (Default: ../snapshots)")
    parser.add_argument("--presence-dir", type=Path, default=None,
                        help="Verzeichnis fuer kurzlebige Host-Presence (Default: ../presence)")
    parser.add_argument("--logs-dir", type=Path, default=None,
                        help="Verzeichnis fuer Diagnose-Logs pro Spieler (Default: ../logs)")
    parser.add_argument("--arrivals-dir", type=Path, default=None,
                        help="Verzeichnis fuer offene Zell-Arrivals (Default: ../arrivals)")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--base-url", default=None,
                        help="Oeffentliche Basis-URL ohne Secret (z.B. http://maps.example.org:8605)")
    # Bewusst KEIN --secret in der systemd-ExecStart-Zeile: Argumente stehen in
    # /proc/<pid>/cmdline und sind fuer jeden lokalen Nutzer lesbar. Die
    # Umgebung (/proc/<pid>/environ) gehoert dagegen nur dem Prozessbesitzer,
    # deshalb ist CONATUS_MAP_SECRET der Normalweg und --secret nur noch fuer
    # Dev/Tests da.
    parser.add_argument("--secret", default=os.environ.get("CONATUS_MAP_SECRET") or None,
                        help="Pfad-Prefix als Karten-Download-Zugang (Default: $CONATUS_MAP_SECRET; "
                             "nicht auf der Kommandozeile uebergeben, das landet in /proc/<pid>/cmdline)")
    parser.add_argument("--tokens-file", type=Path,
                        default=(Path(os.environ["CONATUS_MAP_TOKENS_FILE"])
                                 if os.environ.get("CONATUS_MAP_TOKENS_FILE") else None),
                        help="Datei mit '<token> <name>' je Zeile (Default: $CONATUS_MAP_TOKENS_FILE). "
                             "Ohne sie bleibt der Pfad-Prefix der einzige Zugang.")
    parser.add_argument("--legacy-secret-scope", choices=LEGACY_SCOPES,
                        default=os.environ.get("CONATUS_LEGACY_SECRET_SCOPE") or LEGACY_SCOPE_ALL,
                        help="Reichweite des Pfad-Prefix, sobald Token konfiguriert sind: "
                             "'all' = Uebergangsfenster (oeffnet alles), "
                             "'maps' = Zielzustand (nur Karten-Downloads, alles andere braucht Token)")
    parser.add_argument("--relay-engine-dir", type=Path, default=None,
                        help="Verzeichnis mit spring-dedicated + base/ (fehlt es, meldet "
                             "/relay/health dedicated_ready=false)")
    parser.add_argument("--relay-run-dir", type=Path, default=None,
                        help="Arbeitsverzeichnis der Relay-Sessions (Default: ../relay)")
    parser.add_argument("--relay-ports", default=DEFAULT_PORT_RANGE,
                        help=f"UDP-Portbereich fuer Relay-Sessions (Default: {DEFAULT_PORT_RANGE})")
    args = parser.parse_args(argv)
    serve_maps(args.maps_dir, snapshots_dir=args.snapshots_dir, presence_dir=args.presence_dir,
               logs_dir=args.logs_dir, arrivals_dir=args.arrivals_dir,
               host=args.host, port=args.port,
               base_url=args.base_url, secret=args.secret,
               relay_engine_dir=args.relay_engine_dir, relay_run_dir=args.relay_run_dir,
               relay_ports=args.relay_ports,
               tokens_file=args.tokens_file,
               legacy_secret_scope=args.legacy_secret_scope)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
