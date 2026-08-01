from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

from map_server.index import MapIndex
from map_server.server import (
    ARRIVAL_ID_DIGITS,
    MAX_ARRIVAL_LINE_BYTES,
    MAX_ARRIVALS_PER_CELL,
    MapServerHandler,
    create_server,
    next_arrival_id,
)


def _write_fixture(maps_dir: Path) -> None:
    (maps_dir / "cell-a1.sd7").write_bytes(b"payload")
    (maps_dir / "maps.json").write_text(json.dumps([
        {"springname": "Cell A1 0.1", "filename": "cell-a1.sd7",
         "size": 7, "md5": hashlib.md5(b"payload").hexdigest()},
    ]), encoding="utf-8")


class MapIndexTest(unittest.TestCase):
    def test_reads_index_file_and_picks_up_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maps_dir = Path(tmp)
            _write_fixture(maps_dir)
            index = MapIndex(maps_dir)
            entries = index.refresh()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0].springname, "Cell A1 0.1")
            self.assertIsNotNone(index.find("Cell A1 0.1"))
            self.assertIsNone(index.find("Unbekannt"))

            (maps_dir / "maps.json").write_text("[]", encoding="utf-8")
            self.assertEqual(index.refresh(), [])

    def test_broken_index_keeps_last_good_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            maps_dir = Path(tmp)
            _write_fixture(maps_dir)
            index = MapIndex(maps_dir)
            self.assertEqual(len(index.refresh()), 1)
            (maps_dir / "maps.json").write_text("{kaputt", encoding="utf-8")
            self.assertEqual(len(index.refresh()), 1)

    def test_missing_index_means_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            index = MapIndex(Path(tmp))
            self.assertEqual(index.refresh(), [])


class ServerTestBase(unittest.TestCase):
    base_url: str | None = None
    secret: str | None = None

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        maps_dir = Path(self._tmp.name)
        _write_fixture(maps_dir)
        self.server = create_server(
            maps_dir, host="127.0.0.1", port=0,
            base_url=self.base_url, secret=self.secret,
            # Explizit isoliert im Test-Tempdir: der Default (maps_dir.parent / "logs")
            # zielt in der Produktion auf ein Geschwisterverzeichnis von "maps/", trifft
            # hier aber das geteilte /tmp, weil maps_dir selbst schon das Tempdir ist.
            logs_dir=maps_dir / "logs",
            arrivals_dir=maps_dir / "arrivals",
            relay_run_dir=maps_dir / "relay",
        )
        self.maps_dir = maps_dir
        self.snapshots_dir = self.server.snapshots_dir
        self.presence_dir = self.server.presence_dir
        self.logs_dir = self.server.logs_dir
        self.arrivals_dir = self.server.arrivals_dir
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def _get(self, path: str) -> tuple[int, bytes]:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}") as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            error.close()
            return error.code, body

    def _put(self, path: str, data: bytes, content_type: str = "text/plain") -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=data,
            method="PUT",
            headers={"Content-Type": content_type},
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            error.close()
            return error.code, body

    def _delete(self, path: str) -> tuple[int, bytes]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            method="DELETE",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            body = error.read()
            error.close()
            return error.code, body


class PlainServerTest(ServerTestBase):
    def test_maps_json(self) -> None:
        status, body = self._get("/maps.json")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(len(payload), 1)
        self.assertEqual(set(payload[0]), {"springname", "filename", "size", "md5"})

    def test_json_php_springfiles_contract(self) -> None:
        # Vertrag aus pr-downloader HttpDownloader.cpp ParseResult.
        status, body = self._get("/json.php?category=map&springname=Cell%20A1%200.1")
        self.assertEqual(status, 200)
        result = json.loads(body)[0]
        self.assertEqual(result["category"], "map")
        self.assertEqual(result["springname"], "Cell A1 0.1")
        self.assertEqual(result["filename"], "cell-a1.sd7")
        self.assertTrue(result["mirrors"][0].startswith("http://"))
        self.assertIn("/maps/cell-a1.sd7", result["mirrors"][0])
        self.assertIsInstance(result["size"], int)
        self.assertRegex(result["md5"], r"^[0-9a-f]{32}$")

    def test_unknown_map_returns_empty_array(self) -> None:
        status, body = self._get("/json.php?category=map&springname=GibtEsNicht")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), [])

    def test_download_matches_md5(self) -> None:
        _, body = self._get("/json.php?category=map&springname=Cell%20A1%200.1")
        result = json.loads(body)[0]
        status, data = self._get(f"/maps/{result['filename']}")
        self.assertEqual(status, 200)
        self.assertEqual(hashlib.md5(data).hexdigest(), result["md5"])
        self.assertEqual(len(data), result["size"])

    def test_traversal_is_blocked(self) -> None:
        status, _ = self._get("/maps/..%2F..%2Fetc%2Fpasswd")
        self.assertEqual(status, 404)

    def test_snapshot_put_get_roundtrip(self) -> None:
        payload = b"schema=2\n[meta]\nmap_cell_id=map_0_0\n"
        status, body = self._put("/snapshots/map_0_0", payload)
        self.assertEqual(status, 200, body)
        self.assertTrue((self.snapshots_dir / "map_0_0.txt").is_file())

        status, body = self._get("/snapshots/map_0_0")
        self.assertEqual(status, 200)
        self.assertEqual(body, payload)

    def test_snapshot_rejects_bad_cell_id_and_empty_body(self) -> None:
        self.assertEqual(self._put("/snapshots/..%2Fbad", b"schema=2\n")[0], 400)
        self.assertEqual(self._put("/snapshots/map_0_0", b"")[0], 400)

    def test_presence_put_get_and_expiry(self) -> None:
        status, body = self._put(
            "/presence/map_0_0",
            json.dumps({"host_ip": "10.0.0.2", "host_port": 8452, "player_name": "PC1"}).encode(),
            "application/json",
        )
        self.assertEqual(status, 200, body)
        status, body = self._get("/presence/map_0_0")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertTrue(payload["hosted"])
        self.assertEqual(payload["host_ip"], "10.0.0.2")
        self.assertEqual(payload["host_port"], 8452)
        self.assertEqual(payload["player_name"], "PC1")

        saved = self.presence_dir / "map_0_0.json"
        stale = json.loads(saved.read_text(encoding="utf-8"))
        stale["updated_at"] = 1
        saved.write_text(json.dumps(stale), encoding="utf-8")
        self.assertFalse(json.loads(self._get("/presence/map_0_0")[1])["hosted"])

    def test_log_put_get_roundtrip(self) -> None:
        status, body = self._put("/logs/PC1/20260725-100000/launcher", b"hello launcher log\n")
        self.assertEqual(status, 200, body)
        status, body = self._get("/logs/PC1/20260725-100000/launcher")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"hello launcher log\n")

    def test_log_rejects_bad_segments_and_unknown_kind(self) -> None:
        self.assertEqual(self._put("/logs/..%2Fbad/run/launcher", b"x")[0], 400)
        self.assertEqual(self._put("/logs/PC1/..%2Fbad/launcher", b"x")[0], 400)
        self.assertEqual(self._put("/logs/PC1/run/enginex", b"x")[0], 400)
        self.assertEqual(self._get("/logs/PC1/does-not-exist/engine")[0], 404)

    def test_log_dot_segments_are_rejected_and_delete_nothing(self) -> None:
        """Regression 2026-08-01 (map-server-log-path-traversal).

        ".." passte auf die Segment-Whitelist (sie erlaubt Punkte fuer
        Datei-/Kartennamen). Damit schrieb _log_path nach logs_dir/../<run>/,
        und _prune_old_runs bekam denselben Rohwert -- es raeumte also das
        ELTERNverzeichnis von logs/ auf und loeschte dort per rmtree alles
        ausser den drei lexikografisch obersten Verzeichnissen. In der
        Produktion sind das genau snapshots/, arrivals/ und maps/.
        """
        # Sortiert bewusst nach ganz unten: waere das Pruning noch offen, traefe
        # es als erstes dieses Verzeichnis.
        victim = self.logs_dir.parent / "aaa-must-survive"
        victim.mkdir(parents=True, exist_ok=True)
        (victim / "keep.txt").write_text("bleibt", encoding="utf-8")
        # Ein echter Log zuerst, damit logs/ existiert wie im Betrieb.
        self.assertEqual(self._put("/logs/PC1/20260801-100000/launcher", b"ok\n")[0], 200)

        for bad in ("..", ".", "%2E%2E", "%2e"):
            status, body = self._put(f"/logs/{bad}/20260801-100001/launcher", b"boom\n")
            self.assertEqual(status, 400, f"player={bad}: {body!r}")
            status, body = self._put(f"/logs/PC1/{bad}/launcher", b"boom\n")
            self.assertEqual(status, 400, f"run_id={bad}: {body!r}")
            self.assertEqual(self._get(f"/logs/{bad}/20260801-100000/launcher")[0], 400)
        # Leeres Segment ebenfalls, sonst zeigte der Pfad auf logs_dir selbst.
        self.assertEqual(self._put("/logs//20260801-100001/launcher", b"boom\n")[0], 400)

        # Nichts geloescht ...
        self.assertTrue((victim / "keep.txt").is_file())
        self.assertTrue((self.logs_dir / "PC1" / "20260801-100000" / "launcher.log").is_file())
        # ... und nichts ausserhalb von logs/ geschrieben.
        self.assertFalse((self.logs_dir.parent / "20260801-100001").exists())

    def test_relay_session_rejects_dot_cell_ids(self) -> None:
        # Gleiche Fehlerklasse: RelayManager.start legt run_dir/<cell_id> an und
        # schreibt dort script.txt/springsettings.cfg/dedicated.log.
        for bad in ("..", ".", "%2E%2E"):
            status, body = self._put(f"/relay/sessions/{bad}", b"HostPort=0;\n")
            self.assertEqual(status, 400, f"cell={bad}: {body!r}")
        self.assertFalse((self.maps_dir / "script.txt").exists())

    def test_log_index_lists_newest_run_first(self) -> None:
        self._put("/logs/PC1/20260725-100000/launcher", b"first\n")
        self._put("/logs/PC1/20260725-100100/launcher", b"second\n")
        self._put("/logs/PC1/20260725-100100/engine", b"infolog\n")
        status, body = self._get("/logs")
        self.assertEqual(status, 200)
        index = json.loads(body)
        self.assertEqual(index["PC1"][0]["run_id"], "20260725-100100")
        self.assertEqual(sorted(index["PC1"][0]["kinds"]), ["engine", "launcher"])
        self.assertEqual(index["PC1"][1]["run_id"], "20260725-100000")

    def test_log_keeps_only_last_three_runs_per_player(self) -> None:
        for run_id in ("20260725-100000", "20260725-100100", "20260725-100200", "20260725-100300"):
            self._put(f"/logs/PC1/{run_id}/launcher", run_id.encode())
        remaining = sorted(p.name for p in (self.logs_dir / "PC1").iterdir())
        self.assertEqual(
            remaining,
            ["20260725-100100", "20260725-100200", "20260725-100300"],
        )

    def test_arrival_put_get_roundtrip(self) -> None:
        line = b"unit=conatus_worker;health=100;from=map_0_0"
        status, body = self._put("/arrivals/map_0_1", line)
        self.assertEqual(status, 200, body)
        payload = json.loads(body)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["cell_id"], "map_0_1")
        self.assertEqual(len(payload["id"]), ARRIVAL_ID_DIGITS)
        self.assertTrue((self.arrivals_dir / "map_0_1" / f"{payload['id']}.txt").is_file())

        status, body = self._get("/arrivals/map_0_1")
        self.assertEqual(status, 200)
        listing = json.loads(body)
        self.assertEqual(listing["cell_id"], "map_0_1")
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["arrivals"], [{"id": payload["id"], "line": line.decode()}])
        # Unbekannte Zelle ist leer, kein 404: der Empfaenger fragt bei jedem Start.
        self.assertEqual(json.loads(self._get("/arrivals/map_9_9")[1])["arrivals"], [])

    def test_arrival_get_returns_oldest_first(self) -> None:
        ids = []
        for index in range(4):
            status, body = self._put("/arrivals/map_0_1", f"unit=nr{index}".encode())
            self.assertEqual(status, 200, body)
            ids.append(json.loads(body)["id"])
        listing = json.loads(self._get("/arrivals/map_0_1")[1])
        self.assertEqual([entry["id"] for entry in listing["arrivals"]], ids)
        self.assertEqual(
            [entry["line"] for entry in listing["arrivals"]],
            ["unit=nr0", "unit=nr1", "unit=nr2", "unit=nr3"],
        )
        self.assertEqual(ids, sorted(ids))

    def test_arrival_delete_acks_only_that_entry(self) -> None:
        first = json.loads(self._put("/arrivals/map_0_1", b"unit=a")[1])["id"]
        second = json.loads(self._put("/arrivals/map_0_1", b"unit=b")[1])["id"]
        status, body = self._delete(f"/arrivals/map_0_1/{first}")
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        listing = json.loads(self._get("/arrivals/map_0_1")[1])
        self.assertEqual([entry["id"] for entry in listing["arrivals"]], [second])

    def test_arrival_delete_is_idempotent(self) -> None:
        arrival_id = json.loads(self._put("/arrivals/map_0_1", b"unit=a")[1])["id"]
        self.assertEqual(self._delete(f"/arrivals/map_0_1/{arrival_id}")[0], 200)
        # Verlorene Quittung: das Wiederholen darf kein Fehler sein.
        self.assertEqual(self._delete(f"/arrivals/map_0_1/{arrival_id}")[0], 200)
        # Nie existierende id genauso.
        self.assertEqual(self._delete("/arrivals/map_0_1/00000000000000000001")[0], 200)
        self.assertEqual(json.loads(self._get("/arrivals/map_0_1")[1])["count"], 0)

    def test_arrival_rejects_bad_segments_and_oversized_body(self) -> None:
        self.assertEqual(self._put("/arrivals/bad%20cell", b"unit=a")[0], 400)
        self.assertEqual(self._put("/arrivals/%2E%2E", b"unit=a")[0], 400)
        # Zelle ist ein Verzeichnisname -> Traversal muss abgewiesen werden.
        self.assertEqual(self._delete("/arrivals/..%2Fbad")[0], 400)
        self.assertEqual(self._delete("/arrivals/map_0_1/%2E%2E")[0], 400)
        self.assertEqual(self._delete("/arrivals/map_0_1/bad%20id")[0], 400)
        self.assertEqual(self._delete("/arrivals/map_0_1/id/zuviel")[0], 400)
        self.assertEqual(self._put("/arrivals/map_0_1", b"")[0], 400)
        self.assertEqual(self._put("/arrivals/map_0_1", b"unit=a\nunit=b")[0], 400)
        self.assertEqual(
            self._put("/arrivals/map_0_1", b"x" * (MAX_ARRIVAL_LINE_BYTES + 1))[0],
            400,
        )
        self.assertEqual(json.loads(self._get("/arrivals/map_0_1")[1])["count"], 0)

    def test_arrival_ids_differ_within_same_second(self) -> None:
        ids = [json.loads(self._put("/arrivals/map_0_1", b"unit=a")[1])["id"] for _ in range(8)]
        self.assertEqual(len(set(ids)), len(ids))
        self.assertEqual(json.loads(self._get("/arrivals/map_0_1")[1])["count"], len(ids))

    def test_arrival_cap_answers_507_and_keeps_queue(self) -> None:
        # Deckel direkt auf der Platte herstellen (MAX_ARRIVALS_PER_CELL echte
        # PUTs waeren nur langsam, nicht aussagekraeftiger).
        cell_dir = self.arrivals_dir / "map_0_1"
        cell_dir.mkdir(parents=True, exist_ok=True)
        for index in range(MAX_ARRIVALS_PER_CELL):
            (cell_dir / f"{index:020d}.txt").write_text(f"unit=nr{index}\n", encoding="utf-8")
        status, body = self._put("/arrivals/map_0_1", b"unit=zuviel")
        self.assertEqual(status, 507, body)
        payload = json.loads(body)
        self.assertEqual(payload["limit"], MAX_ARRIVALS_PER_CELL)
        # Nichts verworfen: der Absender behaelt seine Einheit und darf spaeter erneut.
        self.assertEqual(len(list(cell_dir.glob("*.txt"))), MAX_ARRIVALS_PER_CELL)
        self.assertEqual(
            json.loads(self._get("/arrivals/map_0_1")[1])["count"],
            MAX_ARRIVALS_PER_CELL,
        )

    def test_arrivals_survive_a_new_server_instance(self) -> None:
        arrival_id = json.loads(self._put("/arrivals/map_0_1", b"unit=bleibt")[1])["id"]
        # Neustart-Simulation: zweite Instanz auf demselben arrivals_dir.
        other = create_server(
            self.maps_dir, host="127.0.0.1", port=0,
            logs_dir=self.maps_dir / "logs",
            arrivals_dir=self.arrivals_dir,
            relay_run_dir=self.maps_dir / "relay",
        )
        thread = threading.Thread(target=other.serve_forever, daemon=True)
        thread.start()
        try:
            port = other.server_address[1]
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/arrivals/map_0_1") as response:
                listing = json.loads(response.read())
        finally:
            other.shutdown()
            other.server_close()
            thread.join(timeout=5)
        self.assertEqual([entry["id"] for entry in listing["arrivals"]], [arrival_id])
        self.assertEqual(listing["arrivals"][0]["line"], "unit=bleibt")


class PruneOldRunsGuardTest(unittest.TestCase):
    """_prune_old_runs loescht per rmtree -- es muss auch dann im logs-Verzeichnis
    bleiben, wenn die Segment-Pruefung davor einmal versagt (zweite Reissleine).
    """

    def _handler(self, logs_dir: Path) -> MapServerHandler:
        # Ohne Socket: _prune_old_runs braucht nur self.server.logs_dir.
        handler = MapServerHandler.__new__(MapServerHandler)
        handler.server = SimpleNamespace(logs_dir=logs_dir)
        return handler

    def test_refuses_everything_outside_logs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            logs_dir = root / "logs"
            (logs_dir / "PC1").mkdir(parents=True)
            for name in ("arrivals", "maps", "snapshots", "presence", "relay"):
                (root / name).mkdir()
            handler = self._handler(logs_dir)
            handler._prune_old_runs(logs_dir / "..")   # der alte Angriffspfad
            handler._prune_old_runs(root)
            handler._prune_old_runs(root / "maps")     # Geschwister von logs/
            self.assertEqual(
                sorted(p.name for p in root.iterdir()),
                ["arrivals", "logs", "maps", "presence", "relay", "snapshots"],
            )

    def test_still_prunes_inside_logs_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp) / "logs"
            player_dir = logs_dir / "PC1"
            for run in ("r1", "r2", "r3", "r4"):
                (player_dir / run).mkdir(parents=True)
            self._handler(logs_dir)._prune_old_runs(player_dir)
            self.assertEqual(sorted(p.name for p in player_dir.iterdir()), ["r2", "r3", "r4"])


class ArrivalIdTest(unittest.TestCase):
    def test_same_nanosecond_cannot_collide(self) -> None:
        first, first_ns = next_arrival_id(None, 0)
        # last_issued_ns == die Nanosekunde, in der auch der zweite PUT liegt.
        second, second_ns = next_arrival_id(None, first_ns)
        self.assertNotEqual(first, second)
        self.assertGreater(second_ns, first_ns)
        # Fest breit -> lexikografische Sortierung == zeitliche Reihenfolge.
        self.assertEqual(len(first), len(second))
        self.assertLess(first, second)

    def test_bumps_past_newest_id_on_disk(self) -> None:
        # Zurueckgestellte Uhr / Neustart: die id muss trotzdem steigen.
        newest = f"{time.time_ns() + 10 ** 12:0{ARRIVAL_ID_DIGITS}d}"
        later, _ = next_arrival_id(newest, 0)
        self.assertGreater(later, newest)
        self.assertEqual(len(later), ARRIVAL_ID_DIGITS)


class SecretAndBaseUrlTest(ServerTestBase):
    base_url = "http://maps.example.org:8605"
    secret = "geheim123"

    def test_routes_require_secret_prefix(self) -> None:
        self.assertEqual(self._get("/maps.json")[0], 404)
        self.assertEqual(self._get("/maps/cell-a1.sd7")[0], 404)
        self.assertEqual(self._get("/geheim123/maps.json")[0], 200)
        self.assertEqual(self._get("/geheim123/maps/cell-a1.sd7")[0], 200)
        self.assertEqual(self._put("/snapshots/map_0_0", b"schema=2\n")[0], 404)
        self.assertEqual(self._put("/geheim123/snapshots/map_0_0", b"schema=2\n")[0], 200)
        # Prefix muss exakt sein, kein Teilstring.
        self.assertEqual(self._get("/geheim123extra/maps.json")[0], 404)

    def test_mirror_uses_base_url_and_secret(self) -> None:
        _, body = self._get("/geheim123/json.php?category=map&springname=Cell%20A1%200.1")
        result = json.loads(body)[0]
        self.assertEqual(
            result["mirrors"][0],
            "http://maps.example.org:8605/geheim123/maps/cell-a1.sd7",
        )


class _FakeRelayManager:
    """Minimaler RelayManager-Ersatz: sagt nur, ob er Sessions fahren kann und
    welche gerade laufen. Mehr braucht die Presence-Wahrheit nicht."""

    def __init__(self, ready: bool, sessions: dict[str, int] | None = None) -> None:
        self._ready = ready
        self._sessions = sessions or {}

    def dedicated_ready(self) -> bool:
        return self._ready

    def session_for(self, cell_id: str):
        port = self._sessions.get(cell_id)
        if port is None:
            return None
        return SimpleNamespace(cell_id=cell_id, port=port, session_dir=None)


class PresenceRelayTruthTest(ServerTestBase):
    """Presence darf nicht behaupten, eine Zelle sei gehostet, wenn die Session
    dafuer gar nicht mehr laeuft.

    Gemessener Engine-Fakt (2026-07-31): der spring-dedicated beendet sich
    SOFORT, sobald der letzte Client weg ist ("No clients connected, shutting
    down server"). Die Presence-Datei lebt danach aber noch bis zu
    PRESENCE_TTL_SECONDS weiter -- und wer in diesem Fenster die Zelle betritt,
    wird auf eine tote Adresse geschickt. Genau das ist Symptom 3 aus der
    2-PC-Abnahme ("obwohl kein Spieler hostet, moechte der sich verbinden").

    Regel: laeuft eine Relay-Session, IST die Zelle gehostet (und der Port der
    Session ist die Wahrheit). Kann der Server Relay-Sessions fahren und es
    laeuft keine, ist sie NICHT gehostet -- egal wie frisch der Heartbeat ist.
    Ohne Relay-Faehigkeit (Dev/LAN) bleibt der Heartbeat mit seiner TTL die
    einzige Quelle.
    """

    def _put_presence(self, cell: str = "map_0_0", port: int = 8452) -> None:
        status, body = self._put(
            f"/presence/{cell}",
            json.dumps({"host_ip": "10.0.0.2", "host_port": port, "player_name": "PC1"}).encode(),
            "application/json",
        )
        self.assertEqual(status, 200, body)

    def test_fresh_presence_without_relay_session_is_not_hosted(self) -> None:
        self.server.relay_manager = _FakeRelayManager(ready=True, sessions={})
        self._put_presence()
        payload = json.loads(self._get("/presence/map_0_0")[1])
        self.assertFalse(payload["hosted"], payload)
        self.assertEqual(payload.get("reason"), "no relay session")

    def test_running_relay_session_is_hosted_with_session_port(self) -> None:
        self.server.relay_manager = _FakeRelayManager(ready=True, sessions={"map_0_0": 8460})
        self._put_presence(port=8452)
        payload = json.loads(self._get("/presence/map_0_0")[1])
        self.assertTrue(payload["hosted"], payload)
        # Der Port der laufenden Session gewinnt gegen den gespeicherten.
        self.assertEqual(payload["host_port"], 8460)
        self.assertEqual(payload["host_ip"], "10.0.0.2")
        self.assertEqual(payload["player_name"], "PC1")

    def test_running_session_outlives_expired_heartbeat(self) -> None:
        # Host geht, Gast spielt weiter: der Heartbeat verfaellt, die Session
        # laeuft. Die Zelle ist gehostet -- sonst betraete der naechste Spieler
        # sie als "Host" und wuerde ihr Offline-Zeit anrechnen, obwohl sie laeuft.
        self.server.relay_manager = _FakeRelayManager(ready=True, sessions={"map_0_0": 8461})
        self._put_presence()
        saved = self.presence_dir / "map_0_0.json"
        stale = json.loads(saved.read_text(encoding="utf-8"))
        stale["updated_at"] = 1
        saved.write_text(json.dumps(stale), encoding="utf-8")
        payload = json.loads(self._get("/presence/map_0_0")[1])
        self.assertTrue(payload["hosted"], payload)
        self.assertEqual(payload["host_port"], 8461)

    def test_running_session_without_any_presence_file(self) -> None:
        # Der Initiator hat sich abgemeldet (Presence geloescht), ein Gast spielt
        # weiter. Der Client braucht hier BEIDES: ohne host_ip haelt er die Zelle
        # fuer frei, betritt sie als Host und rechnet ihr Offline-Zeit an --
        # obwohl sie gerade laeuft. Die Session laeuft auf DIESEM Server, also
        # kann er die Adresse selbst beantworten.
        self.server.relay_manager = _FakeRelayManager(ready=True, sessions={"map_9_9": 8462})
        payload = json.loads(self._get("/presence/map_9_9")[1])
        self.assertTrue(payload["hosted"], payload)
        self.assertEqual(payload["host_port"], 8462)
        self.assertTrue(payload.get("host_ip"), payload)

    def test_running_session_keeps_known_host_ip(self) -> None:
        # Steht schon eine Adresse in der Presence, bleibt sie stehen -- hinter
        # einem Proxy/einer Domain ist sie genauer als der Host-Header.
        self.server.relay_manager = _FakeRelayManager(ready=True, sessions={"map_0_0": 8463})
        self._put_presence()
        payload = json.loads(self._get("/presence/map_0_0")[1])
        self.assertEqual(payload["host_ip"], "10.0.0.2")
        self.assertEqual(payload["host_port"], 8463)

    def test_without_relay_capability_heartbeat_still_decides(self) -> None:
        # Dev/LAN ohne dedicated-Binary: alles laeuft wie vorher ueber die TTL.
        self.server.relay_manager = _FakeRelayManager(ready=False, sessions={})
        self._put_presence()
        payload = json.loads(self._get("/presence/map_0_0")[1])
        self.assertTrue(payload["hosted"], payload)
        self.assertEqual(payload["host_port"], 8452)

        saved = self.presence_dir / "map_0_0.json"
        stale = json.loads(saved.read_text(encoding="utf-8"))
        stale["updated_at"] = 1
        saved.write_text(json.dumps(stale), encoding="utf-8")
        self.assertFalse(json.loads(self._get("/presence/map_0_0")[1])["hosted"])


if __name__ == "__main__":
    unittest.main()
