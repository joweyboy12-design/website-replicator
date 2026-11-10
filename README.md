# Website Replicator

Capture any URL (SPA, WebGL/Three.js, Canvas, WASM, workers) and replay it 100% offline.

**Start:** double-click `START.bat` (first run creates a venv in `%LOCALAPPDATA%\WebsiteReplicator\venv`
and downloads Playwright Chromium). Archives are stored in `%LOCALAPPDATA%\WebsiteReplicator\archives`
(override with the `REPLICATOR_HOME` environment variable).

## How it works

| Layer | File | What it does |
|---|---|---|
| Capture | `replicator/capture.py` | Chromium via Playwright. *Every* request goes through `context.route`; we fetch it ourselves (`route.fetch`), store the raw bytes, and fulfil the browser with those same bytes. Range/conditional headers are stripped so media is stored whole, never as 206/304 fragments. Auto-scroll, mouse/wheel interaction, a WebGL settle wait and a "network quiet" detector catch lazy assets. Optional same-origin crawl (depth / max pages). |
| Storage | `replicator/store.py` | SQLite, one row per exchange: method, URL, status, request/response headers, **body as BLOB** (never decoded). Export to HAR 1.2 (base64 bodies). |
| Play Offline | `replicator/server.py` | Local HTTP server on `127.0.0.1:<port>` (real origin, so no `file://` CORS/tainted-canvas problems). Same-origin paths map 1:1 to the original origin; a Service Worker reroutes cross-origin requests (CDNs, APIs, fonts) to `/__ext__/…`. JS bundles are never rewritten. Supports Range requests (video seeking), SPA route fallback and cache-busting query fallback. Misses are logged and listed at `/__replicator/status`. |
| Play in Chromium (exact) | `replicator/replay.py` | Opens the **original** URL in Playwright Chromium and answers every request from the archive, so `location.origin`, storage and host checks behave exactly like online. |

## CLI

```
python -m replicator capture https://example.com --depth 1 --wait 10
python -m replicator serve <archive>
python -m replicator exact <archive>
python -m replicator list
python -m replicator har <archive> out.har
```

## Limits

- Live WebSockets / WebRTC / server-sent streams cannot be replayed (logged during capture).
- Requests whose URL is built from random values or the current time fall back to "same path, any query"; truly server-computed answers (search, login) only work for requests made during capture.
- "Play Offline" needs Service Worker support: use Chrome or Edge (not private/incognito windows in Firefox). If it is unavailable, use "Play in Chromium (exact)".
- Only archive sites you have the right to copy.
