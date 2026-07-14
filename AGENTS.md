# conatus-map-server — Agenten-Einstieg

Schlankes **Deployment-Repo** fuer den VPS-Map-Server. Projekt-Kopf und alle
Regeln: `/home/chede/spring-data/games/conatus-studio/` (`AI_RULES.md`).

## Kernregeln (verbindlich)

- **Public Repo**: keine Secrets, keine Tokens, keine privaten Pfade/IPs
  committen. Secret/Config liegen NUR auf dem VPS (`/etc/conatus-map-server.env`).
- **Keine Karten/Binaerdateien** ins Repo — Karten laufen per rsync
  (`sync-map-server` im Studio).
- **Push = Deployment**: der VPS zieht `origin/main` automatisch (Timer).
  Vor jedem Push muessen die Tests gruen sein (`python3 -m unittest discover -s tests`).
  Push nur auf explizite Nutzer-Anfrage (wie im ganzen Workspace).
- **stdlib-only**: keine Abhaengigkeiten einfuehren; Publish-/Sync-Tooling
  gehoert ins Studio (`src/conatus_studio/map_server/`), nicht hierher.
- Der springfiles-JSON-Vertrag ist extern fixiert (RecoilEngine
  `tools/pr-downloader/.../HttpDownloader.cpp` `ParseResult`) — nicht
  "verbessern".

## Sprache

Deutsch mit dem Nutzer, Code/Kommentare an Entwickler Englisch/Deutsch wie
im Workspace ueblich (siehe `AI_RULES.md` Sprachregeln).
