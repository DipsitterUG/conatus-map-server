"""Relay-Manager: startet/verwaltet spring-dedicated-Prozesse pro MapCell.

Conatus-Baustein internet-hosting: beide Spieler verbinden sich als normale
Clients (IsHost=0) zu einem dedicated-Prozess auf dem VPS, statt dass einer
selbst hostet. Der Manager bekommt vom Launcher des initiierenden Spielers
das komplette Startscript (inkl. MapHash/ModHash -> "reduced mode", der VPS
braucht KEINE Spiel-/Map-Archive, nur Engine-Binary + base/-Archive; ohne
springcontent.sdz kann die Engine nicht einmal das Script parsen).

Verifizierte Engine-Fakten (2026-07-25, lokaler WSL2-Test):
- Script braucht IsHost=1 (der dedicated-Prozess IST der Host).
- MapHash/ModHash muessen exakt 128 Hex-Zeichen sein (sha512), sonst
  verlangt die Engine die Archive.
- SPRING_DATADIR muss das Engine-Verzeichnis (mit base/) enthalten;
  SPRING_WRITEDIR/HOME zeigen auf das Session-Verzeichnis.
- Auto-Exit bei "keine Clients" funktioniert doch, und zwar SOFORT: der Server
  meldet "No clients connected, shutting down server" und ist ~2 s spaeter weg
  (gemessen 2026-07-31 im authority-vacancy-Smoke; die aeltere Notiz hier
  behauptete das Gegenteil). Der Reaper bleibt trotzdem noetig -- fuer
  Prozesse, die beim Start haengen bleiben, und als max_age-Deckel. Wichtiger:
  daraus folgt "Session laeuft" == "Zelle wird gerade gespielt", worauf sich
  die Presence-Wahrheit in server.py stuetzt.
"""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

MAX_SCRIPT_BYTES = 256 * 1024
# Platzhalter, den das Menue ins Script schreibt; der Manager ersetzt ihn
# durch den zugewiesenen Port (deterministisch, kein TDF-Parsen noetig).
PORT_MARKER = "HostPort=0;"
DEFAULT_PORT_RANGE = "8452-8461"
DEFAULT_MAX_AGE_SECONDS = 6 * 3600
SPAWN_GRACE_SECONDS = 1.0
DEDICATED_BINARY = "spring-dedicated"

# Explizite Config pro Session. Das dedicated-Binary hat eigene Defaults
# (ConfigVariable.dedicatedValue in der Engine), die von der normalen Engine
# abweichen -- beide hier gesetzten Werte sind genau solche Faelle:
#
#  * AllowSpectatorJoin: defaultValue(true), aber dedicatedValue(FALSE).
#    Ohne diesen Wert weist der Server jeden Client ab, dessen Name nicht im
#    Startscript steht ("User name not authorized to connect", GameServer.cpp).
#    Unser Script kennt nur den Initiator als PLAYER0 -- der Async-Flow laesst
#    Gaeste bewusst spaeter und unter unbekanntem Namen dazukommen, sie werden
#    als "~Name"-Spectator aufgenommen und vom guest_promotion-Gadget befoerdert.
#    Beim lokalen IsHost=1-Pfad griff der true-Default, deshalb fiel das vorher
#    nie auf.
#  * ServerRecordDemos: defaultValue(false), aber dedicatedValue(TRUE). Der
#    Reaper raeumt nur Prozesse ab, nicht die Session-Verzeichnisse -- Demos
#    wuerden sich auf dem kleinen VPS unbegrenzt sammeln. Fuer Desync-Analysen
#    hier bewusst auf 1 stellen und danach wieder aus.
#
# StoreDefaultSettings ist der Grund, warum die Datei ueberhaupt wirkt, und darf
# NICHT entfernt werden: ConfigHandlerImpl::FinalizeLoad ruft sonst
# RemoveDefaults(), und das vergleicht die Datei gegen die NORMALE
# DefaultConfigSource -- ohne zu wissen, dass die DedicatedConfigSource in der
# Quellen-Kette zwischen Datei und Defaults sitzt. Beide Werte oben entsprechen
# genau dem normalen Default, wuerden also als "ueberfluessig" aus der Datei
# geloescht, worauf wieder der abweichende Dedicated-Wert greift. Netto: ein
# Config-Key mit dedicatedValue != defaultValue laesst sich per Datei sonst
# nicht auf seinen normalen Default setzen. StoreDefaultSettings=1 ueberspringt
# RemoveDefaults und macht die Werte damit stabil.
CONFIG_TEMPLATE = """StoreDefaultSettings = 1
AllowSpectatorJoin = 1
ServerRecordDemos = 0
"""


class RelaySession:
    def __init__(self, cell_id: str, port: int, proc: subprocess.Popen,
                 session_dir: Path) -> None:
        self.cell_id = cell_id
        self.port = port
        self.proc = proc
        self.session_dir = session_dir
        self.started_at = int(time.time())

    def is_running(self) -> bool:
        return self.proc.poll() is None

    def to_dict(self) -> dict[str, object]:
        return {
            "cell_id": self.cell_id,
            "port": self.port,
            "running": self.is_running(),
            "started_at": self.started_at,
            "pid": self.proc.pid,
        }


def parse_port_range(text: str) -> list[int]:
    lo_text, _, hi_text = text.partition("-")
    lo = int(lo_text)
    hi = int(hi_text) if hi_text else lo
    if not (0 < lo <= hi <= 65535):
        raise ValueError(f"invalid port range: {text}")
    return list(range(lo, hi + 1))


class RelayManager:
    """Ein dedicated-Prozess pro Zelle, Ports aus einem kleinen Pool.

    MVP bewusst grob: kein Autoscaling, keine Session-Historie. Der Reaper
    raeumt beendete Prozesse und Ueberlaeufer (max_age) ab und laeuft lazy
    vor jeder Manager-Operation.
    """

    def __init__(self, engine_dir: Path | None, run_dir: Path,
                 port_range: str = DEFAULT_PORT_RANGE,
                 max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS) -> None:
        self.engine_dir = engine_dir
        self.run_dir = run_dir
        self.ports = parse_port_range(port_range)
        self.max_age_seconds = max_age_seconds
        self.sessions: dict[str, RelaySession] = {}
        self.lock = threading.Lock()

    def _binary(self) -> Path | None:
        if self.engine_dir is None:
            return None
        return self.engine_dir / DEDICATED_BINARY

    def dedicated_ready(self) -> bool:
        binary = self._binary()
        return binary is not None and binary.is_file()

    def _reap_locked(self) -> None:
        now = int(time.time())
        for cell_id in list(self.sessions):
            session = self.sessions[cell_id]
            if not session.is_running():
                del self.sessions[cell_id]
                continue
            if now - session.started_at > self.max_age_seconds:
                self._kill(session)
                del self.sessions[cell_id]

    @staticmethod
    def _kill(session: RelaySession) -> None:
        try:
            session.proc.terminate()
            try:
                session.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                session.proc.kill()
                session.proc.wait(timeout=5)
        except OSError:
            pass

    def _free_port_locked(self) -> int | None:
        used = {s.port for s in self.sessions.values()}
        for port in self.ports:
            if port not in used:
                return port
        return None

    def health(self) -> dict[str, object]:
        with self.lock:
            self._reap_locked()
            return {
                "ok": True,
                "dedicated_ready": self.dedicated_ready(),
                "active_sessions": len(self.sessions),
                "ports_free": len(self.ports) - len(self.sessions),
            }

    def list_sessions(self) -> dict[str, object]:
        with self.lock:
            self._reap_locked()
            return {"sessions": [s.to_dict() for s in self.sessions.values()]}

    def session_for(self, cell_id: str) -> RelaySession | None:
        """Laufende Session dieser Zelle (oder None).

        Das ist die einzige verlaessliche Antwort auf "wird diese Zelle gerade
        gespielt": die Engine beendet den dedicated SOFORT, sobald der letzte
        Client weg ist ("No clients connected, shutting down server", gemessen
        2026-07-31). Die Presence-Datei dagegen lebt noch bis zu ihrer TTL
        weiter und schickt Beitretende in diesem Fenster auf eine tote Adresse.
        """
        with self.lock:
            self._reap_locked()
            session = self.sessions.get(cell_id)
            if session is None or not session.is_running():
                return None
            return session

    def start(self, cell_id: str, script_text: str) -> tuple[int, dict[str, object]]:
        """Startet einen dedicated fuer die Zelle (oder meldet den laufenden).

        Rueckgabe: (http_status, payload). payload enthaelt bei Erfolg den
        zugewiesenen UDP-Port; die Client-Seite kombiniert ihn mit dem Host
        der Relay-URL.
        """
        if not self.dedicated_ready():
            return 503, {"error": "dedicated binary not available", "cell_id": cell_id}
        if PORT_MARKER not in script_text:
            return 400, {"error": f"script must contain '{PORT_MARKER}' marker",
                         "cell_id": cell_id}

        with self.lock:
            self._reap_locked()
            existing = self.sessions.get(cell_id)
            if existing is not None and existing.is_running():
                return 200, {"ok": True, "cell_id": cell_id, "port": existing.port,
                             "already_running": True}

            port = self._free_port_locked()
            if port is None:
                return 503, {"error": "no free relay port", "cell_id": cell_id}

            session_dir = self.run_dir / cell_id
            session_dir.mkdir(parents=True, exist_ok=True)
            script_path = session_dir / "script.txt"
            script_path.write_text(
                script_text.replace(PORT_MARKER, f"HostPort={port};", 1),
                encoding="utf-8")

            # Config MUSS explizit mit: das dedicated-Binary hat abweichende
            # Defaults (ConfigVariable dedicatedValue), und zwei davon treffen uns.
            config_path = session_dir / "springsettings.cfg"
            config_path.write_text(CONFIG_TEMPLATE, encoding="utf-8")

            log_path = session_dir / "dedicated.log"
            env = {
                "HOME": str(session_dir),
                "SPRING_DATADIR": str(self.engine_dir),
                "SPRING_WRITEDIR": str(session_dir),
                "SPRING_NOCOLOR": "1",
            }
            try:
                with log_path.open("wb") as log_handle:
                    proc = subprocess.Popen(
                        [str(self._binary()), "--config", str(config_path),
                         str(script_path)],
                        cwd=session_dir, env=env,
                        stdout=log_handle, stderr=subprocess.STDOUT,
                        stdin=subprocess.DEVNULL)
            except OSError as error:
                return 500, {"error": f"spawn failed: {error}", "cell_id": cell_id}

            session = RelaySession(cell_id, port, proc, session_dir)
            self.sessions[cell_id] = session

        # Kurze Gnadenfrist ausserhalb des Locks: stirbt der Prozess sofort
        # (Script-Fehler), soll der Client eine klare Fehlermeldung sehen
        # statt eines Verbindungs-Timeouts.
        time.sleep(SPAWN_GRACE_SECONDS)
        if session.is_running():
            return 200, {"ok": True, "cell_id": cell_id, "port": port,
                         "already_running": False}

        with self.lock:
            self.sessions.pop(cell_id, None)
        return 500, {"error": "dedicated exited immediately",
                     "cell_id": cell_id, "log_tail": self._log_tail(session_dir)}

    @staticmethod
    def _log_tail(session_dir: Path, limit: int = 800) -> str:
        # infolog.txt traegt die eigentliche Fehlermeldung der Engine.
        for name in ("infolog.txt", "dedicated.log"):
            path = session_dir / name
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if text.strip():
                    return text[-limit:]
        return ""

    def stop(self, cell_id: str) -> tuple[int, dict[str, object]]:
        with self.lock:
            self._reap_locked()
            session = self.sessions.pop(cell_id, None)
            if session is None:
                return 200, {"ok": True, "cell_id": cell_id, "stopped": False}
            self._kill(session)
            return 200, {"ok": True, "cell_id": cell_id, "stopped": True}

    def shutdown(self) -> None:
        with self.lock:
            for session in self.sessions.values():
                self._kill(session)
            self.sessions.clear()
