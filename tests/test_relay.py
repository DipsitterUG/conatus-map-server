from __future__ import annotations

import json
import os
import stat
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from map_server import relay
from map_server.relay import RelayManager
from map_server.server import create_server


def _write_stub_binary(engine_dir: Path, body: str) -> None:
    """Fake-spring-dedicated: Shell-Skript statt Engine, testet nur den Manager."""
    engine_dir.mkdir(parents=True, exist_ok=True)
    binary = engine_dir / "spring-dedicated"
    binary.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)


SCRIPT = "[GAME]\n{\n\tHostPort=0;\n\tIsHost=1;\n}\n"


class RelayManagerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.engine_dir = self.root / "engine"
        self.run_dir = self.root / "run"
        # Kurze Gnadenfrist, damit die Tests nicht pro Start 1s warten.
        self._old_grace = relay.SPAWN_GRACE_SECONDS
        relay.SPAWN_GRACE_SECONDS = 0.2

    def tearDown(self) -> None:
        relay.SPAWN_GRACE_SECONDS = self._old_grace
        if hasattr(self, "manager"):
            self.manager.shutdown()
        self._tmp.cleanup()

    def _manager(self, ports: str = "20000-20001") -> RelayManager:
        self.manager = RelayManager(self.engine_dir, self.run_dir, port_range=ports)
        return self.manager

    def test_health_without_binary(self) -> None:
        manager = self._manager()
        health = manager.health()
        self.assertTrue(health["ok"])
        self.assertFalse(health["dedicated_ready"])
        status, payload = manager.start("cell-a1", SCRIPT)
        self.assertEqual(status, 503)
        self.assertIn("not available", payload["error"])

    def test_start_replaces_port_marker_and_runs(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager()
        status, payload = manager.start("cell-a1", SCRIPT)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["port"], 20000)
        self.assertFalse(payload["already_running"])
        written = (self.run_dir / "cell-a1" / "script.txt").read_text(encoding="utf-8")
        self.assertIn("HostPort=20000;", written)
        self.assertNotIn("HostPort=0;", written)
        health = manager.health()
        self.assertEqual(health["active_sessions"], 1)
        self.assertEqual(health["ports_free"], 1)

    def test_start_twice_reports_already_running(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager()
        manager.start("cell-a1", SCRIPT)
        status, payload = manager.start("cell-a1", SCRIPT)
        self.assertEqual(status, 200)
        self.assertTrue(payload["already_running"])
        self.assertEqual(payload["port"], 20000)

    def test_port_pool_exhaustion(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager(ports="20000-20000")
        manager.start("cell-a1", SCRIPT)
        status, payload = manager.start("cell-b2", SCRIPT)
        self.assertEqual(status, 503)
        self.assertIn("no free relay port", payload["error"])

    def test_script_without_marker_rejected(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager()
        status, payload = manager.start("cell-a1", "[GAME]\n{\n\tHostPort=8452;\n}\n")
        self.assertEqual(status, 400)
        self.assertIn("marker", payload["error"])

    def test_immediate_exit_reports_log_tail(self) -> None:
        _write_stub_binary(self.engine_dir, 'echo "boom kaputt"; exit 3')
        manager = self._manager()
        status, payload = manager.start("cell-a1", SCRIPT)
        self.assertEqual(status, 500)
        self.assertIn("exited immediately", payload["error"])
        self.assertIn("boom kaputt", payload["log_tail"])
        self.assertEqual(manager.health()["active_sessions"], 0)

    def test_stop_kills_session(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager()
        manager.start("cell-a1", SCRIPT)
        status, payload = manager.stop("cell-a1")
        self.assertEqual(status, 200)
        self.assertTrue(payload["stopped"])
        self.assertEqual(manager.health()["active_sessions"], 0)
        status, payload = manager.stop("cell-a1")
        self.assertFalse(payload["stopped"])

    def test_reap_removes_dead_sessions(self) -> None:
        # Prozess lebt die Gnadenfrist knapp ueber, stirbt danach von selbst.
        _write_stub_binary(self.engine_dir, "sleep 0.4")
        manager = self._manager()
        status, _ = manager.start("cell-a1", SCRIPT)
        self.assertEqual(status, 200)
        time.sleep(0.5)
        self.assertEqual(manager.health()["active_sessions"], 0)

    def test_max_age_kills_old_sessions(self) -> None:
        _write_stub_binary(self.engine_dir, "sleep 30")
        manager = self._manager()
        manager.max_age_seconds = 0
        manager.start("cell-a1", SCRIPT)
        time.sleep(1.1)  # started_at ist sekundengenau
        self.assertEqual(manager.health()["active_sessions"], 0)


class RelayHttpTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        maps_dir = root / "maps"
        maps_dir.mkdir()
        self.engine_dir = root / "engine"
        _write_stub_binary(self.engine_dir, "sleep 30")
        self._old_grace = relay.SPAWN_GRACE_SECONDS
        relay.SPAWN_GRACE_SECONDS = 0.2
        self.server = create_server(
            maps_dir, host="127.0.0.1", port=0,
            logs_dir=root / "logs",
            relay_engine_dir=self.engine_dir,
            relay_run_dir=root / "relay",
            relay_ports="20100-20101",
        )
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        relay.SPAWN_GRACE_SECONDS = self._old_grace
        self.server.relay_manager.shutdown()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self._tmp.cleanup()

    def _request(self, method: str, path: str, data: bytes | None = None) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            body = error.read()
            error.close()
            return error.code, json.loads(body)

    def test_full_session_lifecycle_over_http(self) -> None:
        status, health = self._request("GET", "/relay/health")
        self.assertEqual(status, 200)
        self.assertTrue(health["dedicated_ready"])

        status, payload = self._request(
            "PUT", "/relay/sessions/cell-a1", SCRIPT.encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(payload["port"], 20100)

        status, listing = self._request("GET", "/relay/sessions")
        self.assertEqual(len(listing["sessions"]), 1)
        self.assertEqual(listing["sessions"][0]["cell_id"], "cell-a1")

        status, payload = self._request("DELETE", "/relay/sessions/cell-a1")
        self.assertEqual(status, 200)
        self.assertTrue(payload["stopped"])

    def test_invalid_cell_and_empty_script(self) -> None:
        status, payload = self._request(
            "PUT", "/relay/sessions/b%C3%B6se", b"x")
        self.assertEqual(status, 400)
        status, payload = self._request(
            "PUT", "/relay/sessions/cell-a1", b"   ")
        self.assertEqual(status, 400)
        self.assertIn("empty script", payload["error"])


if __name__ == "__main__":
    unittest.main()
