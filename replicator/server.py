"""Offline replay server for any normal browser (Chrome / Edge recommended).

How it stays faithful without touching the site's JavaScript:

* The archived site is served from ``http://127.0.0.1:PORT`` - a real HTTP origin,
  so no ``file://`` CORS / tainted-canvas / worker restrictions.
* Same-origin paths map 1:1 to the original origin (``/assets/a.js`` ->
  ``https://site.com/assets/a.js``), so chunk loaders and relative paths just work.
* A tiny Service Worker is installed first (``/__boot``). It intercepts every
  *cross-origin* request the page makes (CDNs, APIs, fonts, model hosts...) and
  re-routes it to ``/__ext__/<scheme>/<host>/<path>``, which the server answers
  from the archive. Bundles are never rewritten - only the network is.
"""
from __future__ import annotations

import html
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import quote, urlsplit

from .store import Archive
from .util import (origin_of, path_query, pick_port, preferred_port, replay_headers,
                   short)

SW_PATH = "/__replicator_sw.js"

SERVICE_WORKER_JS = r"""
// Website Replicator service worker: re-route cross-origin requests to the local archive.
const ORIGIN = self.location.origin;
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', event => {
  let u;
  try { u = new URL(event.request.url); } catch (_) { return; }
  if (u.origin === ORIGIN) return;                  // already local
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return;
  const target = ORIGIN + '/__ext__/' + u.protocol.slice(0, -1) + '/' + u.host + u.pathname + u.search;
  event.respondWith(forward(event.request, target));
});
async function forward(req, target) {
  const init = { method: req.method, headers: req.headers, redirect: 'follow', credentials: 'same-origin' };
  if (req.method !== 'GET' && req.method !== 'HEAD') init.body = await req.arrayBuffer();
  try {
    const r = await fetch(target, init);
    // Re-wrap so the response keeps the ORIGINAL url: ES-module identity, import.meta.url,
    // relative imports and CSS url() then resolve exactly as online (no duplicate module graphs).
    const status = r.status === 0 ? 200 : r.status;
    const nullBody = status === 204 || status === 304 || req.method === 'HEAD';
    return new Response(nullBody ? null : r.body, { status, statusText: r.statusText, headers: r.headers });
  } catch (e) { return new Response('offline: ' + e, { status: 504 }); }
}
"""

BOOT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Loading offline copy...</title>
<style>body{font:15px system-ui;background:#111;color:#ddd;display:grid;place-items:center;height:100vh;margin:0}</style>
</head><body><div id="m">Preparing offline copy...</div><script>
(async () => {
  const to = new URLSearchParams(location.search).get('to') || '/';
  if (!('serviceWorker' in navigator)) {
    document.getElementById('m').textContent =
      'This browser has no Service Worker support here (private mode?). Cross-origin assets may be missing.';
    setTimeout(() => location.replace(to), 1500); return;
  }
  await navigator.serviceWorker.register('__SW__', { scope: '/' });
  await navigator.serviceWorker.ready;
  location.replace(to);
})().catch(e => {
  // Same-origin assets still work without the SW; only cross-origin (CDN) requests need it.
  document.getElementById('m').textContent = 'Service worker unavailable (' + e.message +
    '). Opening anyway - cross-origin assets may be missing. Use Chrome/Edge or "Play in Chromium".';
  setTimeout(() => location.replace(new URLSearchParams(location.search).get('to') || '/'), 2500);
});
</script></body></html>""".replace("__SW__", SW_PATH)

# Injected (as the first thing in <head>) into every archived HTML document.
GUARD_JS = r"""(function(){try{
var sw=navigator.serviceWorker; if(!sw) return;
var reg=sw.register.bind(sw), SW='__SW__';
sw.register=function(u,o){ if(String(u).indexOf(SW)>=0) return reg(u,o);
  return sw.getRegistration('/').then(function(r){return r||reg(SW,{scope:'/'});}); };
var K='__replicator_boot';
if(!sw.controller){ var n=+(sessionStorage.getItem(K)||0);
  if(n<3){ sessionStorage.setItem(K,n+1);
    location.replace('/__boot?to='+encodeURIComponent(location.pathname+location.search+location.hash));
    try{window.stop();}catch(e){} return; } }
else sessionStorage.removeItem(K);
var O=location.origin;
function fix(el){ var s=el.getAttribute('src'); if(!s) return; var u;
  try{u=new URL(s,location.href);}catch(e){return;}
  if(u.origin!==O&&/^https?:$/.test(u.protocol))
    el.setAttribute('src',O+'/__ext__/'+u.protocol.slice(0,-1)+'/'+u.host+u.pathname+u.search+u.hash); }
new MutationObserver(function(ms){ms.forEach(function(m){
  if(m.type==='attributes'){ if(m.target.tagName==='IFRAME') fix(m.target); return; }
  m.addedNodes.forEach(function(n){ if(n.nodeType!==1) return;
    if(n.tagName==='IFRAME') fix(n); if(n.querySelectorAll) n.querySelectorAll('iframe').forEach(fix); });
});}).observe(document.documentElement,{childList:true,subtree:true,attributes:true,attributeFilter:['src']});
// Keep link clicks and form posts to captured sites (e.g. https://site.com/page) inside the local copy.
var P=__PRIMARY__, KNOWN=__ORIGINS__;
function local(href){ var u; try{u=new URL(href,location.href);}catch(e){return null;}
  if(!/^https?:$/.test(u.protocol)||u.origin===O||KNOWN.indexOf(u.origin)<0) return null;
  if(P.indexOf(u.origin)>=0) return O+u.pathname+u.search+u.hash;
  return O+'/__ext__/'+u.protocol.slice(0,-1)+'/'+u.host+u.pathname+u.search+u.hash; }
function relink(e){ var a=e.target&&e.target.closest&&e.target.closest('a[href],area[href]'); if(!a) return;
  var l=local(a.href); if(l) a.setAttribute('href',l); }
['pointerdown','mousedown','keydown','focusin','click'].forEach(function(t){document.addEventListener(t,relink,true);});
document.addEventListener('submit',function(e){ var f=e.target; if(!f||!f.getAttribute) return;
  var l=local(f.getAttribute('action')||location.href); if(l) f.setAttribute('action',l); },true);
var wo=window.open; window.open=function(u){ var a=[].slice.call(arguments); if(u){var l=local(String(u)); if(l) a[0]=l;}
  return wo.apply(window,a); };
}catch(e){}})();""".replace("__SW__", SW_PATH)


def guard_tag(primary: list[str], origins: list[str]) -> bytes:
    js = GUARD_JS.replace("__PRIMARY__", json.dumps(primary)).replace("__ORIGINS__", json.dumps(origins))
    return b"<script>" + js.encode() + b"</script>"


def inject_guard(body: bytes, tag: bytes) -> bytes:
    """Insert the guard script at the top of <head> - pure byte surgery, no decoding."""
    head = body[:262144]
    for pat in (rb"<head\b[^>]*>", rb"<html\b[^>]*>", rb"<!doctype[^>]*>"):
        m = re.search(pat, head, re.I)
        if m:
            return body[:m.end()] + tag + body[m.end():]
    return tag + body


SPA_MARKERS = (b'id="root"', b"id='root'", b'id="app"', b"id='app'", b'id="__next"', b'id="__nuxt"',
               b"data-reactroot", b"ng-version", b'id="svelte"', b"data-sveltekit", b'id="___gatsby"')

NOT_SAVED_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Page not saved</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 system-ui;background:#0f1115;color:#e6e6e6}}
main{{max-width:560px;padding:32px}} a{{color:#6ea0ff}} code{{background:#232834;padding:2px 6px;border-radius:4px;word-break:break-all}}</style>
</head><body><main><h1>This page was not saved offline</h1>
<p><code>{url}</code></p>
<p>Only pages visited during the capture are in this copy. Capture again with a higher
<b>Crawl depth</b> (for example 1 or 2) to include linked pages.</p>
<p><a href="{home}">&larr; Back to the start page</a></p></main></body></html>"""


def parse_range(value: str, size: int):
    m = re.fullmatch(r"\s*bytes=(\d*)-(\d*)\s*", value or "")
    if not m or (not m.group(1) and not m.group(2)):
        return None
    s, e = m.groups()
    if s == "":
        start, end = max(0, size - int(e)), size - 1
    else:
        start, end = int(s), (int(e) if e else size - 1)
    end = min(end, size - 1)
    if start >= size or end < start:
        return "unsatisfiable"
    return start, end


class ReplayServer:
    def __init__(self, archive: Archive, port: int | None = None,
                 log: Callable[[str], None] = print):
        self.archive = archive
        self.log = log
        self.misses: list[str] = []
        self.hits = 0
        conn = archive.connect()
        meta = archive.meta(conn)
        origins = [r[0] for r in conn.execute("SELECT DISTINCT origin FROM responses")]
        start_doc = archive.start_document(conn)
        conn.close()
        self.is_spa = bool(start_doc is not None and any(m in (start_doc["body"] or b"")[:500000]
                                                         for m in SPA_MARKERS))
        if "start_url" not in meta:
            raise ValueError("Archive has no start_url - capture did not finish.")
        self.start_url: str = meta["start_url"]
        self.primary_origin: str = meta.get("origin") or origin_of(self.start_url)
        # www./bare variants of the main site count as the same site for links.
        host = urlsplit(self.primary_origin).hostname or ""
        bare = host[4:] if host.startswith("www.") else host
        scheme = urlsplit(self.primary_origin).scheme
        variants = [f"{s}://{h}" for s in (scheme, "http", "https") for h in (bare, "www." + bare)]
        same_site = sorted({self.primary_origin, *variants})
        self._guard = guard_tag(same_site, sorted(set(origins) | set(same_site)))
        self.port = pick_port(port or preferred_port(archive.name))
        self._httpd: ThreadingHTTPServer | None = None
        self._local = threading.local()
        self._conns: list = []

    # ------------------------------------------------------------ lifecycle
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def launch_url(self) -> str:
        s = urlsplit(self.start_url)
        to = path_query(self.start_url) + (f"#{s.fragment}" if s.fragment else "")
        return f"{self.base_url}/__boot?to={quote(to, safe='')}"

    def start(self) -> str:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):  # silence default stderr logging
                pass

            def _go(self):
                try:
                    server._handle(self)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            do_GET = do_HEAD = do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = _go

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = False

        self._httpd = Server(("127.0.0.1", self.port), Handler)
        threading.Thread(target=self._httpd.serve_forever, daemon=True,
                         name=f"replay-{self.archive.name}").start()
        self.log(f"Serving '{self.archive.name}' at {self.base_url}")
        return self.launch_url

    def stop(self) -> None:
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        for c in self._conns:  # release file locks so the archive can be deleted
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()

    # -------------------------------------------------------------- serving
    def _conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            c = self._local.conn = self.archive.connect()
            self._conns.append(c)
        return c

    def _send(self, h, status: int, headers, body: bytes, reason: str | None = None):
        h.send_response(status, reason)
        for k, v in headers:
            h.send_header(k, v)
        h.send_header("Content-Length", str(len(body)))
        h.end_headers()
        if h.command != "HEAD":
            h.wfile.write(body)

    def _handle(self, h: BaseHTTPRequestHandler) -> None:
        path = h.path
        if path.startswith("//"):
            path = "/" + path.lstrip("/")
        method = h.command.upper()

        # ---- built-in endpoints
        if path.split("?")[0] == SW_PATH:
            return self._send(h, 200, [("Content-Type", "text/javascript"), ("Service-Worker-Allowed", "/"),
                                       ("Cache-Control", "no-store")], SERVICE_WORKER_JS.encode())
        if path.startswith("/__boot"):
            return self._send(h, 200, [("Content-Type", "text/html; charset=utf-8"),
                                       ("Cache-Control", "no-store")], BOOT_HTML.encode())
        if path.startswith("/__replicator/ping.js"):  # used by the package's index.html
            js = f"window.__replicator_pong && window.__replicator_pong({json.dumps(self.archive.name)});"
            return self._send(h, 200, [("Content-Type", "text/javascript"), ("Cache-Control", "no-store"),
                                       ("Access-Control-Allow-Origin", "*"),
                                       ("Access-Control-Allow-Private-Network", "true")], js.encode())
        if path.startswith("/__replicator/status"):
            data = {"archive": self.archive.name, "start_url": self.start_url,
                    "hits": self.hits, "misses": self.misses[-500:]}
            return self._send(h, 200, [("Content-Type", "application/json")], json.dumps(data, indent=2).encode())
        if method == "OPTIONS":
            return self._send(h, 204, [
                ("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Methods", "*"),
                ("Access-Control-Allow-Headers", h.headers.get("Access-Control-Request-Headers", "*")),
                ("Access-Control-Max-Age", "86400")], b"")

        # ---- map local path -> original URL
        target = self._target_url(path)
        length = int(h.headers.get("Content-Length") or 0)
        req_body = h.rfile.read(length) if length > 0 else None

        prefer = []
        ref = h.headers.get("Referer") or ""
        if ref:
            ref_path = urlsplit(ref).path + (("?" + urlsplit(ref).query) if urlsplit(ref).query else "")
            prefer.append(origin_of(self._target_url(ref_path or "/")))
        prefer.append(self.primary_origin)

        conn = self._conn()
        row, how = self.archive.lookup(conn, method, target, req_body, tuple(dict.fromkeys(prefer)))
        if row is None and method in ("GET", "HEAD"):
            dest = h.headers.get("Sec-Fetch-Dest", "")
            accept = h.headers.get("Accept", "")
            if dest in ("document", "iframe") or (not dest and "text/html" in accept):
                if self.is_spa:  # client-side router: every route is the same HTML shell
                    row, how = self.archive.start_document(conn), "spa-fallback"
                else:            # normal multi-page site: say clearly the page wasn't captured
                    self.misses.append(f"{method} {target} (page not captured)")
                    self.log(f"  page not captured: {short(target)}")
                    page = NOT_SAVED_HTML.format(url=html.escape(target), home=html.escape(path_query(self.start_url)))
                    return self._send(h, 404, [("Content-Type", "text/html; charset=utf-8"),
                                               ("Cache-Control", "no-store")], page.encode())
        if row is None:
            self.misses.append(f"{method} {target}")
            self.log(f"  miss: {method} {short(target)}")
            return self._send(h, 404, [("Content-Type", "text/plain"), ("Access-Control-Allow-Origin", "*"),
                                       ("Cache-Control", "no-store")],
                              f"Not in archive: {method} {target}\n".encode())
        if row["truncated"]:
            self.misses.append(f"{method} {target} (over size limit at capture)")
        self.hits += 1

        body: bytes = row["body"] or b""
        pairs = replay_headers(json.loads(row["resp_headers"]), row["url"])
        ctype = next((v for k, v in pairs if k.lower() == "content-type"), "")
        if "html" in ctype.lower() and row["status"] == 200:
            body = inject_guard(body, self._guard)

        status = row["status"]
        if 300 <= status < 400:  # redirects were followed at capture time; never replay a bare 3xx
            status = 200
        rng = h.headers.get("Range")
        if rng and status == 200 and body:
            r = parse_range(rng, len(body))
            if r == "unsatisfiable":
                return self._send(h, 416, [("Content-Range", f"bytes */{len(body)}")], b"")
            if r:
                start, end = r
                return self._send(h, 206, pairs + [("Content-Range", f"bytes {start}-{end}/{len(body)}")],
                                  body[start:end + 1])
        self._send(h, status, pairs, body, row["status_text"] or None)

    def _target_url(self, path: str) -> str:
        if path.startswith("/__ext__/"):
            rest = path[len("/__ext__/"):]
            scheme, _, rest = rest.partition("/")
            # host ends at the first '/', '?' or end of string
            m = re.match(r"([^/?]*)(.*)", rest)
            host, tail = m.group(1), m.group(2)
            if not tail.startswith("/"):
                tail = "/" + tail
            return f"{scheme}://{host}{tail}"
        return self.primary_origin + path
