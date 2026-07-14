from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from map_server.index import INDEX_FILENAME, MapEntry, MapIndex

DEFAULT_PORT = 8605


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


class MapServerHandler(BaseHTTPRequestHandler):
    # ThreadingHTTPServer-Attribute, von create_server gesetzt:
    #   self.server.map_index: MapIndex
    #   self.server.base_url: str | None   (oeffentliche Basis OHNE Secret-Anteil)
    #   self.server.secret: str | None     (Pfad-Prefix als Minimal-Zugriffsschutz)

    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            self._route()
        except BrokenPipeError:
            pass

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
            self._serve_map_file(unquote(path[len("/maps/"):]))
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
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((host, port), MapServerHandler)
    server.map_index = MapIndex(maps_dir)
    server.base_url = base_url.rstrip("/") if base_url else None
    server.secret = secret.strip("/") if secret else None
    return server


def serve_maps(
    maps_dir: Path,
    host: str = "0.0.0.0",
    port: int = DEFAULT_PORT,
    base_url: str | None = None,
    secret: str | None = None,
) -> None:
    maps_dir.mkdir(parents=True, exist_ok=True)
    server = create_server(maps_dir, host, port, base_url=base_url, secret=secret)
    entries = server.map_index.refresh()
    if not (maps_dir / INDEX_FILENAME).is_file():
        print(f"[map-server] HINWEIS: {maps_dir}/{INDEX_FILENAME} fehlt noch "
              f"-- erst sync-map-server vom Laptop ausfuehren", flush=True)
    print(f"[map-server] {len(entries)} Karten in {maps_dir}", flush=True)
    for entry in entries:
        print(f"[map-server]   {entry.springname} ({entry.filename}, {entry.size} bytes)", flush=True)
    prefix = ("/" + server.secret) if server.secret else ""
    print(f"[map-server] lauscht auf http://{host}:{port}{prefix} "
          f"(maps.json | json.php?category=map&springname=... | maps/<datei>)", flush=True)
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
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--base-url", default=None,
                        help="Oeffentliche Basis-URL ohne Secret (z.B. http://maps.example.org:8605)")
    parser.add_argument("--secret", default=None,
                        help="Pfad-Prefix als Minimal-Zugriffsschutz (Clients haengen ihn an die URL)")
    args = parser.parse_args(argv)
    serve_maps(args.maps_dir, host=args.host, port=args.port,
               base_url=args.base_url, secret=args.secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
