from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

INDEX_FILENAME = "maps.json"


@dataclass(frozen=True)
class MapEntry:
    springname: str
    filename: str
    size: int
    md5: str

    def to_dict(self) -> dict[str, object]:
        return {
            "springname": self.springname,
            "filename": self.filename,
            "size": self.size,
            "md5": self.md5,
        }


def parse_index_entries(text: str) -> list[MapEntry]:
    raw = json.loads(text)
    if not isinstance(raw, list):
        raise ValueError("maps.json muss ein JSON-Array sein")
    entries: list[MapEntry] = []
    for item in raw:
        entries.append(MapEntry(
            springname=str(item["springname"]),
            filename=str(item["filename"]),
            size=int(item["size"]),
            md5=str(item["md5"]),
        ))
    return entries


class MapIndex:
    """Index ueber das Maps-Verzeichnis, ausschliesslich aus maps.json.

    Die Datei kommt per sync-map-server (Studio) vom Laptop und wird
    stat-basiert neu eingelesen, sobald rsync sie ersetzt hat -- neue
    Karten erscheinen ohne Server-Neustart. Fehlt/bricht die Datei,
    bleibt der letzte gute Stand aktiv (rsync schreibt atomar).
    """

    def __init__(self, maps_dir: Path):
        self.maps_dir = maps_dir
        self._index_stat: tuple[int, int] | None = None
        self._index_entries: list[MapEntry] = []

    def refresh(self) -> list[MapEntry]:
        index_file = self.maps_dir / INDEX_FILENAME
        if not index_file.is_file():
            if self._index_stat is not None:
                print(f"[map-server] WARNUNG: {INDEX_FILENAME} fehlt -- behalte alten Index", flush=True)
            return self._index_entries
        stat = index_file.stat()
        statkey = (stat.st_mtime_ns, stat.st_size)
        if self._index_stat == statkey:
            return self._index_entries
        try:
            entries = parse_index_entries(index_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError) as error:
            print(f"[map-server] WARNUNG: {INDEX_FILENAME} unlesbar ({error}) -- behalte alten Index", flush=True)
            return self._index_entries
        self._index_stat = statkey
        self._index_entries = entries
        return entries

    def find(self, springname: str) -> MapEntry | None:
        for entry in self.refresh():
            if entry.springname == springname:
                return entry
        return None
