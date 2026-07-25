from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from map_server.index import MapIndex
from map_server.server import create_server


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
            relay_run_dir=maps_dir / "relay",
        )
        self.snapshots_dir = self.server.snapshots_dir
        self.presence_dir = self.server.presence_dir
        self.logs_dir = self.server.logs_dir
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


if __name__ == "__main__":
    unittest.main()
