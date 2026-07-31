from __future__ import annotations

import argparse
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


def safe_log_segment(value: str) -> str | None:
    if _SAFE_LOG_SEGMENT.fullmatch(value):
        return value
    return None


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

    protocol_version = "HTTP/1.1"

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

    def _route(self) -> None:
        url = urlsplit(self.path)
        path = url.path
        secret = self.server.secret
        if secret:
            prefix = "/" + secret
            if path != prefix and not path.startswith(prefix + "/"):
                self._send_json(404, {"error": "not found"})
                return
            path = path[len(prefix):] or "/"
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
    # Anders als bei Snapshots/Logs ist die Zelle hier ein VERZEICHNIS-Name.
    # Die Whitelist erlaubt Punkte (Karten-Namen wie "nature.0.2"), deshalb
    # muessen "." und ".." zusaetzlich raus -- sonst zeigte der Pfad aus
    # arrivals_dir heraus (bei Snapshots faengt das angehaengte ".txt" das ab).
    def _arrivals_cell_dir(self, cell_id: str) -> Path | None:
        safe = safe_cell_id(cell_id)
        if safe is None or safe.startswith("."):
            return None
        return self.server.arrivals_dir / safe

    def _arrival_id_segment(self, arrival_id: str) -> str | None:
        safe = safe_log_segment(arrival_id)
        if safe is None or safe.startswith("."):
            return None
        return safe

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
            arrival_id = self._arrival_id_segment(parts[1])
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
    def _route_relay_session(self, cell_id: str) -> None:
        if safe_cell_id(cell_id) is None:
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
        self._prune_old_runs(player)
        self._send_json(200, {"ok": True, "player": player, "run_id": run_id, "size": len(body)})

    def _prune_old_runs(self, player: str) -> None:
        player_dir = self.server.logs_dir / player
        if not player_dir.is_dir():
            return
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

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        print(f"[map-server] {self.client_address[0]} {format % args}", flush=True)


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
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MapServerHandler)
    server.map_index = MapIndex(maps_dir)
    server.base_url = base_url.rstrip("/") if base_url else None
    server.secret = secret.strip("/") if secret else None
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
    )
    entries = server.map_index.refresh()
    if not (maps_dir / INDEX_FILENAME).is_file():
        print(f"[map-server] HINWEIS: {maps_dir}/{INDEX_FILENAME} fehlt noch "
              f"-- erst sync-map-server vom Laptop ausfuehren", flush=True)
    print(f"[map-server] {len(entries)} Karten in {maps_dir}", flush=True)
    for entry in entries:
        print(f"[map-server]   {entry.springname} ({entry.filename}, {entry.size} bytes)", flush=True)
    prefix = ("/" + server.secret) if server.secret else ""
    print(f"[map-server] lauscht auf http://{host}:{port}{prefix} "
          f"(maps.json | json.php?category=map&springname=... | maps/<datei> | "
          f"snapshots/<cell> | presence/<cell> | arrivals/<cell>[/<id>] | "
          f"logs | logs/<player>/<run>/<kind> | "
          f"relay/health | relay/sessions[/<cell>])", flush=True)
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
    parser.add_argument("--secret", default=None,
                        help="Pfad-Prefix als Minimal-Zugriffsschutz (Clients haengen ihn an die URL)")
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
               relay_ports=args.relay_ports)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
