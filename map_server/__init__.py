"""Conatus Map-Server: statische .sd7-Auslieferung fuer Launcher + Engine.

Deployment-Quelle fuer den VPS (Baustein map-server-vps). Laeuft stdlib-only
im Index-Datei-Modus: maps.json (vom Laptop via sync-map-server gespiegelt)
ist die Wahrheit, keine Archiv-Inspektion noetig. Die Publish-/Sync-Tools
(publish-map, build-map-index, sync-map-server) leben im Studio-Repo.
"""
